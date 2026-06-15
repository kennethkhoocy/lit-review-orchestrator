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

## Recipe (agent-driven, subagent fan-out)

This is the default. The orchestrator emits a batched task plan, fans the batches
out across parallel Opus subagents — so the raw WebSearch/WebFetch text stays inside
the subagent contexts — and then merges the distilled candidates. It runs the same
way whether web search is the sole channel (no keys) or an add-on alongside the keyed
channels.

1. **Emit the task plan** from the Stage-0 queries:
   ```bash
   python websearch-search/scripts/websearch_ingest.py --emit-tasks \
       --queries-file OUT/scholar_queries.json --research-question "<rq>" \
       --batch-size 3 -o OUT/websearch_tasks.json
   ```
   This writes `{system_prompt, research_question, tasks:[{batch_id, queries:[...]}]}`.
   With no queries it prints `WEBSEARCH_DEFERRED` and writes an empty plan.
2. **Fan out across Opus subagents** — one per `tasks[k]`. Hand each subagent the
   `system_prompt`, the `research_question`, and its `queries`, and have it run
   **WebSearch** on each query, **WebFetch** the most promising hits (publisher /
   SSRN / arXiv / NBER / OpenAlex / Semantic Scholar) to read the real title,
   authors, year, venue, DOI, and abstract — never inventing a field, leaving
   unknowns `""` — and **Write** its candidates to
   `OUT/websearch_results_batch_<id>.json`:
   ```json
   [{"title": "...", "authors": "First Last, Second Author", "year": "2021",
     "journal": "...", "doi": "10.xxxx/...", "url": "https://...", "abstract": "..."}]
   ```
   Only `title` is required. Do NOT WebFetch `scholar.google.com` (bot-blocked).
3. **Merge** the partial files into the stage output:
   ```bash
   python websearch-search/scripts/websearch_ingest.py \
       --results OUT/websearch_results_batch_*.json -o OUT/stage4d_websearch.json
   ```
   This dedups by title across all batches, does best-effort keyless Crossref DOI
   fill (`--no-enrich` to skip), and writes `stage4d_websearch.json` (+ `.ris`,
   `source="websearch"`). The `stage[0-9]*.json` dedup glob then picks it up. If no
   batch yielded a usable candidate it prints `WEBSEARCH_DEFERRED` and writes an
   empty file, so the pipeline continues on the other channels.

### Inline fallback (a handful of queries)

For a small query set you can skip the fan-out: run WebSearch/WebFetch yourself,
collect everything into one `OUT/websearch_results.json`, and merge the single file
(`--results` accepts one or many):
```bash
python websearch-search/scripts/websearch_ingest.py \
    --results OUT/websearch_results.json -o OUT/stage4d_websearch.json
```

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
