# AGENTS.md - lit-review-orchestrator

This repository contains a document-driven literature-review pipeline intended to
run as both a Claude Code skill and a Codex skill. It is a standalone public GitHub
repository at `github.com/kennethkhoocy/lit-review-orchestrator`; keep committed
content neutral and free of secrets or personal data.

## Canonical Docs

- `SKILL.md`: shared invocation reference, stage map, exact commands, CLI flags,
  GUI config shape, and emit/ingest contracts.
- `docs/codex.md`: Codex-specific model routing, subagent authorization, and
  keyless profile.
- `docs/claude-code.md`: Claude Code-specific routing and tool notes.
- `README.md`: public explanation, installation instructions for both hosts, and
  hosted architecture diagrams from `docs/images/`.

When changing stages, flags, GUI config JSON, output files, or routing semantics,
update `SKILL.md`, `README.md`, and the relevant platform appendix together.
Update the diagrams in `docs/images/` when the architecture changes.

## Codex Routing

Use the shared emit/ingest workflow in `SKILL.md`. For Codex runs, apply
`docs/codex.md`:

- Use `gpt-5.5` with `xhigh` reasoning for Stage 0 extraction, Stage 4d keyless
  web-search batches, and Stage 6 screening.
- Use cheaper Codex workers for lower-stakes batch judgments: `gpt-5.4-mini`
  with `medium` reasoning for Stage 4a query writing and Undermind clarifying
  answers, and `gpt-5.4-mini` with `high` reasoning for Stage 5 dedup judgments.
  Escalate low-confidence dedup pairs to `gpt-5.5`.
- Codex subagents require explicit user authorization. Ask before spawning
  parallel workers unless the user has already requested parallel Codex subagents.
- Treat the GUI field `all_opus` as a high-accuracy routing request in Codex:
  use `gpt-5.5` with `xhigh` reasoning for all delegated reasoning stages.

## Architecture

- The pipeline is keyed by stage number: 0, 1, 2/2b, 4a-4e, 5/5b, and 6.
- Each search channel owns a directory such as `<channel>-search/` with driver
  and ingest scripts that normalize records into `stageN_*.json`.
- Stage 5 deduplication and verification live in `lit-dedup/scripts/lit_dedup.py`.
  Stage 6 screening lives in `lit-screen/scripts/lit_screen.py`. Stage 0 lives in
  `scripts/extract_search_plan.py`.
- Reasoning scripts expose an emit/ingest seam. Preserve that seam when editing:
  scripts emit prompt or task files, the agent writes JSON results, and scripts
  ingest those results to finish deterministic output generation.
- The autonomous fallback is `scripts/orchestrator.py`, which runs end-to-end with
  API-backed reasoning for unattended runs.

## Conventions

- Failed search channels should print `<NAME>_DEFERRED`, write empty results, and
  allow the pipeline to continue. Stage 0 failure aborts the run.
- PowerShell is the primary shell for local work. Playwright drivers need
  `playwright install chromium`.
- API keys belong in `~/.lit-review-pipeline.env`; real environment variables
  take precedence.
- Keep `*.env`, `AUDIT.md`, `_legacy/`, `lit-review-output/`, `debug_*/`, and
  generated run artifacts out of commits.
- `scripts/manuscript_parser.py` is bundled so a fresh clone can parse `.docx`
  and `.tex` inputs with the listed Python dependencies.

## Verification

There is no full automated test suite. For documentation or metadata changes, run
the Codex skill validator against the root skill. For behavior changes, run the
affected stage as a subprocess against `examples/sample_manuscript.tex` into a
scratch `--output-dir`, then inspect the resulting `stageN_*.json` files.
