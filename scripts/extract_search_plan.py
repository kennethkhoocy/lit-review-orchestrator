#!/usr/bin/env python3
"""Stage 0 — Parse a document and extract a literature-search plan.

Reads a .docx or .tex document (a full manuscript, an abstract, or any text
describing the article), sends the salient text to Claude, and produces the
material the two downstream channels need:

  * undermind_brief   — a rich natural-language research description for
                        Undermind.ai (which searches on descriptions, not
                        keywords), plus suggested answers to the clarifying
                        questions Undermind typically asks.
  * scholar_queries   — short keyword queries for Google Scholar.
  * research_question — used by the screening stage.

Outputs (next to ``-o``):
  search_plan.json      full structured plan (+ manuscript meta)
  search_plan.md        human-readable rendering
  scholar_queries.json  bare array of query strings (for --queries-file)
  undermind_brief.txt   the brief, ready to paste / feed to the driver

Usage:
    python extract_search_plan.py paper.docx -o search_plan.json
    python extract_search_plan.py paper.tex  -o out/search_plan.json --model claude-opus-4-8

Requires: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Load ~/.lit-review-pipeline.env if present (portable key store)
_env_file = Path.home() / ".lit-review-pipeline.env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

# Import the bundled parser (same directory)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from manuscript_parser import parse_manuscript, build_salient_text  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-6"

REQUIRED_KEYS = (
    "title", "research_question", "contribution", "key_concepts",
    "literature_themes", "undermind_brief", "undermind_clarifications",
    "scholar_labs_query", "scholar_queries",
)

PROMPT_TEMPLATE = """\
You are preparing a literature-review search plan from a research document.
The document may be a full manuscript, an abstract, a proposal, or any text
describing an article's content; infer reasonably what is not stated explicitly,
and do not invent specifics the document does not support.
Read the document content and produce a JSON object that will drive two
academic search channels:
  (1) Undermind.ai, which searches on a rich natural-language research
      description and asks a few clarifying questions; and
  (2) Google Scholar, which takes short keyword queries.

Return ONLY a JSON object (no prose, no markdown fences) with EXACTLY these keys:

- "title": the paper's title (infer from the content if not stated).
- "research_question": one or two sentences stating the central question the
  paper addresses.
- "contribution": one or two sentences on what the paper contributes / which
  gap it fills.
- "key_concepts": array of 5-12 short noun-phrase concepts central to the work.
- "literature_themes": array of 3-7 objects, each
  {{"theme": short name, "rationale": why this body of work is relevant}}.
- "undermind_brief": ONE rich paragraph of 120-220 words describing, in natural
  language, the set of papers the author needs in order to situate this work.
  Cover the topic, the setting/population, the time period, the theoretical
  framing, the empirical method or identification strategy, and the kinds of
  related work that should surface. Write it as a research description a search
  engine can act on — not as a summary of this paper.
- "undermind_clarifications": array of 3-5 objects, each
  {{"likely_question": a clarifying question Undermind might ask,
    "suggested_answer": a concise answer grounded in the manuscript}}
  (scope, time period, methods, fields to include or exclude, and similar).
- "scholar_labs_query": ONE concise research QUESTION for Google Scholar Labs,
  phrased as a researcher would type it into a search box — a SINGLE sentence of
  roughly 15-30 words, under ~200 characters. Pitch it at the LEVEL OF THE
  LITERATURE this review must cover, NOT at the paper's narrow finding: name the
  central construct(s) and the broad relationship or area of interest so Scholar
  Labs surfaces the foundational and related work, not only papers testing the
  exact hypothesis. Do NOT over-specify by stacking the paper's precise setting,
  method, time frame, and sub-conditions into one query (that returns too few or
  zero papers), and do NOT write a paragraph, multiple sentences, sub-lists, or
  meta-instructions like "emphasize X rather than Y" (Scholar Labs has a limited
  input). Make it broader than the short scholar_queries and distinct from the
  undermind_brief paragraph. Example of the right scope — general, not narrow:
  "How do dual-class share structures and disproportionate voting rights affect
  firm value, governance, and the cost of capital?"
- "scholar_queries": array of 8-12 short Google Scholar queries, 3-6 words each,
  that together cover the literature themes and the key methods; include a few
  that pair the topic with the method.

Document content:
<<<
{salient}
>>>
"""


def validate_plan(plan) -> list[str]:
    """Return a list of schema problems with a plan dict (empty list == valid).

    Shared by the API retry loop and the agent-driven --plan-file ingest path so
    both enforce exactly the same contract.
    """
    if not isinstance(plan, dict):
        return ["top-level value was not a JSON object"]
    problems = [k for k in REQUIRED_KEYS if k not in plan]
    # Scalars the downstream stages actually consume must be non-empty strings.
    for field in ("research_question", "undermind_brief", "scholar_labs_query"):
        val = plan.get(field)
        if not isinstance(val, str) or not val.strip():
            problems.append(f"{field} (must be a non-empty string)")
    # scholar_queries must be a non-empty list of non-empty strings.
    sq = plan.get("scholar_queries")
    if not isinstance(sq, list) or not sq:
        problems.append("scholar_queries (must be a non-empty array)")
    elif not all(isinstance(q, str) and q.strip() for q in sq):
        problems.append("scholar_queries (every entry must be a non-empty string)")
    # key_concepts is rendered as a list; require list shape if present.
    if "key_concepts" in plan and not isinstance(plan.get("key_concepts"), list):
        problems.append("key_concepts (must be an array)")
    # literature_themes / undermind_clarifications render as lists of dicts/strings;
    # require list shape so rendering never silently drops them.
    for field in ("literature_themes", "undermind_clarifications"):
        if field in plan and not isinstance(plan.get(field), list):
            problems.append(f"{field} (must be an array)")
    return problems


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def extract_plan(salient: str, model: str) -> dict:
    """Call Claude and return the validated plan dict.

    JSON parsing AND schema validation share one retry loop, with the specific
    problem fed back into the prompt, so a syntactically valid but incomplete
    response is repaired rather than aborting Stage 0 (which is fatal).
    """
    import anthropic

    client = anthropic.Anthropic()
    prompt = PROMPT_TEMPLATE.format(salient=salient)

    def _call(extra: str = "") -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt + extra}],
        )
        return resp.content[0].text

    extra = ""
    last_err = "no response"
    for _attempt in range(3):
        raw = _call(extra)
        try:
            plan = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as e:
            last_err = f"invalid JSON ({e})"
            extra = "\n\nReturn ONLY a single valid JSON object — no prose, no code fences."
            continue
        if not isinstance(plan, dict):
            last_err = "top-level value was not a JSON object"
            extra = "\n\nReturn ONLY a single JSON OBJECT with the required keys."
            continue
        problems = validate_plan(plan)
        if problems:
            last_err = f"missing/invalid: {problems}"
            extra = (f"\n\nThe previous response had these problems: {problems}. "
                     "Return ONLY a single valid JSON object with EXACTLY the required "
                     "keys and correct types.")
            continue
        return plan
    raise ValueError(f"Extraction failed after 3 attempts ({last_err}).")


def _as_list(value) -> list:
    """Coerce a plan field to a list so rendering never crashes on a bad shape."""
    return value if isinstance(value, list) else []


def render_markdown(plan: dict, meta: dict) -> str:
    lines = [
        f"# Search plan — {plan.get('title', '(untitled)')}",
        "",
        f"*Source: `{meta.get('path', '')}` ({meta.get('format', '')}, "
        f"{meta.get('n_sections', 0)} sections, {meta.get('n_footnotes', 0)} footnotes)*",
        "",
        "## Research question",
        plan.get("research_question", ""),
        "",
        "## Contribution",
        plan.get("contribution", ""),
        "",
        "## Key concepts",
        ", ".join(str(x) for x in _as_list(plan.get("key_concepts"))),
        "",
        "## Literature themes",
    ]
    for t in _as_list(plan.get("literature_themes")):
        if isinstance(t, dict):
            lines.append(f"- **{t.get('theme', '')}** — {t.get('rationale', '')}")
        else:
            lines.append(f"- {t}")
    lines += [
        "",
        "## Undermind brief",
        plan.get("undermind_brief", ""),
        "",
        "### Suggested answers to likely clarifying questions",
    ]
    for c in _as_list(plan.get("undermind_clarifications")):
        if isinstance(c, dict):
            lines.append(f"- *Q:* {c.get('likely_question', '')}")
            lines.append(f"  *A:* {c.get('suggested_answer', '')}")
        else:
            lines.append(f"- {c}")
    lines += ["", "## Scholar Labs query", plan.get("scholar_labs_query", "")]
    lines += ["", "## Google Scholar queries"]
    for q in _as_list(plan.get("scholar_queries")):
        lines.append(f"- {q}")
    lines.append("")
    return "\n".join(lines)


def _meta_from_parsed(parsed: dict, salient: str | None = None) -> dict:
    return {
        "path": parsed["path"],
        "format": parsed["format"],
        "n_sections": len(parsed["sections"]),
        "n_footnotes": len(parsed["footnotes"]),
        "salient_chars": len(salient) if salient is not None else 0,
    }


def _write_plan_outputs(plan: dict, meta: dict, out_json: Path) -> None:
    """Write the five Stage-0 artifacts. Shared by the API path and --plan-file."""
    out_json.parent.mkdir(parents=True, exist_ok=True)
    stem = out_json.stem
    out_md = out_json.with_name(f"{stem}.md")
    out_queries = out_json.with_name("scholar_queries.json")
    out_brief = out_json.with_name("undermind_brief.txt")
    out_slq = out_json.with_name("scholarlabs_query.txt")

    out_json.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_markdown(plan, meta), encoding="utf-8")
    out_queries.write_text(
        json.dumps(plan.get("scholar_queries", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    out_brief.write_text(plan.get("undermind_brief", ""), encoding="utf-8")
    out_slq.write_text(plan.get("scholar_labs_query", ""), encoding="utf-8")

    print(f"[EXTRACT] Wrote {out_json}")
    print(f"[EXTRACT] Wrote {out_md}")
    print(f"[EXTRACT] Wrote {out_queries} ({len(plan.get('scholar_queries', []))} queries)")
    print(f"[EXTRACT] Wrote {out_brief}")
    print(f"[EXTRACT] Wrote {out_slq}")
    print(f"[EXTRACT] Research question: {plan.get('research_question', '')[:160]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract a literature-search plan from a manuscript.")
    ap.add_argument("manuscript", metavar="DOCUMENT",
                    help="Path to a .tex or .docx document (manuscript, abstract, or description)")
    ap.add_argument("-o", "--output", default="search_plan.json",
                    help="Output JSON path (default: search_plan.json)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Anthropic model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--max-chars", type=int, default=30000,
                    help="Max manuscript characters sent to the model (default: 30000)")
    ap.add_argument("--emit-prompt", metavar="PATH",
                    help="Agent-driven mode: write the fully-built extraction prompt to PATH and "
                         "exit (no API call). Complete it with Opus, then pass it via --plan-file.")
    ap.add_argument("--plan-file", metavar="PATH",
                    help="Agent-driven mode: read an Opus-produced plan JSON from PATH, validate it, "
                         "and write all Stage-0 outputs (no API call).")
    args = ap.parse_args()

    # Agent-driven ingest: validate an Opus-produced plan and write outputs (no API).
    if args.plan_file:
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        problems = validate_plan(plan)
        if problems:
            print(f"[EXTRACT] Provided plan is invalid: {problems}", file=sys.stderr)
            sys.exit(1)
        parsed = parse_manuscript(args.manuscript)
        salient = build_salient_text(parsed, args.max_chars)
        meta = _meta_from_parsed(parsed, salient)
        plan["_manuscript"] = meta
        _write_plan_outputs(plan, meta, Path(args.output))
        return

    print(f"[EXTRACT] Parsing {args.manuscript}")
    parsed = parse_manuscript(args.manuscript)
    salient = build_salient_text(parsed, args.max_chars)
    meta = _meta_from_parsed(parsed, salient)
    print(f"[EXTRACT] {meta['format']} | {meta['n_sections']} sections | "
          f"{meta['n_footnotes']} footnotes | {meta['salient_chars']} salient chars")

    # Agent-driven emit: write the prompt for Opus to complete (no API).
    if args.emit_prompt:
        Path(args.emit_prompt).write_text(PROMPT_TEMPLATE.format(salient=salient), encoding="utf-8")
        print(f"[EXTRACT] Wrote extraction prompt to {args.emit_prompt} "
              f"({meta['salient_chars']} salient chars). Complete with Opus, then --plan-file.")
        return

    print(f"[EXTRACT] Extracting search plan via {args.model}...")
    plan = extract_plan(salient, args.model)
    plan["_manuscript"] = meta
    _write_plan_outputs(plan, meta, Path(args.output))


if __name__ == "__main__":
    main()
