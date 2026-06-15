---
name: deepresearch-search
description: >
  Stage 2b of the lit review pipeline: run a Google Gemini Deep Research deep
  search (Interactions API) from the brief produced by Stage 0, then parse the
  cited report into the pipeline schema. API-driven (GEMINI_API_KEY), no browser.
  An alternative deep-search pathway alongside Undermind (Stage 1) and Scholar
  Labs (Stage 2). Only use this skill when explicitly requested. Do NOT
  auto-trigger on general literature review or paper search requests.
---

# Deep Research Search (Stage 2b)

Takes the Stage-0 brief and uses the **Gemini Deep Research Agent** to
autonomously plan, search, read, and synthesize the prior literature, then parses
the resulting cited report into `<stem>.json` + `<stem>.bib` for dedup and
screening. It complements the other deep searches: Undermind and Scholar Labs are
browser-driven; this one is a pure API call.

The stage is two halves:

- **`scripts/deepresearch_search.py`** — calls the Interactions API
  (`deep-research-max-preview-04-2026` by default), runs the task in the
  background, polls to completion, and saves the report + raw response.
- **`scripts/deepresearch_ingest.py`** — UI-independent parsing. It reads the
  agent's `KEY PAPERS` section (and, as a fallback, citations in the raw
  response), best-effort-enriches via Crossref, and writes the pipeline JSON.
  Importable, and runnable standalone on a saved report.

## The flow

`brief` → wrapped into a literature-review prompt that ends with a parseable
`KEY PAPERS` section (`Title | Authors | Year | Venue | DOI or URL`, one per
line) → `POST /v1beta/interactions` with `background=true`, `store=true`, agent
`deep-research-max-preview-04-2026` → poll `GET /v1beta/interactions/{id}` until
`completed` → save `deepresearch_report.md` + `deepresearch_raw.json` → parse the
`KEY PAPERS` lines → enrich → `stage2b_deepresearch.json` (+ `.bib`).

Deep Research is asynchronous and takes minutes (the API caps a task at 60
minutes; most finish under 20). Google lists "literature reviews" as a primary
use case for the agent.

## Why an API, not a browser

When this pathway was first built, "Google deep research" had no usable API and
was retired in favour of Scholar Labs. Google has since shipped the **Deep
Research Agent** on the Interactions API (`deep-research-preview-04-2026` and
`deep-research-max-preview-04-2026`, on Gemini 3.1 Pro), so the stage is now a
clean, scriptable API call. It is a *retrieval service* (like SearchAPI), not
part of the pipeline's own reasoning, so it is compatible with the agent-driven
run's no-Anthropic-API rule.

## API key

| Variable | Meaning |
|----------|---------|
| `GEMINI_API_KEY` | Google AI Studio / Gemini API key (get one at aistudio.google.com/apikey) |

Stored in `~/.lit-review-pipeline.env` (gitignored). If it is missing the stage
degrades gracefully.

## Cost

Pay-as-you-go per task (it is an agentic loop, not one request):

- **Deep Research** (`deep-research-preview-04-2026`): ~$1–3 per task.
- **Deep Research Max** (`deep-research-max-preview-04-2026`, the default here):
  ~$3–7 per task — more searches and synthesis, better for literature coverage.

## CLI

| Flag | Default | Description |
|------|---------|-------------|
| `--query-file PATH` | — | File holding the brief (the orchestrator passes the Undermind brief) |
| `--query TEXT` | — | Brief/research description for standalone use |
| `--research-question TEXT` | — | Fallback question if no query/file is given |
| `-o, --output PATH` | `stage2b_deepresearch.json` | Output JSON (a `.bib` sibling is written) |
| `--debug-dir PATH` | output dir | Where the report + raw response are saved |
| `--model ID` | `deep-research-max-preview-04-2026` | Deep Research agent id (use `deep-research-preview-04-2026` for the cheaper tier) |
| `--no-enrich` | off | Skip Crossref enrichment (dedup enriches anyway) |

```bash
# Driven by the orchestrator (the normal path)
python scripts/deepresearch_search.py --query-file undermind_brief.txt \
    -o stage2b_deepresearch.json --debug-dir debug_deepresearch

# Re-parse a saved report (no API call)
python scripts/deepresearch_ingest.py --report debug_deepresearch/deepresearch_report.md \
    --raw debug_deepresearch/deepresearch_raw.json -o stage2b_deepresearch.json
```

## Graceful degradation

If `GEMINI_API_KEY` is missing, the task fails or times out, or no papers can be
parsed, the driver prints `DEEPRESEARCH_DEFERRED`, writes an empty result list,
and exits 0. The orchestrator marks the stage **deferred** and the rest of the
pipeline still completes.

## Source extraction

The reliable channel is the `KEY PAPERS` section the prompt asks the agent to
emit, because the Interactions API does not support structured outputs. Each line
is `Title | Authors | Year | Venue | DOI or URL`. The ingest also scans the raw
response for `(title, url)` citations as a fallback when the section is thin.
Records carry `source: "deepresearch"`; missing DOIs/abstracts/journals are filled
at the dedup stage's enrichment cascade (OpenAlex → Crossref → S2).

## Output schema

Same as the other stages, with `source: "deepresearch"`:

```json
{"title": "...", "authors": "A, B", "year": "2024",
 "doi": "https://doi.org/10.x/y", "abstract": "", "journal": "...",
 "url": "...", "source": "deepresearch", "verified": false,
 "citations": 0, "open_access": false}
```

## Notes / limitations

- The Interactions API is in public beta; response schemas may change. The driver
  dumps `deepresearch_raw.json` so the ingest (and you) can adapt if the citation
  structure shifts; the `KEY PAPERS` text channel is schema-independent.
- `background=true` is required and implies `store=true`; both are set.
- Default tools are Google Search + URL Context + Code Execution.
