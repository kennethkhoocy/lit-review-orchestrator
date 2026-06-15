---
name: scholarlabs-search
description: >
  Stage 2 of the lit review pipeline: run a Google Scholar Labs deep search from
  the detailed research question produced by Stage 0, scrape each result's
  citation (Cite -> BibTeX), then parse + enrich into the pipeline schema. The
  driver signs in to Google with a persistent profile and runs headless via
  Playwright. Only use this skill when explicitly requested. Do NOT auto-trigger
  on general literature review or paper search requests.
---

# Scholar Labs Search (Stage 2)

Takes the **Scholar Labs query** that Stage 0 extracts — a single detailed
natural-language research question — and returns the papers Google Scholar Labs
surfaces, enriched and saved as `<stem>.json` + `<stem>.bib` for dedup and
screening.

The stage is two halves:

- **`scripts/scholarlabs_search.py`** — a Playwright driver that signs in, submits
  the question to Scholar Labs, and reads each result's citation via the standard
  Scholar **Cite → BibTeX** export.
- **`scripts/scholarlabs_ingest.py`** — UI-independent parsing + enrichment. It
  parses the collected BibTeX, fills missing DOIs (Crossref) and
  abstracts/journals (OpenAlex), and writes the pipeline JSON. Importable, and
  runnable standalone on any `.bib`/`.ris` file.

## Quick start

```bash
# One-time setup: sign in to Google and seed the session (opens a browser).
# Complete any 2FA yourself in the window; the session then persists.
python scripts/scholarlabs_search.py --login

# Driven by the orchestrator (the normal path)
python scripts/scholarlabs_search.py --query-file scholarlabs_query.txt \
    -o stage2_scholarlabs.json --debug-dir debug_scholarlabs

# Standalone, from a question, watching the browser
python scripts/scholarlabs_search.py --query "How do dual-class shares affect the cost of equity?" --headed

# Ingest an already-collected .bib (no browser)
python scripts/scholarlabs_ingest.py --input references.bib -o stage2_scholarlabs.json
```

## The flow (validated against the June 2026 UI)

`scholar.google.com/scholar_labs/search` (signed in) → type the detailed research
question into **"Ask Scholar"** → submit → a `/session/<id>` opens and the model
evaluates results ("Evaluated N top results" → **"Found N relevant results"**) →
for each result card click **Cite** → read the **BibTeX** export link → fetch
every link with the logged-in session → parse + enrich →
`stage2_scholarlabs.json` (+ `.bib`).

Unlike Undermind, Scholar Labs asks **no clarifying questions** — it answers a
single detailed question directly — so there is no clarification loop and
`ANTHROPIC_API_KEY` is not needed. Scholar Labs typically returns ~10 highly
relevant results, complementing Undermind's broader set.

## The Scholar Labs query is distinct from the Undermind brief

Scholar Labs retrieves papers from a **concise but general natural-language
question** — one sentence of roughly 15-30 words, pitched at the *literature area*
the review must cover rather than the paper's narrow finding, so its ~10 results
are the surrounding foundational and related work. Two failure modes to avoid:
an over-long or multi-part query (or one with "emphasize X rather than Y"
meta-instructions) returns nothing because the input is limited, and an
over-narrow query that stacks the paper's exact setting/method/sub-conditions
returns too few papers. Stage 0 emits a dedicated `scholar_labs_query` field
(written to `scholarlabs_query.txt`), separate from `undermind_brief.txt` and
`scholar_queries.json`. The driver reads that file via `--query-file`;
`--research-question` is the fallback, and as a safety net the driver trims an
over-long query to its first sentence before submitting.

## Login & credentials

The driver uses a dedicated persistent browser profile (`--user-data-dir`,
default `~/.scholar-profile`) and a Google account:

| Variable | Meaning |
|----------|---------|
| `SCHOLAR_EMAIL` | Google account email |
| `SCHOLAR_PASSWORD` | Google account password |

Stored in `~/.lit-review-pipeline.env` (gitignored; never commit credentials).
**Google blocks scripted password entry**, so the reliable mechanism is the
persistent session: run `--login` once (headed), sign in by hand — including any
2FA — and the session cookie persists for later runs. On first run `--login`
captures the credentials if they are absent (prompts, then writes them to the env
file); if they are already set they pre-fill the form so you never re-enter them,
and the driver attempts best-effort autofill before falling back to manual sign-in.

**2FA:** the first run is headed, so you complete 2FA live in the window. If a
later **headless** run finds the session lapsed and hits a login/2FA wall, the
driver raises a desktop notification (`SCHOLARLABS_2FA`) and defers, telling you
to re-run `--login`.

**Why off-screen, not headless:** Google serves automated/headless Scholar a
non-interactive "unusual traffic" block — a bare "try again later" page with **no
reCAPTCHA widget**, so a CAPTCHA solver has nothing to act on — and Chrome's "new"
headless (`--invisible`) is detected the same way (both tested, both fail).
Firecrawl's stealth proxy *does* reach Scholar without the block, but the Google
sign-in it lands on is itself CAPTCHA-gated, so it cannot authenticate the Labs
surface either. A *real* headed Chrome passes, so the orchestrator runs the stage
with `--hidden`: a genuine headed window positioned far off-screen — invisible to
you yet accepted by Google. Use `--headed` (on-screen) for the first `--login`
and whenever a 2FA approval is needed, since an off-screen window can't be
interacted with.

## CLI

| Flag | Default | Description |
|------|---------|-------------|
| `--query-file PATH` | — | File holding the research question (the orchestrator passes this) |
| `--query TEXT` | — | Research question for standalone use |
| `--research-question TEXT` | — | Fallback question if no query/file is given |
| `-o, --output PATH` | `stage2_scholarlabs.json` | Output JSON (a `.bib` sibling is written) |
| `--debug-dir PATH` | — | Save step screenshots / HTML dumps here |
| `--format {bibtex,ris}` | `bibtex` | Export to read from the Cite menu (BibTeX or RefMan/RIS) |
| `--user-data-dir PATH` | `~/.scholar-profile` | Persistent browser profile |
| `--headed` | off | Show the browser on-screen (first login / 2FA / debugging) |
| `--hidden` | off | Real headed Chrome positioned off-screen — invisible but passes Google (the orchestrator default) |
| `--invisible` | off | Try Chrome's "new" headless — usually still blocked by Google; not recommended |
| `--login` | off | First-run setup: capture credentials (prompts if missing) + sign in (headed) |
| `--no-enrich` | off | Skip Crossref/OpenAlex enrichment |

## Graceful degradation

If credentials are missing, sign-in fails, 2FA is required headlessly, or no
citable results are found, the driver prints the `SCHOLARLABS_DEFERRED` sentinel,
writes an empty result list, and exits 0. The orchestrator marks the stage
**deferred** and the rest of the pipeline (Undermind, Google Scholar, dedup,
screening) still completes. The question remains in `scholarlabs_query.txt` for a
manual run.

## Output schema

Same as the other stages, with `source: "scholarlabs"`:

```json
{"title": "...", "authors": "A, B", "year": "2024",
 "doi": "https://doi.org/10.x/y", "abstract": "...", "journal": "...",
 "url": "...", "source": "scholarlabs", "verified": true,
 "citations": 0, "open_access": false}
```

## Testing with windows-mcp (headed)

The Playwright driver is the production path. When the Scholar Labs UI changes and
a selector needs re-confirming, drive the live site by hand with the
**windows-mcp** desktop tools (open Chrome, submit a question, open a result's
**Cite** popup, read the **BibTeX** link) to read the current element
labels/roles, then update the corresponding Playwright locators here. The durable
locators in use — roles/text ("Ask Scholar" / "Ask a follow-up" textbox, "Found N
relevant results", "Cite" buttons, the "BibTeX" link, "Cancel") — are chosen to
survive minor UI churn.

## Troubleshooting

- **`SCHOLARLABS_DEFERRED: Not signed in...`** — run `--login` once, or check the
  env credentials. Inspect `--debug-dir` screenshots (`00_landing`, `00a_signin`).
- **`SCHOLARLABS_2FA` desktop alert** — a headless run needs re-authorization; run
  `--login` (headed) and approve the sign-in / 2FA, then re-run the pipeline.
- **"This browser or app may not be secure"** — the driver launches with
  anti-automation flags to avoid this, but if Google still blocks sign-in, sign in
  manually in the `--login` window; the seeded session then carries headless runs.
- **No results / empty output** — check `04_results` / `05_no_cites` screenshots;
  the "Cite" or "BibTeX" locator may need re-confirming via windows-mcp.
- **0 citable links / "try again later"** — Google rate-limits the Cite/export
  operation under automation (toast: "The system can't perform the operation now.
  Try again later."), so the modal opens but shows no BibTeX link. The driver
  paces clicks, backs off, and defers cleanly when it persists. It is aggravated
  by many rapid runs; wait and retry from a fresh session, or rely on the Deep
  Research (Stage 2b) and Undermind pathways, which do not hit this limit.
- **Login special characters** — credentials are read from the env file (not the
  shell), so passwords with `!`, `$`, etc. are passed through unmangled.
