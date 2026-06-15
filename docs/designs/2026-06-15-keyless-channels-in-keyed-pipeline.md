# Design: keyless channels as optional add-ons in the keyed pipeline (subagent-driven web search)

- **Date:** 2026-06-15
- **Status:** approved design, pre-implementation
- **Scope confirmed by user:** (1) Approach 1 — emit/fan-out/merge seam; (2) keyless channels additive whenever enabled; (3) wire free index (4e) into the autonomous orchestrator, keep web search (4d) agent-only.

## Problem

The two keyless channels are not symmetric in how they can be run. Free index search
(Stage 4e) is a plain subprocess, so any context — the main agent or a subagent — can
invoke it. Web search (Stage 4d) is written for the top-level Claude Code agent: the
recipe has "you, the agent, run WebSearch/WebFetch and write `websearch_results.json`."
Consequently web search cannot be folded cleanly into a normal keyed run the way the
subprocess channels (Undermind, Deep Research, Scholar) are, and when it does run it
executes inline in the main loop, which serialises the searches and loads large raw
result blobs into the orchestrator's context. Dedup and screening already avoid this by
fanning out to parallel Opus subagents; web search should do the same.

## Goal

A normal keyed run can optionally include the keyless channels as additive coverage, and
web search is executed by parallel Opus subagents rather than the main agent inline. The
heavy WebSearch/WebFetch text stays inside the subagent contexts; only distilled
candidate records return to the orchestrator. Web search becomes a peer of the other
channels: emit a plan, run it, collect an output file that merges at dedup.

## Non-goals

- Auto-fallback (running keyless channels only when keyed channels defer). Additive-
  when-enabled is the chosen behaviour; auto-fallback is a possible later enhancement.
- Running web search in a pure subprocess / the autonomous path. Web search needs agent
  tools, so it stays agent-driven only.
- Any change to dedup, verification, or screening logic. Stage 4d/4e outputs already
  merge through the `stage[0-9]*.json` glob.

## Design (Approach 1: emit → fan-out → merge → ingest)

Mirror the seams already in the codebase: `lit_screen` uses `--emit-tasks` /
`--ingest-results`, `lit_dedup` uses `--emit-pairs` / `--ingest-verdicts`. Web search
gets the same shape.

### New modes in `websearch-search/scripts/websearch_ingest.py`

1. **Emit tasks.**
   `python websearch_ingest.py --emit-tasks --queries-file OUT/scholar_queries.json
   --research-question "<rq>" --batch-size 3 -o OUT/websearch_tasks.json`
   Writes:
   ```json
   {
     "system_prompt": "<the websearch rules: fetch real pages, never invent fields, leave unknowns blank>",
     "research_question": "<rq>",
     "tasks": [ {"batch_id": 0, "queries": ["q1","q2","q3"]}, {"batch_id": 1, "queries": ["q4","q5","q6"]} ]
   }
   ```
   Batch count ≈ ceil(num_queries / batch_size). The research question is included so a
   subagent can add a couple of sensible variant searches.

2. **Merge + ingest (multiple partial files).** Extend the existing ingest so `--results`
   accepts more than one path (`nargs="+"`). The orchestrator concatenates the
   per-batch partial files, then the current logic runs unchanged: dedup by title, keyless
   Crossref DOI fill (`--no-enrich` still honoured), write `stage4d_websearch.json` (+ `.ris`).
   The single-file form (`--results one.json`) keeps working for backward compatibility and
   the inline fallback.

### Orchestration (main agent, when 4d enabled)

1. Run `--emit-tasks` to produce `websearch_tasks.json`.
2. Dispatch one Opus subagent per `task` in parallel. Each subagent reads its batch,
   runs WebSearch on its queries, WebFetches promising hits to read real metadata, and
   writes `OUT/websearch_results_batch_<id>.json` as a candidate array (schema below).
   Raw search/fetch output never leaves the subagent.
3. Merge the partial files via the ingest mode above → `stage4d_websearch.json`.

Downstream is untouched: the stage file joins the dedup `--inputs` glob exactly as today,
and Stage 5b verification stays ON for 4d (the candidates are real web hits, not zero —
but a snippet can still carry a wrong year, so verification confirms each one).

### Partial result schema (per subagent)
```json
[ {"title": "...", "authors": "First Last, Second Author", "year": "2021",
   "journal": "...", "doi": "10.xxxx/...", "url": "https://...", "abstract": "..."} ]
```
Only `title` is required; anything not read from a real page is left `""`.

### Integration into the keyed pipeline (agent-driven)

In SKILL.md's "Agent-driven run", the keyless channels are launched in the same parallel
batch as the keyed channels when their config flags are true: web search via the subagent
fan-out above, free index via its subprocess. All outputs land as `stageN_*.json` and
merge at dedup. The GUI checkboxes already exist and default on; this only documents the
execution pattern and wires the flags.

### Autonomous path (`scripts/orchestrator.py`)

- Add an optional **freesearch** search stage gated on a new `--freesearch` /
  `--no-freesearch` CLI flag (default on, matching the agent-path default). It runs
  `freesearch_search.py --queries-file <scholar_queries.json> -o stage4e_freesearch.json`.
  Add `freesearch` to the `search_stages` tuple so `_collect_dedup_inputs` picks up its
  output. freesearch already prints `FREESEARCH_DEFERRED`, writes `[]`, and exits 0 on
  failure, so graceful degradation holds.
- Web search has no autonomous form, so `orchestrator.py` gains no websearch stage.

The GUI's `channels.freesearch` / `channels.websearch` flags are consumed by the
agent-driven path, where the agent maps them onto the stages it launches; the autonomous
`orchestrator.py` uses its own CLI flags.

## Files changed

| File | Change |
|---|---|
| `websearch-search/scripts/websearch_ingest.py` | Add `--emit-tasks` mode; allow `--results` to merge multiple partial files; add `WEBSEARCH_DEFERRED` empty-output path. |
| `websearch-search/SKILL.md` | Replace the recipe with emit → subagent fan-out → merge → ingest; keep inline single-agent path as a documented fallback. |
| `SKILL.md` (main) | Add keyless channels to the agent-driven parallel search batch with the 4d fan-out recipe; note 4d is agent-only, autonomous gets 4e only. |
| `scripts/orchestrator.py` | Optional freesearch (4e) stage gated on a `--freesearch`/`--no-freesearch` CLI flag (default on); add to `search_stages`. No websearch stage (agent-only). |
| dedup / verify / screen | No change. |

## Edge cases and graceful degradation

- **No queries** (emit) or **no candidates** (merge): write empty `stage4d_websearch.json`,
  print `WEBSEARCH_DEFERRED`, exit 0; the pipeline continues on the other channels.
- **A subagent returns nothing / errors:** its partial file is empty or absent; the merge
  skips it. The run is not blocked by one failed batch.
- **Nesting limit:** the main agent dispatches the batch subagents (subagents do not spawn
  subagents). Same pattern used for screening.
- **Batch size:** default small (3) to bound each subagent's context and maximise
  parallelism; tunable via `--batch-size`.
- **Verification:** never pair 4d with `--no-verify`; keep Stage 5b on.

## Success criteria

1. `--emit-tasks` over Q queries with batch size B yields ceil(Q/B) batches in the task file.
2. Dispatching subagents over the batches and merging produces `stage4d_websearch.json`
   with deduped real candidates, and no raw web-result text appears in the main context.
3. In a keyed agent-driven run with the keyless boxes on, `stage4d` + `stage4e` + the keyed
   stage files all merge at dedup, and verification runs over the union.
4. `orchestrator.py` with freesearch enabled produces `stage4e` and includes it in dedup;
   with it disabled, no `stage4e` is produced; `websearch` enabled in autonomous mode logs
   a skip note rather than failing.
5. Backward compatibility: `websearch_ingest.py --results <single-file>` still works.
6. All degradation paths above exit 0 and leave the rest of the pipeline runnable.

## Verification plan

- Unit-level: `--emit-tasks` batch math; multi-file merge dedup; empty-input deferral.
- Integration: re-run the keyless dry run using the new seam (emit → 2–3 subagents →
  merge) and confirm parity with the hand-run result; run `orchestrator.py` on the example
  doc with `--freesearch` to confirm 4e enters dedup.
