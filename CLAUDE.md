# CLAUDE.md — lit-review-orchestrator

A Claude Code skill that runs a document-driven literature-review pipeline. This
directory is a **standalone, public git repo**
(github.com/kennethkhoocy/lit-review-orchestrator); anything committed here ships
publicly, so keep additions neutral and free of secrets or personal data.

## Canonical docs — read before changing behavior

- **`SKILL.md`** — the invocation reference: stage map, exact per-stage commands, CLI flags, GUI config shape.
- **`README.md`** — public explanation + hosted architecture diagrams (`docs/images/`).

When you change stages, flags, the GUI config JSON, or output files, update
`SKILL.md`, `README.md`, and the `docs/images/` diagrams together — they are kept in sync by hand.

## Architecture

- Multi-stage pipeline keyed by stage number (0, 1, 2/2b, 4a–4e, 5/5b, 6). Stage table is in `SKILL.md`.
- **Two run modes.** *Agent-driven* (default): the agent does every LLM step at the agent layer with **zero Anthropic API**, routed by model — **Opus 4.8** orchestrates, extracts the search plan (Stage 0), runs the keyless web search (4d), and re-ranks (Stage 6); **Sonnet 4.6** subagents do the rest (4a query writing, Stage 5 dedup, Undermind clarifications). The GUI's *Use Opus for all tasks* (`all_opus`) promotes everything to Opus. Both models are keyless (Task/Agent tool, not the API). See `SKILL.md` → "Model routing". *Autonomous fallback*: `scripts/orchestrator.py` runs end-to-end with reasoning on the Sonnet/DeepSeek API.
- **Each search channel is its own sub-skill directory**: `<channel>-search/scripts/<channel>_search.py` (driver) + `<channel>_ingest.py` (parse/enrich/normalize into `stageN_*.json`). Stage 5 dedup + 5b verification live in `lit-dedup/scripts/lit_dedup.py`; Stage 6 screening in `lit-screen/scripts/lit_screen.py`; Stage 0 in `scripts/extract_search_plan.py`.

## Key convention: the emit/ingest seam

Reasoning scripts never call the agent directly. The LLM step is split into a pair
of file flags — `--emit-*` / `--plan-file` writes a tasks/prompt file, the agent
fills it on the routed model (Sonnet subagent by default; Opus for Stage 0 extraction,
the keyless web search, and the re-ranker, or for everything when `all_opus`), an
`--ingest-*` flag reads it back — so the deterministic work
(parsing, validation, enrichment, merge, file I/O) stays in the script. When
editing any reasoning script, preserve this seam **and** keep its in-script
Sonnet/DeepSeek fallback working for unattended runs.

## Conventions & gotchas

- **Graceful degradation:** a failed channel prints `<NAME>_DEFERRED`, writes empty results, and the pipeline continues. Do not make a channel failure fatal — only Stage 0 failure aborts the run.
- **Windows-first.** PowerShell is the primary shell. Playwright drivers (Undermind, Scholar Labs) need `playwright install chromium`. Scholar Labs runs headed-but-off-screen because Google CAPTCHA-blocks headless.
- **Secrets / git hygiene:** API keys live in `~/.lit-review-pipeline.env`. `*.env`, `AUDIT.md`, `_legacy/` (login screenshots/HTML dumps), `lit-review-output/`, and `debug_*/` are gitignored — keep them out of commits.
- `scripts/manuscript_parser.py` is bundled so a fresh clone parses `.docx`/`.tex` with only `python-docx`.

## Verifying changes

No automated test suite. Verify a change by running the affected stage as a
subprocess against `examples/sample_manuscript.tex` into a scratch `--output-dir`
and inspecting the resulting `stageN_*.json`.
