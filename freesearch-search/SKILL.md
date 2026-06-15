# Free Index Search channel (Stage 4e) — keyless OpenAlex / Crossref / Semantic Scholar

A zero-key discovery channel: it runs the Stage-0 query list against the **free,
keyless keyword-search endpoints** of OpenAlex, Crossref, and Semantic Scholar and
normalizes the real records into the pipeline schema. Together with the agent
web-search channel (Stage 4d) it is the "only Claude Code" search fallback — no
search account or API key required.

Unlike web search, this is a plain subprocess (it calls keyless HTTP APIs), so it
runs the same way in any context.

## When to use

- The user has no `SEARCHAPI_API_KEY` / `GEMINI_API_KEY` / Undermind login — this is
  the keyless search backbone, OR
- You want extra grounded, clean-metadata coverage from the scholarly indexes
  alongside the other channels.

Run it in parallel with whatever else is available; its output merges at dedup.

## Recipe

Use the same Stage-0 query list Google Scholar uses:
```bash
python freesearch-search/scripts/freesearch_search.py \
    --queries-file OUT/scholar_queries.json -o OUT/stage4e_freesearch.json
```
Or a single query with `--query "..."`. Options: `--per-query N` (default 20) and
`--sources openalex,crossref,semanticscholar` (any subset). It writes
`stage4e_freesearch.json` (+ `.ris`) with `source` set per index, deduped across
indexes by DOI then title. The `stage[0-9]*.json` dedup glob picks it up
automatically.

## Keys

None required. OpenAlex and Crossref use the polite pool via a mailto (set
`LITREVIEW_CONTACT_EMAIL` to use your own contact). If present, `OPENALEX_API_KEY`
and `SEMANTIC_SCHOLAR_API_KEY` are used only to raise rate limits. It makes no LLM
API calls.

## Anti-hallucination

Every hit is a real index record, so there is nothing to fabricate — relevance, not
existence, is the only variable, and screening handles relevance. Stage 5b
verification still confirms each paper downstream. The keyless Semantic Scholar tier
is rate-limited, so it is queried with a short delay and skipped gracefully on a 429.
