---
name: undermind-search
description: >
  Stage 1 of the lit review pipeline: run an Undermind.ai "Classic" deep search
  from the natural-language brief produced by Stage 0, then parse + enrich the
  exported references into the pipeline schema. The driver logs in automatically
  with stored credentials and runs headless via Playwright. Only use this skill
  when explicitly requested. Do NOT auto-trigger on general literature review or
  paper search requests.
---

# Undermind Search (Stage 1)

Takes the **Undermind brief** that Stage 0 extracts and returns the papers
Undermind finds, enriched and saved as `<stem>.json` + `<stem>.bib` for dedup and
screening.

The stage is two halves:

- **`scripts/undermind_search.py`** — a Playwright driver that signs in, drives the
  Classic search UI, and exports the references (BibTeX by default).
- **`scripts/undermind_ingest.py`** — UI-independent parsing + enrichment. It reads
  the exported `.bib`/`.ris`, fills missing DOIs (Crossref) and abstracts/journals
  (OpenAlex), and writes the pipeline JSON. Importable, and runnable standalone on
  any reference file.

## Quick start

```bash
# One-time setup: store Undermind credentials and verify sign-in (opens a browser)
python scripts/undermind_search.py --login

# Driven by the orchestrator (the normal path)
python scripts/undermind_search.py --brief-file undermind_brief.txt \
    -o stage1_undermind.json --debug-dir debug_undermind

# Standalone, from a short query, watching the browser
python scripts/undermind_search.py --query "dual-class shares cost of equity" --headed

# Ingest an already-exported file (no browser)
python scripts/undermind_ingest.py --input references.bib -o stage1_undermind.json
```

## The flow (validated against the June 2026 UI)

`app.undermind.ai` → (auto-login if needed) → sidebar **Classic** → **Search** →
type the brief into "I want to find…" → answer Undermind's clarifying questions
(a Sonnet subagent by default, Opus when `all_opus`, via `--answers-dir`; or the
Sonnet API as the standalone fallback) → **Generate Research
Report** → wait for the report → **Export → BibTeX**
→ parse + enrich → `stage1_undermind.json` (+ `.bib`).

Undermind's clarifying questions render as clickable chips, but the composer also
accepts free text; the driver answers in free text, which is robust to UI changes.

## Login & credentials

The driver uses a dedicated persistent browser profile (`--user-data-dir`, default
`~/.undermind-profile`) and signs in with:

| Variable | Meaning |
|----------|---------|
| `UNDERMIND_EMAIL` | Undermind account email |
| `UNDERMIND_PASSWORD` | Undermind account password |

Stored in `~/.lit-review-pipeline.env` (gitignored; never commit credentials).
`--login` captures them on first run if they are absent (prompts, then writes them
to the env file) and verifies sign-in. Once the profile holds a valid session,
later runs skip the login step; if the session lapses, the driver logs in again
from the stored credentials.

**Answering clarifying questions — two modes.** In the orchestrator's agent-driven
flow, pass `--answers-dir DIR`: the driver writes each question to
`clarify_request_<n>.json` and waits for the orchestrator to drop
`clarify_answer_<n>.json` (`{"answer": "..."}`) — produced by a Sonnet subagent by
default, or Opus when `all_opus` — then types it — no API key is
used, and the stage is interactive in this mode. Without `--answers-dir`
(standalone or autonomous fallback) the driver answers with the Anthropic API
(Sonnet), which requires `ANTHROPIC_API_KEY`.

## CLI

| Flag | Default | Description |
|------|---------|-------------|
| `--brief-file PATH` | — | File holding the brief (the orchestrator passes this) |
| `--query TEXT` | — | Short brief for standalone use |
| `-o, --output PATH` | `stage1_undermind.json` | Output JSON (a `.bib` sibling is written) |
| `--debug-dir PATH` | — | Save step screenshots / HTML dumps here |
| `--format {bibtex,ris}` | `bibtex` | Format to export from Undermind |
| `--user-data-dir PATH` | `~/.undermind-profile` | Persistent browser profile |
| `--model ID` | `claude-sonnet-4-6` | Model for clarifying answers in API/autonomous mode |
| `--answers-dir PATH` | — | Agent-in-the-loop: hand clarifying questions to a subagent via files (no API), Sonnet by default / Opus when `all_opus`; the orchestrator default |
| `--headed` | off | Show the browser (debugging) |
| `--login` | off | First-run setup: capture credentials + verify sign-in (headed) |
| `--no-enrich` | off | Skip Crossref/OpenAlex enrichment |

## Graceful degradation

If credentials are missing, login fails, or the export does not produce a file,
the driver prints the `UNDERMIND_DEFERRED` sentinel, writes an empty result list,
and exits 0. The orchestrator marks the stage **deferred** and the rest of the
pipeline (Google Scholar, dedup, screening) still completes. The brief is always
available in `undermind_brief.txt` for a manual run.

## Output schema

Same as the other stages, with `source: "undermind"`:

```json
{"title": "...", "authors": "A, B", "year": "2024",
 "doi": "https://doi.org/10.x/y", "abstract": "...", "journal": "...",
 "url": "...", "source": "undermind", "verified": true,
 "citations": 0, "open_access": false}
```

## Testing with windows-mcp (headed)

The Playwright driver is the production path. When the Undermind UI changes and a
selector needs re-confirming, drive the live site by hand with the **windows-mcp**
desktop tools (open Chrome, walk Classic → Search → export) to read the current
element labels/roles, then update the corresponding Playwright locators here. The
durable locators in use — roles/text/placeholders ("Classic", "Search",
placeholder "I want to find…", "Generate Research Report", "Export", menu item
"BibTeX", inputs `#email`/`#password`) — are chosen to survive minor UI churn.

## Troubleshooting

- **`UNDERMIND_DEFERRED: Not logged in...`** — run `--login` once, or check the env
  credentials. Inspect `--debug-dir` screenshots (`00_landing`, `00a_login_modal`).
- **Stuck after launch** — `wait_for_report` polls up to 20 min. Check
  `05_search_launched` / `06_report_*` screenshots; the "Generate Research Report"
  or "Export" locator may need re-confirming via windows-mcp.
- **Login special characters** — credentials are read from the env file (not the
  shell), so passwords with `!`, `$`, etc. are passed through unmangled.
- The previous Playwright automation (older UI) is preserved under `../_legacy/`.
