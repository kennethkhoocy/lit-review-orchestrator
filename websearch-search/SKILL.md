# Web Search channel (Stage 4d) — keyless, agent-driven

A zero-dependency discovery channel for users who have **only Claude Code** and no
search accounts (no SearchAPI / Gemini / Undermind). It uses the agent's own
**WebSearch / WebFetch** tools to find real literature on the open web, then funnels
the hits through the same dedup -> verify -> screen pipeline as every other channel.

There is **no subprocess driver** here: a Python script cannot run WebSearch. The
agent (you, in Claude Code) does the searching; `scripts/websearch_ingest.py` only
normalizes what you gather into the pipeline schema.

## When to use

- The user has no `SEARCHAPI_API_KEY`, `GEMINI_API_KEY`, or Undermind login, OR
- You want a broad open-web sweep (working papers, very recent work, SSRN / arXiv /
  NBER / OpenReview / publisher pages) alongside the keyed channels.

Run it **in parallel** with whatever other channels are available; its output merges
with theirs at dedup.

## Recipe (agent-driven)

1. Build a query set from Stage 0: the `scholar_queries` in `search_plan.json` plus
   the `research_question`. Add a few author/concept variants if useful.
2. For each query, call **WebSearch**; for the most promising results, **WebFetch**
   the page (publisher / SSRN / arXiv / NBER / OpenAlex / Semantic Scholar) to read
   the real title, authors, year, venue, DOI, and abstract. Do NOT invent fields —
   leave anything you cannot read as `""`. Fan out across parallel subagents for
   many queries.
3. Collect the candidates into `OUT/websearch_results.json` as a JSON array:
   ```json
   [{"title": "...", "authors": "First Last, Second Author", "year": "2021",
     "journal": "...", "doi": "10.xxxx/...", "url": "https://...", "abstract": "..."}]
   ```
   Only `title` is required; fill what you actually found.
4. Normalize into a stage file:
   ```bash
   python websearch-search/scripts/websearch_ingest.py \
       --results OUT/websearch_results.json -o OUT/stage4d_websearch.json
   ```
   This writes `stage4d_websearch.json` (+ `.ris`) with `source="websearch"`,
   deduped by title, with best-effort keyless Crossref DOI fill (`--no-enrich` to
   skip). The `stage[0-9]*.json` dedup glob then picks it up automatically.

## Anti-hallucination

Web hits are real records, so fabrication is far lower than asking the model to
recall papers from memory. It is not zero — a snippet can carry a wrong year, or a
non-peer-reviewed page can slip in — so **keep Stage 5b verification ON**: it
confirms every paper against OpenAlex / Crossref / Semantic Scholar and drops
anything that cannot be confirmed. Never pair this channel with `--no-verify`.

## Notes

- Keyless: the only network call the script makes is the keyless Crossref polite
  pool (set `LITREVIEW_CONTACT_EMAIL` to use your own contact). No LLM API calls.
- Google Scholar itself is bot-blocked, so do not WebFetch `scholar.google.com`
  directly; rely on WebSearch results and on fetching the underlying source pages.
- Coverage depends on what surfaces in search; this is a strong keyless **baseline**,
  not a replacement for Undermind / Deep Research / the SearchAPI Google Scholar
  channel.
