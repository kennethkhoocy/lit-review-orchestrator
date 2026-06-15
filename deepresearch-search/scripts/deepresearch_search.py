#!/usr/bin/env python3
"""Stage 2b — Google Gemini Deep Research deep search (API-driven, no browser).

Calls the Gemini Deep Research Agent (Interactions API) to autonomously research
the prior literature for the brief that Stage 0 produced, then parses the cited
report into the pipeline schema. This is an *alternative* deep-search pathway to
Undermind (Stage 1) and Scholar Labs (Stage 2); it uses GEMINI_API_KEY and runs
as a plain subprocess — it is a retrieval service, not part of the agent's own
reasoning, so it fits both the agent-driven and autonomous runs.

Model (agent): deep-research-max-preview-04-2026 (maximum comprehensiveness),
overridable with --model. The task runs in the background (the API requires it)
and is polled to completion; a task takes minutes (the API caps it at 60).

Flow:
  read brief -> wrap into a literature-review prompt that ends with a parseable
  "KEY PAPERS" section -> POST /v1beta/interactions (background) -> poll
  GET /v1beta/interactions/{id} until completed -> write the report (.md) and the
  raw response (.json) to the debug dir -> deepresearch_ingest parses the KEY
  PAPERS lines (and any citations) into stage2b_deepresearch.json (+ .bib).

Graceful degradation: if GEMINI_API_KEY is missing, or the task fails/times out,
or no papers can be parsed, prints DEEPRESEARCH_DEFERRED, writes empty outputs,
and exits 0 so the rest of the pipeline still runs.

Requires: GEMINI_API_KEY, requests
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deepresearch_ingest import ingest_report  # noqa: E402

SENTINEL = "DEEPRESEARCH_DEFERRED"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"
DEFAULT_MODEL = "deep-research-max-preview-04-2026"
POLL_INTERVAL_S = 15
TASK_TIMEOUT_S = 2400          # 40 min; Deep Research caps at 60, most finish < 20
HEARTBEAT_EVERY_S = 60

PROMPT_TEMPLATE = """\
You are compiling the prior academic literature for the literature-review section \
of a research paper. Conduct deep research to identify the most important and \
relevant PRIOR academic works that this research builds on — foundational papers, \
closely related empirical and theoretical studies, and key methodological \
references — across the relevant literatures. Prioritize peer-reviewed journal \
articles and serious working papers; avoid blogs, news, and marketing pages.

Research description:
{brief}

Do NOT write an essay, a synthesis, or any discussion. Your ENTIRE response must be \
the ranked list of works described below and nothing else. Output a section that \
begins with a line containing ONLY the heading:

KEY PAPERS

After that heading, list 20-40 of the works you found, RANKED BY IMPORTANCE (the \
most seminal and most relevant first), ONE WORK PER LINE in EXACTLY this \
pipe-delimited format with four "|" separators per line:

Title | Authors (comma-separated) | Year | Venue or Journal | DOI or URL

These rules for the KEY PAPERS section are mandatory:
- The heading must be exactly "KEY PAPERS" on its own line — not "Sources", \
"References", or "Bibliography", and with no numbering or extra words.
- Do NOT format the list as a Markdown table: no leading or trailing "|" on a \
line, and no "---" separator row — just the five fields joined by " | ".
- Put one real paper per line; never place taxonomy categories, section labels, \
or commentary in this section, and never use a placeholder such as "Title".
- Every line must contain exactly four "|"; if a field is unknown leave it empty \
but keep the pipes, e.g.  Smith on control premia | Smith, J. | 2019 |  | https://doi.org/10.x .\
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _defer(output: Path, message: str) -> None:
    """Graceful no-op: emit sentinel, write empty outputs, exit 0."""
    _log(f"{SENTINEL}: {message}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("[]", encoding="utf-8")
    output.with_suffix(".bib").write_text("", encoding="utf-8")
    sys.exit(0)


def _load_brief(args) -> str:
    if args.query_file and Path(args.query_file).is_file():
        t = Path(args.query_file).read_text(encoding="utf-8").strip()
        if t:
            return t
    if args.query:
        return args.query.strip()
    if args.research_question:
        return args.research_question.strip()
    return ""


def _collect_output_text(obj) -> str:
    """Pull the FINAL report text out of the (beta) Interactions response.

    The ``steps`` array interleaves the echoed input prompt and intermediate
    "thinking" narration with the final report, so joining every text item would
    fold the prompt and progress notes into the parsed report (and confuse the
    KEY PAPERS parser). Strategy: an explicit ``output_text`` wins; otherwise take
    the report-bearing step (the last step carrying a substantial text block);
    otherwise fall back to a defensive join of any text items found."""
    # 1) explicit output_text anywhere wins
    if isinstance(obj, dict):
        explicit: list[str] = []
        seen: set[int] = set()

        def walk_ot(o, depth=0):
            if depth > 200 or id(o) in seen:
                return
            if isinstance(o, dict):
                seen.add(id(o))
                ot = o.get("output_text")
                if isinstance(ot, str) and ot.strip():
                    explicit.append(ot)
                for v in o.values():
                    walk_ot(v, depth + 1)
            elif isinstance(o, list):
                seen.add(id(o))
                for v in o:
                    walk_ot(v, depth + 1)

        walk_ot(obj)
        if explicit:
            return max(explicit, key=len)

        # 2) per-step text: the report is the last step with a real text block
        steps = obj.get("steps")
        if isinstance(steps, list):
            step_texts: list[str] = []
            for s in steps:
                if not isinstance(s, dict):
                    continue
                cont = s.get("content")
                if not isinstance(cont, list):
                    continue
                txt = "\n".join(
                    c["text"] for c in cont
                    if isinstance(c, dict) and c.get("type") == "text"
                    and isinstance(c.get("text"), str)
                )
                if txt.strip():
                    step_texts.append(txt)
            if step_texts:
                longest = max(step_texts, key=len)
                last = step_texts[-1]
                # Prefer the final step (the report); fall back to the longest if
                # the last step is only a short note or caption.
                return last if len(last) >= 0.5 * len(longest) else longest

    # 3) defensive: join any {"type":"text"} items found anywhere
    found: list[str] = []
    seen2: set[int] = set()

    def walk(o, depth=0):
        if depth > 200 or id(o) in seen2:
            return
        if isinstance(o, dict):
            seen2.add(id(o))
            if (o.get("type") == "text" and isinstance(o.get("text"), str)
                    and o["text"].strip()):
                found.append(o["text"])
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            seen2.add(id(o))
            for v in o:
                walk(v, depth + 1)

    walk(obj)
    return "\n".join(found)


def _start_task(api_key: str, prompt: str, model: str) -> dict:
    import requests
    body = {
        "agent": model,
        "input": prompt,
        "agent_config": {"type": "deep-research"},
        "background": True,
        "store": True,
    }
    headers = {
        "x-goog-api-key": api_key,
        "Api-Revision": API_REVISION,
        "Content-Type": "application/json",
    }
    r = requests.post(API_BASE, json=body, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def _get_task(api_key: str, interaction_id: str) -> dict:
    import requests
    headers = {"x-goog-api-key": api_key, "Api-Revision": API_REVISION}
    r = requests.get(f"{API_BASE}/{interaction_id}", headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def run(args) -> None:
    # Resolve the output path first so any failure below can defer to it.
    output = Path(args.output)

    # Preflight (debug dir, brief load, prompt write) runs inside the top-level
    # defer wrapper so a local I/O or setup failure still emits the sentinel,
    # writes the empty output, and exits 0 like every other failure path.
    try:
        debug_dir = Path(args.debug_dir) if args.debug_dir else output.parent
        debug_dir.mkdir(parents=True, exist_ok=True)

        brief = _load_brief(args)
        if not brief:
            _defer(output, "No brief/query supplied (need --query-file/--query/--research-question).")
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            _defer(output, "GEMINI_API_KEY not set in environment / ~/.lit-review-pipeline.env.")

        prompt = PROMPT_TEMPLATE.format(brief=brief)
        (debug_dir / "deepresearch_prompt.txt").write_text(prompt, encoding="utf-8")
    except SystemExit:
        raise
    except Exception as e:
        _defer(output, f"Deep Research preflight failed ({type(e).__name__}: {str(e)[:160]}).")

    try:
        _log(f"Starting Gemini Deep Research ({args.model})...")
        started = _start_task(api_key, prompt, args.model)
    except Exception as e:
        _defer(output, f"Could not start Deep Research task ({type(e).__name__}: {str(e)[:160]}).")

    interaction_id = started.get("id") or started.get("name", "").split("/")[-1]
    if not interaction_id:
        (debug_dir / "deepresearch_start.json").write_text(
            json.dumps(started, ensure_ascii=False, indent=2), encoding="utf-8")
        _defer(output, "Deep Research start response had no interaction id (see debug dir).")
    _log(f"  Task started: {interaction_id}. Polling (this can take many minutes)...")

    start = time.time()
    last_beat = 0.0
    result: dict = {}
    while time.time() - start < TASK_TIMEOUT_S:
        time.sleep(POLL_INTERVAL_S)
        try:
            result = _get_task(api_key, interaction_id)
        except Exception as e:
            _log(f"  (poll error, retrying: {type(e).__name__})")
            continue
        status = (result.get("status") or "").lower()
        elapsed = time.time() - start
        if elapsed - last_beat >= HEARTBEAT_EVERY_S:
            last_beat = elapsed
            _log(f"    status={status or '?'} ({int(elapsed)}s)")
        if status in ("completed", "succeeded", "done"):
            _log(f"  Completed after {int(elapsed)}s.")
            break
        if status in ("failed", "error", "cancelled", "canceled"):
            (debug_dir / "deepresearch_raw.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            _defer(output, f"Deep Research task {status} (see debug dir).")
    else:
        _defer(output, f"Deep Research task did not finish within {TASK_TIMEOUT_S}s.")

    # Persist the raw response + the report for inspection / re-ingest.
    (debug_dir / "deepresearch_raw.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report_text = _collect_output_text(result)
    (debug_dir / "deepresearch_report.md").write_text(report_text, encoding="utf-8")
    _log(f"  Report length: {len(report_text)} chars. Parsing KEY PAPERS...")

    try:
        results = ingest_report(report_text, result, output, enrich=not args.no_enrich)
    except SystemExit:
        raise
    except Exception as e:
        _defer(output, f"Ingest of the Deep Research report failed ({type(e).__name__}: {str(e)[:160]}).")

    if not results:
        _defer(output, "Deep Research returned a report but no papers could be parsed "
                       "(see deepresearch_report.md in the debug dir).")
    _log(f"Saved {len(results)} papers to {output}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Gemini Deep Research deep-search driver (Stage 2b).")
    ap.add_argument("--query-file", default="", help="File holding the brief/research description")
    ap.add_argument("--query", default="", help="Brief/research description (standalone use)")
    ap.add_argument("--research-question", default="", help="Fallback question if no query/file")
    ap.add_argument("-o", "--output", default="stage2b_deepresearch.json", help="Output JSON path")
    ap.add_argument("--debug-dir", default="", help="Directory for the report + raw response dumps")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Deep Research agent id (default: {DEFAULT_MODEL}; "
                         "use deep-research-preview-04-2026 for the cheaper/faster tier)")
    ap.add_argument("--no-enrich", action="store_true", help="Skip Crossref/OpenAlex enrichment")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
