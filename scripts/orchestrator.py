#!/usr/bin/env python3
"""Lit-Review Orchestrator — run the pipeline from a document.

Front-end is any .tex or .docx document describing the article — a full
manuscript, an abstract, or a proposal. Stage 0 extracts a search plan (an
Undermind brief + Google Scholar queries + the research question); the search
channels then run, and results are merged and screened.

Stages:
  0  Extract       parse manuscript -> search_plan.json + scholar_queries.json
                   + undermind_brief.txt           (Claude)
  1  Undermind     automated Classic deep search from the brief (Playwright;
                   signs in with stored credentials; see undermind-search/)
  2  Scholar Labs  Google Scholar Labs deep search from a research question
                   (Playwright; signs in with stored credentials)  [default]
  2b Deep Research Gemini Deep Research Agent (Interactions API, GEMINI_API_KEY)
                   [default] (alternative deep-search pathway; see deepresearch-search/)
  4a Google Scholar SearchAPI.io, driven by the extracted queries  [default]
  4b Supplementary SSRN / NBER / HeinOnline / forthcoming          [opt-in]
  4c Citation chain Semantic Scholar (needs DOI-bearing seeds)      [opt-in]
  4e Free index    keyless OpenAlex / Crossref / Semantic Scholar lexical search [default]
                   (Web search / Stage 4d is agent-only — it needs the agent's
                   WebSearch tools and is not run by this autonomous runner.)
  5  Dedup         merge all outputs, DOI + LLM
  6  Screen        abstract screening against the research question

DAG:
  Stage 0 runs first (everything depends on it).
  Stages 1, 2, 2b, 4a, 4b, 4e run concurrently.
  Stage 4c runs after the search stages (it seeds from their output).
  Stage 5 waits for everything; Stage 6 runs after Stage 5.

A raw query (no manuscript) is still supported via --query, which skips Stage 0.

This orchestrator is the AUTONOMOUS FALLBACK: it runs every stage as a subprocess,
and the reasoning stages (extract, dedup, screen) call the Anthropic API on Sonnet
(DeepSeek for dedup). The default agent-driven flow — where Opus 4.8 (the
orchestrator agent plus subagents) performs those reasoning stages through each
script's emit/ingest seam, with Sonnet used only for Undermind's in-browser
clarifying answers — is documented in SKILL.md ("How it runs").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Load ~/.lit-review-pipeline.env on startup (portable API key store)
# ---------------------------------------------------------------------------
_env_file = Path.home() / ".lit-review-pipeline.env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        print(
            f"[ORCH] Warning: python-dotenv not installed — skipping {_env_file}\n"
            "       Install with: pip install python-dotenv",
            file=sys.stderr,
        )

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

# Sub-skills live as sibling directories to scripts/
_ORCH_DIR = Path(__file__).resolve().parent
SKILLS_DIR = _ORCH_DIR.parent

STAGE_LABELS = {
    "extract":       ("Stage 0 ", "Extract"),
    "undermind":     ("Stage 1 ", "Undermind"),
    "scholarlabs":   ("Stage 2 ", "Scholar Labs"),
    "deepresearch":  ("Stage 2b", "Deep Research"),
    "scholar":       ("Stage 4a", "Google Scholar"),
    "supplementary": ("Stage 4b", "Supplementary"),
    "citation":      ("Stage 4c", "Citation Chain"),
    "freesearch":    ("Stage 4e", "Free Index"),
    "dedup":         ("Stage 5 ", "Dedup"),
    "screen":        ("Stage 6 ", "Screening"),
}

SYM_PENDING  = "○"  # ○
SYM_RUNNING  = "●"  # ●
SYM_COMPLETE = "✓"  # ✓
SYM_FAILED   = "✗"  # ✗
SYM_SKIPPED  = "—"  # —
SYM_DEFERRED = "⊘"  # ⊘


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StageResult:
    name: str
    status: str = "pending"   # pending|running|complete|failed|skipped|deferred
    paper_count: int | None = None
    elapsed: float = 0.0
    returncode: int | None = None
    error_msg: str = ""
    output_path: Path | None = None   # the stage's -o/--output target, for this run


@dataclass
class PipelineState:
    stages: dict[str, StageResult] = field(default_factory=dict)
    log_file: object = None
    start_time: float = 0.0


# ---------------------------------------------------------------------------
# Logging / dashboard
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def log_line(state: PipelineState, stage_tag: str, line: str):
    formatted = f"{_timestamp()} [{stage_tag}] {line}"
    print(formatted, flush=True)
    if state.log_file:
        state.log_file.write(formatted + "\n")
        state.log_file.flush()


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def print_dashboard(state: PipelineState):
    lines = ["\n=== Pipeline Status ==="]
    for key, (num, label) in STAGE_LABELS.items():
        sr = state.stages.get(key)
        if sr is None:
            continue

        if sr.status == "pending":
            sym, txt, detail, el = SYM_PENDING, "pending", "", ""
        elif sr.status == "running":
            sym, txt, detail, el = SYM_RUNNING, "running", "...", ""
        elif sr.status == "complete":
            sym, txt = SYM_COMPLETE, "complete"
            detail = f"{sr.paper_count} papers" if sr.paper_count is not None else ""
            el = _fmt_elapsed(sr.elapsed)
        elif sr.status == "deferred":
            sym, txt, detail = SYM_DEFERRED, "deferred", "driver pending"
            el = _fmt_elapsed(sr.elapsed)
        elif sr.status == "failed":
            sym, txt = SYM_FAILED, "FAILED"
            detail = sr.error_msg[:40] if sr.error_msg else ""
            el = _fmt_elapsed(sr.elapsed)
        elif sr.status == "skipped":
            sym, txt, detail, el = SYM_SKIPPED, "skipped", "", ""
        else:
            sym, txt, detail, el = "?", sr.status, "", ""

        parts = [f"  {num} {label:<18s} {sym} {txt:<10s}"]
        parts.append(f" {detail:<16s}" if detail else f" {'':16s}")
        if el:
            parts.append(f" {el}")
        lines.append("".join(parts))

    out = "\n".join(lines)
    print(out, flush=True)
    if state.log_file:
        state.log_file.write(out + "\n")
        state.log_file.flush()


# ---------------------------------------------------------------------------
# Run a single stage as a subprocess
# ---------------------------------------------------------------------------

async def run_stage(state: PipelineState, stage_key: str, cmd: list[str]) -> StageResult:
    import traceback as _traceback

    sr = state.stages[stage_key]
    tag = STAGE_LABELS[stage_key][1].upper().replace(" ", "-")
    sr.status = "running"
    print_dashboard(state)

    t0 = time.time()
    log_line(state, tag, f"=== START {datetime.now().isoformat(timespec='seconds')} ===")
    log_line(state, tag, f"Command: {' '.join(cmd)}")

    paper_count = None
    deferred = False
    stderr_lines: list[str] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )

        async def read_stdout():
            nonlocal deferred
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                log_line(state, tag, line)
                if "_DEFERRED" in line:
                    deferred = True

        async def read_stderr():
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                log_line(state, tag, f"[STDERR] {line}")
                stderr_lines.append(line)
                if len(stderr_lines) > 150:
                    stderr_lines.pop(0)

        await asyncio.gather(read_stdout(), read_stderr())
        await proc.wait()
        sr.elapsed = time.time() - t0
        sr.returncode = proc.returncode
        # Authoritative count: read the stage's own JSON output (-o), not stdout.
        out_path = None
        for i, tok in enumerate(cmd):
            if tok in ("-o", "--output") and i + 1 < len(cmd):
                out_path = Path(cmd[i + 1])
                break
        if out_path is not None and out_path.exists():
            paper_count = _count_papers(out_path)
        sr.paper_count = paper_count
        sr.output_path = out_path
        end_ts = datetime.now().isoformat(timespec="seconds")

        if proc.returncode == 0 and deferred:
            sr.status = "deferred"
            log_line(state, tag, f"=== DEFERRED {end_ts} | elapsed={_fmt_elapsed(sr.elapsed)} ===")
        elif proc.returncode == 0:
            sr.status = "complete"
            bits = [f"=== END {end_ts}", f"elapsed={_fmt_elapsed(sr.elapsed)}"]
            if paper_count is not None:
                bits.append(f"papers={paper_count}")
            log_line(state, tag, " | ".join(bits) + " ===")
        else:
            sr.status = "failed"
            sr.error_msg = f"exit code {proc.returncode}"
            log_line(state, tag,
                     f"=== FAILED {end_ts} | exit_code={proc.returncode}"
                     f" | elapsed={_fmt_elapsed(sr.elapsed)} ===")
            if stderr_lines:
                log_line(state, tag, "--- STDERR TAIL ---")
                for ln in stderr_lines[-60:]:
                    log_line(state, tag, f"  {ln}")
                log_line(state, tag, "--- END STDERR TAIL ---")

    except Exception as exc:
        sr.elapsed = time.time() - t0
        sr.status = "failed"
        sr.error_msg = str(exc)[:80]
        log_line(state, tag, f"=== EXCEPTION {type(exc).__name__}: {exc} ===")
        for ln in _traceback.format_exc().splitlines():
            log_line(state, tag, f"  {ln}")

    print_dashboard(state)
    return sr


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def _py() -> str:
    return sys.executable


def cmd_extract(args, output_dir: Path) -> list[str]:
    return [
        _py(), str(_ORCH_DIR / "extract_search_plan.py"),
        str(Path(args.document).expanduser()),
        "-o", str(output_dir / "search_plan.json"),
        "--model", args.model,
        "--max-chars", str(args.max_chars),
    ]


def cmd_undermind(output_dir: Path) -> list[str]:
    return [
        _py(), str(SKILLS_DIR / "undermind-search" / "scripts" / "undermind_search.py"),
        "--brief-file", str(output_dir / "undermind_brief.txt"),
        "-o", str(output_dir / "stage1_undermind.json"),
        "--debug-dir", str(output_dir / "debug_undermind"),
    ]


def cmd_scholarlabs(output_dir: Path, research_question: str) -> list[str]:
    # --hidden runs a real headed Chrome positioned off-screen. Google blocks
    # headless Scholar with an "unusual traffic" CAPTCHA, but a genuine headful
    # window (even off-screen) passes and reuses the seeded ~/.scholar-profile
    # session — so the stage works without a window in the user's face. A 2FA
    # wall defers with a desktop alert to re-run --login (visible). See
    # scholarlabs-search/SKILL.md.
    return [
        _py(), str(SKILLS_DIR / "scholarlabs-search" / "scripts" / "scholarlabs_search.py"),
        "--query-file", str(output_dir / "scholarlabs_query.txt"),
        "--research-question", research_question,
        "-o", str(output_dir / "stage2_scholarlabs.json"),
        "--debug-dir", str(output_dir / "debug_scholarlabs"),
        "--hidden",
    ]


def cmd_deepresearch(output_dir: Path, research_question: str) -> list[str]:
    # API-driven Gemini Deep Research (no browser). Uses the rich Undermind brief
    # as the research description, with the research question as a fallback.
    # Defers gracefully if GEMINI_API_KEY is unset. See deepresearch-search/.
    return [
        _py(), str(SKILLS_DIR / "deepresearch-search" / "scripts" / "deepresearch_search.py"),
        "--query-file", str(output_dir / "undermind_brief.txt"),
        "--research-question", research_question,
        "-o", str(output_dir / "stage2b_deepresearch.json"),
        "--debug-dir", str(output_dir / "debug_deepresearch"),
    ]


def _supp_script() -> str:
    return str(SKILLS_DIR / "supplementary-search" / "scripts" / "supplementary_search.py")


def cmd_scholar(args, output_dir: Path, doc_mode: bool) -> list[str]:
    cmd = [_py(), _supp_script(), "--scholar",
           "-o", str(output_dir / "stage4a_scholar.json"),
           "--debug-dir", str(output_dir / "debug_scholar")]
    if doc_mode:
        cmd += ["--queries-file", str(output_dir / "scholar_queries.json")]
    else:
        cmd += [args.query]
    return cmd


def cmd_supplementary(args, output_dir: Path, doc_mode: bool) -> list[str]:
    cmd = [_py(), _supp_script(),
           "-o", str(output_dir / "stage4b_supplementary.json"),
           "--debug-dir", str(output_dir / "debug_supplementary")]
    if args.ssrn:
        cmd.append("--ssrn")
    if args.nber:
        cmd.append("--nber")
    if args.heinonline:
        cmd.append("--heinonline")
    if args.forthcoming:
        cmd.append("--forthcoming")
    if doc_mode:
        cmd += ["--queries-file", str(output_dir / "scholar_queries.json")]
    else:
        cmd += [args.query]
    return cmd


def cmd_citation(args, output_dir: Path, seeds_file: Path) -> list[str]:
    return [
        _py(), _supp_script(), "--citation-chain",
        "--seeds-from", str(seeds_file),
        "--top-seeds", str(args.top_seeds),
        "-o", str(output_dir / "stage4c_citations.json"),
        "--debug-dir", str(output_dir / "debug_citations"),
    ]


def cmd_freesearch(args, output_dir: Path, doc_mode: bool) -> list[str]:
    # Keyless lexical search of OpenAlex / Crossref / Semantic Scholar (Stage 4e).
    # Defaults (per-query count, all three sources) suit an unattended run, and it
    # defers gracefully (FREESEARCH_DEFERRED, empty output, exit 0) on any failure.
    cmd = [
        _py(), str(SKILLS_DIR / "freesearch-search" / "scripts" / "freesearch_search.py"),
        "-o", str(output_dir / "stage4e_freesearch.json"),
    ]
    if doc_mode:
        cmd += ["--queries-file", str(output_dir / "scholar_queries.json")]
    else:
        cmd += ["--query", args.query]
    return cmd


def cmd_screen(args, output_dir: Path, research_question: str) -> list[str]:
    return [
        _py(), str(SKILLS_DIR / "lit-screen" / "scripts" / "lit_screen.py"),
        "--input", str(output_dir / "stage5_merged.json"),
        "--query", research_question,
        "-o", str(output_dir / "stage6_screened.json"),
        "--model", args.screen_model,
        "--concurrency", "5",
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_papers(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


def _collect_dedup_inputs(output_dir: Path, state: PipelineState) -> list[Path]:
    """Collect THIS run's search-stage outputs (RIS fallback when JSON is empty).

    Only stages that completed or deferred in the current run contribute, so a
    stale stageN_*.json left behind by an earlier run in a reused output
    directory cannot contaminate the merge.
    """
    search_stages = ("undermind", "scholarlabs", "deepresearch", "scholar",
                     "supplementary", "citation", "freesearch")
    json_inputs: list[Path] = []
    ris_inputs: list[Path] = []
    for key in search_stages:
        sr = state.stages.get(key)
        if not sr or sr.status not in ("complete", "deferred"):
            continue
        jp = sr.output_path
        if jp is None:
            continue
        if jp.exists() and _count_papers(jp) > 0:
            json_inputs.append(jp)
        else:
            rp = jp.with_suffix(".ris")
            if rp.exists() and rp.stat().st_size > 0:
                ris_inputs.append(rp)
                log_line(state, "ORCH", f"Including {rp.name} (JSON missing/empty)")
    return sorted(set(json_inputs + ris_inputs))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(args: argparse.Namespace):
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    doc_mode = bool(args.document)
    skip = set(args.skip or [])
    quick = getattr(args, "quick", False)
    if quick:
        # Quick mode is a Google-Scholar-only run: drop the browser/API deep-search
        # stages AND the opt-in supplementary/citation sources, matching the GUI's
        # quick behavior so the CLI promise of "only SearchAPI Google Scholar" holds.
        skip |= {"undermind", "scholarlabs", "deepresearch"}

    supp_on = (not quick) and any([args.ssrn, args.nber, args.heinonline, args.forthcoming])
    cite_on = (not quick) and bool(args.citation_chain)
    # Keyless free-index search (Stage 4e): on by default, off in quick (GS-only) mode.
    freesearch_on = (not quick) and getattr(args, "freesearch", True)

    # Register stages (in dashboard order). Optional sources only appear when on.
    state = PipelineState()
    state.start_time = time.time()

    def _reg(key: str, active: bool):
        state.stages[key] = StageResult(name=key, status="pending" if active else "skipped")

    if doc_mode:
        _reg("extract", True)
    _reg("undermind", "undermind" not in skip)
    _reg("scholarlabs", getattr(args, "scholarlabs", False) and "scholarlabs" not in skip)
    _reg("deepresearch", "deepresearch" not in skip)
    _reg("scholar", "scholar" not in skip)
    _reg("freesearch", freesearch_on)
    if supp_on:
        _reg("supplementary", True)
    if cite_on:
        _reg("citation", True)
    dedup_active = "dedup" not in skip
    _reg("dedup", dedup_active)
    _reg("screen", dedup_active and "screen" not in skip)

    log_path = output_dir / "pipeline.log"
    state.log_file = open(log_path, "w", encoding="utf-8")

    log_line(state, "ORCH", f"=== PIPELINE START {datetime.now().isoformat(timespec='seconds')} ===")
    if doc_mode:
        log_line(state, "ORCH", f"Document: {args.document}")
    else:
        log_line(state, "ORCH", f"Query: {args.query[:200]}")
    log_line(state, "ORCH", f"Output dir: {output_dir}")
    print_dashboard(state)

    # -----------------------------------------------------------------------
    # Phase 0: Extract (manuscript mode) OR synthesize plan (query mode)
    # -----------------------------------------------------------------------
    research_question = args.query or ""
    if doc_mode:
        await run_stage(state, "extract", cmd_extract(args, output_dir))
        if state.stages["extract"].status != "complete":
            log_line(state, "ORCH", "Stage 0 failed — cannot continue without a search plan.")
            _finish(state, output_dir, log_path, fatal=True)
            return
        try:
            plan = json.loads((output_dir / "search_plan.json").read_text(encoding="utf-8"))
            research_question = plan.get("research_question", "") or research_question
            state.stages["extract"].paper_count = len(plan.get("scholar_queries", []))
        except Exception as exc:
            log_line(state, "ORCH", f"Could not read search_plan.json: {exc}")
            _finish(state, output_dir, log_path, fatal=True)
            return
    else:
        # Query mode: synthesize the inputs the channels need.
        (output_dir / "undermind_brief.txt").write_text(args.query, encoding="utf-8")
        (output_dir / "scholarlabs_query.txt").write_text(args.query, encoding="utf-8")
        log_line(state, "ORCH", "Query mode — Stage 0 skipped; using raw query.")

    if not research_question:
        research_question = args.query or "(no research question extracted)"

    # -----------------------------------------------------------------------
    # Phase 1: concurrent search stages
    # -----------------------------------------------------------------------
    tasks: dict[str, asyncio.Task] = {}
    if state.stages["undermind"].status == "pending":
        tasks["undermind"] = asyncio.create_task(run_stage(state, "undermind", cmd_undermind(output_dir)))
    if state.stages["scholarlabs"].status == "pending":
        tasks["scholarlabs"] = asyncio.create_task(
            run_stage(state, "scholarlabs", cmd_scholarlabs(output_dir, research_question)))
    if state.stages["deepresearch"].status == "pending":
        tasks["deepresearch"] = asyncio.create_task(
            run_stage(state, "deepresearch", cmd_deepresearch(output_dir, research_question)))
    if state.stages["scholar"].status == "pending":
        tasks["scholar"] = asyncio.create_task(
            run_stage(state, "scholar", cmd_scholar(args, output_dir, doc_mode)))
    if state.stages["freesearch"].status == "pending":
        tasks["freesearch"] = asyncio.create_task(
            run_stage(state, "freesearch", cmd_freesearch(args, output_dir, doc_mode)))
    if supp_on and state.stages["supplementary"].status == "pending":
        tasks["supplementary"] = asyncio.create_task(
            run_stage(state, "supplementary", cmd_supplementary(args, output_dir, doc_mode)))

    if tasks:
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    log_line(state, "ORCH", "Search stages finished")

    # -----------------------------------------------------------------------
    # Phase 2: citation chain (needs DOI-bearing seeds from prior stages)
    # -----------------------------------------------------------------------
    if cite_on and state.stages["citation"].status == "pending":
        # Seed from every DOI-bearing search output (Undermind, Scholar Labs,
        # Google Scholar, supplementary), merged into one file. supplementary's
        # _load_seeds sorts by score/citations and keeps the DOI-bearing top-N.
        seed_stage_keys = ("undermind", "scholarlabs", "deepresearch",
                           "scholar", "supplementary", "freesearch")
        merged_seeds: list[dict] = []
        for key in seed_stage_keys:
            sr = state.stages.get(key)
            if not sr or sr.status not in ("complete", "deferred"):
                continue
            p = sr.output_path
            if p is None or not p.exists():
                continue
            try:
                recs = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(recs, list):
                    merged_seeds.extend(recs)
            except Exception:
                pass
        doi_seeds = [r for r in merged_seeds if isinstance(r, dict) and r.get("doi")]
        if not doi_seeds:
            state.stages["citation"].status = "skipped"
            state.stages["citation"].error_msg = "no seed papers"
            log_line(state, "ORCH", "Stage 4c skipped — no DOI-bearing seed papers available")
            print_dashboard(state)
        else:
            # Write only the DOI-bearing seeds, so the downstream top-N selection
            # (which keeps the DOI-bearing top-N) cannot silently start with zero
            # usable seeds even though the pre-check found some.
            seeds_file = output_dir / "_citation_seeds.json"
            seeds_file.write_text(json.dumps(doi_seeds, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            log_line(state, "ORCH", f"Citation seeds: {len(doi_seeds)} DOI-bearing "
                                    f"records from this run's search stages")
            await run_stage(state, "citation", cmd_citation(args, output_dir, seeds_file))

    # -----------------------------------------------------------------------
    # Phase 3: Dedup
    # -----------------------------------------------------------------------
    if state.stages["dedup"].status == "pending":
        inputs = _collect_dedup_inputs(output_dir, state)
        total_papers = sum(_count_papers(f) for f in inputs if f.suffix == ".json")
        has_ris = any(f.suffix == ".ris" for f in inputs)
        if not inputs or (total_papers == 0 and not has_ris):
            state.stages["dedup"].status = "skipped"
            state.stages["dedup"].error_msg = "no papers"
            log_line(state, "ORCH", "No papers from any stage — skipping dedup")
        else:
            dedup_cmd = [
                _py(), str(SKILLS_DIR / "lit-dedup" / "scripts" / "lit_dedup.py"),
                "--yes",
            ]
            if args.no_llm:
                dedup_cmd.append("--no-llm")
            if not getattr(args, "verify", True):
                dedup_cmd.append("--no-verify")
            dedup_cmd += ["--inputs", *[str(f) for f in inputs],
                          "-o", str(output_dir / "stage5_merged.json")]
            await run_stage(state, "dedup", dedup_cmd)

    # -----------------------------------------------------------------------
    # Phase 4: Screen
    # -----------------------------------------------------------------------
    if state.stages["screen"].status == "pending":
        merged = output_dir / "stage5_merged.json"
        if not merged.exists():
            state.stages["screen"].status = "skipped"
            state.stages["screen"].error_msg = "no input"
            log_line(state, "ORCH", "No stage5_merged.json — skipping screening")
            print_dashboard(state)
        else:
            await run_stage(state, "screen", cmd_screen(args, output_dir, research_question))

    _finish(state, output_dir, log_path, fatal=False)


def _finish(state: PipelineState, output_dir: Path, log_path: Path, fatal: bool):
    total = time.time() - state.start_time
    log_line(state, "ORCH",
             f"=== PIPELINE END {datetime.now().isoformat(timespec='seconds')} "
             f"| elapsed={_fmt_elapsed(total)} ===")
    print_dashboard(state)

    print(f"\n{'=' * 52}")
    print(f"Pipeline finished in {_fmt_elapsed(total)}")
    failed = [s for s in state.stages.values() if s.status == "failed"]
    deferred = [s for s in state.stages.values() if s.status == "deferred"]
    dedup_sr = state.stages.get("dedup")
    if dedup_sr and dedup_sr.status == "complete" and dedup_sr.paper_count is not None:
        print(f"After dedup: {dedup_sr.paper_count} unique papers")
    if deferred:
        print(f"Deferred: {', '.join(s.name for s in deferred)} "
              "(see undermind_brief.txt / scholarlabs_query.txt for a manual run)")
    if failed:
        print("Failed stages: " + ", ".join(f"{s.name} ({s.error_msg})" for s in failed))
    print(f"Outputs: {output_dir}")
    print(f"Log:     {log_path}")
    print("=" * 52)

    if state.log_file:
        state.log_file.close()
    # Only a failed REQUIRED stage (the processing backbone) is fatal. A search
    # channel that errored without reaching its graceful-defer path should not
    # turn an otherwise usable run red — its results are simply absent from the merge.
    required_failed = [s for s in failed if s.name in ("extract", "dedup", "screen")]
    if fatal or required_failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Lit-Review Orchestrator — run the pipeline from a manuscript.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("document", nargs="?", metavar="DOCUMENT",
                   help="Path to a .tex or .docx document (full manuscript, "
                        "abstract, or any description of the article)")
    p.add_argument("--query", default="",
                   help="Run from a raw query string instead of a manuscript (skips Stage 0)")
    p.add_argument("--output-dir", default="./lit-review-output",
                   help="Output directory (default: ./lit-review-output)")
    p.add_argument("--model", default="claude-sonnet-4-6",
                   help="Claude model for Stage 0 extraction (default: claude-sonnet-4-6)")
    p.add_argument("--screen-model", default="claude-sonnet-4-6",
                   help="Claude model for Stage 6 screening (default: claude-sonnet-4-6)")
    p.add_argument("--max-chars", type=int, default=30000,
                   help="Max manuscript characters sent to the extractor (default: 30000)")
    p.add_argument("--skip", nargs="*", default=[],
                   choices=["undermind", "scholarlabs", "deepresearch", "scholar", "dedup", "screen"],
                   help="Default stages to skip")
    p.add_argument("--quick", action="store_true",
                   help="Fast run: keep ONLY the SearchAPI Google Scholar channel — skip the "
                        "Undermind, Scholar Labs, and Deep Research deep searches AND force the "
                        "opt-in supplementary/citation sources off (no login/browser/API deep search)")
    # Opt-in deep-search / extra sources (default off)
    p.add_argument("--scholarlabs", action="store_true",
                   help="Also run the Google Scholar Labs deep search (Stage 2). OFF by default: "
                        "Google rate-limits its Cite/BibTeX export under automation, so it often "
                        "defers. Enable it and retry from a fresh session when not throttled.")
    p.add_argument("--ssrn", action="store_true", help="Also search SSRN")
    p.add_argument("--nber", action="store_true", help="Also search NBER")
    p.add_argument("--heinonline", action="store_true", help="Also search HeinOnline")
    p.add_argument("--forthcoming", action="store_true",
                   help="Also search forthcoming lists (JF, JFE, RFS)")
    p.add_argument("--citation-chain", action="store_true",
                   help="Also run Semantic Scholar citation chaining (needs DOI seeds)")
    p.add_argument("--freesearch", action=argparse.BooleanOptionalAction, default=True,
                   help="Keyless free-index search (Stage 4e: OpenAlex/Crossref/Semantic "
                        "Scholar lexical search). On by default; use --no-freesearch to skip. "
                        "Web search (Stage 4d) is agent-only and not available in this runner.")
    # Tuning
    p.add_argument("--no-llm", action="store_true", help="DOI-only dedup (skip LLM pass)")
    p.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                   help="Cross-check papers against OpenAlex/Crossref/Semantic Scholar and drop "
                        "those no index can confirm (anti-hallucination). On by default; use "
                        "--no-verify to keep all. Dropped papers go to stage5_merged_unverified.json.")
    p.add_argument("--top-seeds", type=int, default=20, help="Seeds for citation chaining")

    args = p.parse_args()
    if not args.document and not args.query:
        p.error("Provide a manuscript path (.docx/.tex) or --query \"...\"")
    if args.document and args.query:
        p.error("Provide either a manuscript or --query, not both")

    asyncio.run(run_pipeline(args))


if __name__ == "__main__":
    main()
