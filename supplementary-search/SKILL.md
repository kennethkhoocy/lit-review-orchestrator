---
name: supplementary-search
description: >
  Stage 4 of the lit review pipeline: search academic sources — general Google
  Scholar (--scholar), SSRN, NBER, HeinOnline, citation chaining via Semantic
  Scholar, and forthcoming paper lists from top finance journals.
  Long research prompts are automatically condensed into 3-5 short queries
  via Claude Sonnet before searching.
  Only use this skill when explicitly requested — e.g., the user says
  "run supplementary search", "supplementary-search", or
  "/supplementary-search". Do NOT auto-trigger on general literature
  review or paper search requests.
---

# Supplementary Search (Multi-Source)

Search additional academic sources for working papers, citation chains,
and forthcoming articles that the primary search engines may miss.

**Input**: a research query string, optionally seed DOIs for citation chaining
**Output**: `<stem>.ris` + `<stem>.json` (same schema as Undermind/Scholar Labs)

This is **Stage 4** of a lit review pipeline. Output can be merged with
Stages 1-2 (Undermind, Scholar Labs) for dedup and screening.

## Quick Start

```bash
# Search all sources
python scripts/supplementary_search.py "corporate governance regulation" -o results.json --all

# Just SSRN and NBER
python scripts/supplementary_search.py "voting rules" -o results.json --ssrn --nber

# Citation chaining from prior results
python scripts/supplementary_search.py "voting rules" -o results.json \
  --citation-chain --seeds-from undermind_results.json --top-seeds 20

# Forthcoming papers with caching
python scripts/supplementary_search.py "shareholder activism" -o results.json --forthcoming --cache
```

## Prerequisites

```bash
pip install requests beautifulsoup4 anthropic
```

## API Keys

- `SEARCHAPI_API_KEY` — required for SSRN, HeinOnline, and forthcoming journal searches
  (uses SearchAPI.io's Google Scholar engine). Get one at searchapi.io.
- `ANTHROPIC_API_KEY` — required for query condensation (uses Claude Sonnet).
  Already needed for other pipeline stages.

## Source Flags

| Flag | Source | Method |
|------|--------|--------|
| `--scholar` | Google Scholar (general) | Google Scholar via SearchAPI (no site filter) |
| `--ssrn` | SSRN | Google Scholar via SearchAPI (`site:ssrn.com`) |
| `--nber` | NBER working papers | NBER REST API |
| `--heinonline` | HeinOnline | Google Scholar via SearchAPI (`site:heinonline.org`) |
| `--ecgi` | ECGI working papers | HTML scrape via requests |
| `--citation-chain` | Semantic Scholar | REST API (free) |
| `--forthcoming` | JF, JFE, RFS | Google Scholar via SearchAPI (`source:"..."`) |
| `--all` | All of the above | — |

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `query` | (optional) | Research query text (optional if `--queries-file` is given) |
| `-o, --output` | `supplementary_results.json` | Output file path |
| `--ssrn` | off | Search SSRN |
| `--nber` | off | Search NBER |
| `--heinonline` | off | Search HeinOnline via Google Scholar |
| `--scholar` | off | Search Google Scholar directly (no site filter) |
| `--ecgi` | off | Search ECGI |
| `--citation-chain` | off | Citation traversal via Semantic Scholar |
| `--forthcoming` | off | Forthcoming lists from top journals |
| `--all` | off | Enable all sources |
| `--no-condense` | off | Skip query condensation; use raw query as-is |
| `--queries-file` | — | JSON array of query strings; overrides condensation and the positional query |
| `--seeds-from` | — | JSON file to extract seed DOIs from |
| `--top-seeds` | `10` | Number of top papers to use as seeds |
| `--hops` | `1` | Citation chain depth (1 = direct citations only) |
| `--cache` | off | Cache forthcoming lists (avoid re-scraping) |
| `--max-per-source` | `100` | Max papers per source |
| `--debug-dir` | `supplementary_debug` | Where to save debug output |

## Output Schema

Same as Undermind/Scholar Labs, with `source` tag:

```json
{
  "title": "Paper Title",
  "authors": "Author1, Author2",
  "year": "2024",
  "doi": "https://doi.org/10.1234/...",
  "abstract": "...",
  "journal": "Journal Name",
  "relevance": "",
  "score": "",
  "url": "https://...",
  "source": "ssrn"
}
```

## Query Condensation

Long research prompts (~2000 chars) produce poor Google Scholar results.
Before any SearchAPI call, the script uses Claude Sonnet to distill the
full prompt into 3-5 short (3-6 word) queries covering distinct literature
streams. Each condensed query is run per source, and results are deduplicated.

- Runs automatically unless `--no-condense` is passed
- `--no-condense` wraps the raw query in a single-element list (useful for
  short manual queries that don't need condensation)
- Citation chaining is unaffected (uses seed DOIs, not text queries)
- NBER also uses condensed queries (one API call per condensed query)

In the orchestrator's agent-driven flow the orchestrator passes `--queries-file`
with agent-written queries (a Sonnet subagent by default, Opus when `all_opus`),
bypassing `condense_query` entirely; the in-script Claude Sonnet condensation here is
the autonomous fallback for standalone raw-query runs.

## Important

- SSRN, HeinOnline, and forthcoming use SearchAPI's Google Scholar engine —
  requires `SEARCHAPI_API_KEY` env var. No Playwright or browser needed.
- Semantic Scholar API is free but rate-limited (100 req / 5 min) —
  the script adds polite delays automatically
- Forthcoming lists: use `--cache` to avoid re-querying within 24 hours
- Results are deduplicated by DOI and title before output

## Troubleshooting

**SSRN/Forthcoming returns no results**: Check that `SEARCHAPI_API_KEY`
is set. Inspect `supplementary_debug/searchapi_*.json` for raw responses.

**Semantic Scholar rate limit**: The script auto-throttles. If you
hit limits, reduce `--top-seeds` or `--hops`.
