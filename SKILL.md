---
name: lit-review-orchestrator
description: >
  Master controller for the lit-review pipeline, driven by a document. Give it a
  .tex or .docx file describing an article — a full manuscript, an abstract, or a
  proposal — and it extracts a search plan, runs Undermind (an automated
  Playwright driver in Classic mode) and Google Scholar (SearchAPI.io), then
  merges, deduplicates, and screens the results. Only use this skill when
  explicitly requested — e.g., the user says "run lit-review-orchestrator",
  "lit-review-orchestrator", or "/lit-review-orchestrator". Do NOT auto-trigger
  on general literature review requests.
---

# Lit-Review Orchestrator

Run the literature-review pipeline from a single command, starting from a
document that describes your article.

**Input**: a `.tex` or `.docx` document — a full manuscript, an abstract, or any
text describing the article's content.
**Output**: a deduplicated, relevance-screened master list (JSON + RIS), plus the
extracted search plan and all intermediate stage files.

## Quick Start

The `orchestrator.py` commands below are the **autonomous fallback** (reasoning on
the Sonnet/DeepSeek API). When an agent runs this skill interactively, use the
**agent-driven flow** instead; see *How it runs* below. That flow performs
non-browser reasoning at the agent layer with no Anthropic API key, using the
platform routing in `docs/claude-code.md` or `docs/codex.md`.

```bash
pip install -r requirements.txt                                  # one-time
cp lit-review-pipeline.env.example ~/.lit-review-pipeline.env    # then fill in keys

# From a full manuscript
python scripts/orchestrator.py paper.docx --output-dir ~/lit-reviews/mypaper

# From just an abstract (any .tex/.docx describing the article works)
python scripts/orchestrator.py abstract.tex --output-dir ~/lit-reviews/mypaper

# Add opt-in sources; DOI-only dedup
python scripts/orchestrator.py paper.tex --ssrn --nber --no-llm --output-dir out

# Escape hatch: run from a raw query string (skips Stage 0 extraction)
python scripts/orchestrator.py --query "dual-class shares cost of equity" --output-dir out
```

## Pipeline

| Stage | Name | Default | What it does |
|-------|------|---------|--------------|
| 0 | Extract | on | Parse the document; the agent derives the research question, an Undermind brief, a Scholar Labs question, and Google Scholar queries |
| 1 | Undermind | on | Automated Undermind.ai Classic deep search from the brief (Playwright; signs in with stored credentials) |
| 2 | Scholar Labs | opt-in | Google Scholar Labs deep search via `--scholarlabs` (Playwright; stored Google login). Off by default — Google rate-limits its Cite/BibTeX export under automation, so it often defers |
| 2b | Deep Research | on | Gemini Deep Research Agent (Interactions API; `GEMINI_API_KEY`) — alternative API-driven deep search |
| 4a | Google Scholar | on | SearchAPI.io Google Scholar, driven by the extracted queries |
| 4b | Supplementary | off | SSRN / NBER / HeinOnline / forthcoming (`--ssrn --nber --heinonline --forthcoming`) |
| 4c | Citation chain | off | Semantic Scholar (`--citation-chain`; needs DOI-bearing seeds) |
| 5 | Dedup | on | Merge all outputs; metadata enrichment + DOI and LLM fuzzy dedup |
| 5b | Verify | on | Cross-check every paper against OpenAlex / Crossref / Semantic Scholar and **drop** any none can confirm (anti-hallucination); if an index outage leaves >30% of papers uncheckable, it keeps everything and warns instead of dropping. `--no-verify` keeps all; dropped papers saved to `stage5_merged_unverified.json` |
| 6 | Screen | on | Abstract relevance screening against the research question |

Stage 0 runs first; Stages 1, 2b, and 4a run concurrently (Stage 2 Scholar Labs joins them only with `--scholarlabs`, and 4b when opted in); 4c follows them; then 5, then 6.

## How it runs: agent-driven (default) vs autonomous fallback

LLM work follows one routing rule: **the agent-driven flow uses no Anthropic API
for the pipeline's reasoning steps.** Stage 0 extraction, Stage 4 query
condensation, Stage 5 dedup judgments, Stage 6 screening, keyless web search, and
Undermind clarifying answers run at the agent layer. The exact model and
delegation policy depends on the host agent:

- Claude Code: read `docs/claude-code.md`.
- Codex: read `docs/codex.md`.

Undermind is a subprocess that owns the live browser, so its clarifying questions
come back to the agent through a small file handshake (`--answers-dir`): the driver
writes each question to a file and types whatever answer you drop back. The
Sonnet/DeepSeek API path remains the **autonomous fallback** (`orchestrator.py`)
for unattended runs with no agent present.

Because a Python subprocess cannot spawn subagents, each reasoning script exposes
an **emit/ingest seam**: the script does the deterministic work (parsing,
candidate-pair generation, validation, enrichment, merge, all file output) and
hands only the LLM step out to you in the middle. Each script also keeps its
in-script Sonnet/DeepSeek API path as an **autonomous fallback** for unattended
runs, so the same files support both interactive and unattended use.

### Platform routing (agent-driven flow)

The shared pipeline uses named reasoning roles. Map those roles to the host
platform before dispatching subagents or doing web-search work.

| Role / stage | Strong-reasoning route | Cost-conscious route |
|--------------|------------------------|----------------------|
| Orchestrator: coordinate the run, parse the GUI config, fan out, merge | Host platform's strongest interactive model | Parent session default |
| Stage 0: extract the search plan | Strong-reasoning route | Avoid downgrading unless the user requests a fast pass |
| Stage 4d: keyless web-search fan-out | Strong-reasoning route | Avoid downgrading; recall and precision matter |
| Stage 6: relevance re-ranking | Strong-reasoning route | Avoid downgrading; this determines final ranking |
| Stage 4a query writing, Stage 5 dedup judgments, Undermind clarifying answers | Strong-reasoning route when accuracy is prioritized | Cheaper platform worker model; escalate uncertain cases |

For Claude Code, the strong route is Opus 4.8 and the cheaper worker route is
Sonnet 4.6. For Codex, the strong keyless route is `gpt-5.5` with `xhigh`
reasoning, and lower-stakes batch judgments use cheaper Codex workers such as
`gpt-5.4-mini` at medium or high reasoning. See `docs/claude-code.md` and
`docs/codex.md` for the complete mapping.

The GUI still emits the legacy field `"all_opus"`. Interpret `"all_opus": true`
as the high-accuracy platform profile: Claude Code uses Opus for all delegated
reasoning, while Codex uses `gpt-5.5` with `xhigh` reasoning for all delegated
reasoning. When it is false or absent, follow the platform's default split.

### Interactive entry (GUI)

When the skill is triggered interactively, open the settings dialog first, let the
user choose the input and options, then run the agent-driven stages below honouring
what it returns:
```bash
python scripts/lit_review_gui.py --config-out OUT/gui_config.json   # blocks until Run/Cancel
```
The window has a **Browse** field for the document (or a raw-query box), an output
folder, the **search channels** as checkboxes — keyed (Undermind / Deep Research /
Google Scholar checked; Scholar Labs unchecked — opt-in) and keyless (Free index
search / Web search, both on) — **supplementary sources**
(SSRN checked by default, NBER, HeinOnline) plus citation chaining, **Processing**
(Deduplicate / Verify sources / Screen / DOI-only), and an **Advanced** group (Quick
mode, Max chars, and a legacy *Use Opus for all tasks* toggle that is off by
default. Leave it off to run the platform's default routing split; check it to
request the high-accuracy route for every delegated reasoning stage. On
**Run** it writes the settings to `--config-out` *and* echoes them to stdout
between `===LITREVIEW_CONFIG_BEGIN===` and `===LITREVIEW_CONFIG_END===` (exit 0);
**Cancel** or closing the window exits 2 — abort the run. Parse that JSON and map it
onto the stages: skip a channel set `false`, set `output_dir`, run Scholar Labs /
supplementary / citation and the keyless `freesearch` (Stage 4e) / `websearch`
(Stage 4d) channels when `true`, pass `--no-verify` to dedup when `verify` is
false and `--no-llm` when `no_llm` is true, skip dedup/screen when false, pass
`max_chars` to extraction, and, when `all_opus` is true, use the high-accuracy
platform profile instead of the default split (see *Platform routing*). The GUI runs
nothing itself and calls no API. Shape:
```json
{"document":"…","query":"","output_dir":"…",
 "channels":{"undermind":true,"deepresearch":true,"scholar":true,"scholarlabs":false,"freesearch":true,"websearch":true},
 "supplementary":{"ssrn":true,"nber":false,"heinonline":false},
 "citation_chain":false,"top_seeds":20,
 "dedup":true,"verify":true,"screen":true,"no_llm":false,"quick":false,"max_chars":30000,"all_opus":false}
```

### Agent-driven run (the default; you orchestrate)

Pick an output dir `OUT`. Run the deterministic stages as subprocesses and do the
reasoning stages with the host platform routing described above. In Codex, spawn
subagents only after explicit user authorization for parallel agent work. Substitute
`<doc>` and the extracted `<research_question>`.

**Stage 0: extract (strong-reasoning route):**
```bash
python scripts/extract_search_plan.py <doc> --emit-prompt OUT/extract_prompt.txt -o OUT/search_plan.json
# Read OUT/extract_prompt.txt, produce the plan JSON with the platform's strong-reasoning route, write OUT/plan.json.
python scripts/extract_search_plan.py <doc> --plan-file OUT/plan.json -o OUT/search_plan.json
```
The plan JSON must carry `extract_search_plan.py`'s `REQUIRED_KEYS`; `--plan-file`
validates them (exit 1 on a bad plan) and writes search_plan.json/.md,
scholar_queries.json, undermind_brief.txt, scholarlabs_query.txt.

**Stages 1 / 2b / 4a — search (subprocesses; run concurrently, background + Monitor). Stage 2 Scholar Labs is opt-in — run it only on request (see below):**
```bash
python undermind-search/scripts/undermind_search.py --brief-file OUT/undermind_brief.txt \
    -o OUT/stage1_undermind.json --debug-dir OUT/debug_undermind --answers-dir OUT/undermind_clarify
# Agent-in-the-loop, no API: while it runs, watch OUT/undermind_clarify for clarify_request_<n>.json.
# When one appears, answer the question with the platform worker route (strong route when all_opus is true), grounded in the
# brief and the Stage-0 undermind_clarifications, and write OUT/undermind_clarify/clarify_answer_<n>.json = {"answer": "..."}.
# Practical pattern: launch a background job that blocks until the request file exists (so you are
# notified), answer it, then re-arm for the next turn. Undermind is interactive in this mode.
# Opt-in only (Scholar Labs): Google rate-limits its Cite export under automation, so skip it by
# default and run this line only when asked / retrying from a fresh session:
python scholarlabs-search/scripts/scholarlabs_search.py --query-file OUT/scholarlabs_query.txt \
    --research-question "<research_question>" -o OUT/stage2_scholarlabs.json --hidden --debug-dir OUT/debug_scholarlabs
python deepresearch-search/scripts/deepresearch_search.py --query-file OUT/undermind_brief.txt \
    --research-question "<research_question>" -o OUT/stage2b_deepresearch.json --debug-dir OUT/debug_deepresearch  # Gemini Deep Research (GEMINI_API_KEY), pure subprocess
python supplementary-search/scripts/supplementary_search.py --scholar \
    --queries-file OUT/scholar_queries.json -o OUT/stage4a_scholar.json --debug-dir OUT/debug_scholar
```
Passing `--queries-file` (the agent-written queries, using the platform worker
route by default and the strong route when `all_opus` is true) bypasses the
in-script `condense_query` fallback in
supplementary-search. For a raw-query agent run (no document, hence no Stage 0 to
produce `scholar_queries.json`), first have the agent write that file (a short
JSON array of query strings) and pass it the same way, or add `--no-condense`.
Either route keeps the agent path free of the in-script API call.

**Web search (keyless agent-driven channel, and a useful add-on alongside
the keyed channels). Subagent fan-out, so the raw web text stays out of your
context.** This is the keyless search route in *Platform routing*. Emit a batched
task plan, dispatch one strong-reasoning subagent per batch when parallel agents
are authorized, then merge:
```bash
python websearch-search/scripts/websearch_ingest.py --emit-tasks \
    --queries-file OUT/scholar_queries.json --research-question "<research_question>" \
    --batch-size 3 -o OUT/websearch_tasks.json
# Dispatch one strong-reasoning worker per tasks[k]: hand it the system_prompt + its queries;
# each uses the platform's web-search/fetch tools over its queries and writes
# OUT/websearch_results_batch_<id>.json (only title required; never invent fields; do
# not fetch scholar.google.com). Then merge the partials:
python websearch-search/scripts/websearch_ingest.py \
    --results OUT/websearch_results_batch_*.json -o OUT/stage4d_websearch.json
```
This writes `stage4d_websearch.json` (`source="websearch"`), deduped by title with
best-effort keyless Crossref DOI fill, which the dedup `--inputs` glob below picks up.
The hits are real web results, so keep Stage 5b verification ON. For a few queries you
can skip the fan-out and ingest a single `websearch_results.json`. Empty input defers
(`WEBSEARCH_DEFERRED`). Full recipe: `websearch-search/SKILL.md`.

**Free index search (keyless; pairs with web search for the no-key fallback).** A
plain keyless subprocess that searches OpenAlex / Crossref / Semantic Scholar with
the Stage-0 queries:
```bash
python freesearch-search/scripts/freesearch_search.py \
    --queries-file OUT/scholar_queries.json -o OUT/stage4e_freesearch.json
```
This writes `stage4e_freesearch.json` (real index records, `source` set per index),
which the dedup `--inputs` glob below also picks up. No key needed; see
`freesearch-search/SKILL.md`.

**Stage 5: dedup (platform worker route by default; strong route when `all_opus`):**
```bash
python lit-dedup/scripts/lit_dedup.py --inputs OUT/stage[0-9]*.json --emit-pairs OUT/dedup_pairs.json -o OUT/stage5_merged.json
# Read OUT/dedup_pairs.json; for each pairs[k] = {i, j, a, b} decide if a and b are the
# same paper. Fan out across parallel platform workers for large pair sets when authorized. Write
# OUT/dedup_verdicts.json = [{"i":N,"j":N,"decision":"yes|no","confidence":"high|medium|low","rationale":"..."}].
python lit-dedup/scripts/lit_dedup.py --ingest-verdicts OUT/dedup_pairs.json OUT/dedup_verdicts.json -o OUT/stage5_merged.json
```
Exclude `stage5_*` / `stage6_*` from the `--inputs` glob. If `dedup_pairs.json` has
no pairs, write `[]` to the verdicts file and still run `--ingest-verdicts`.

**Stage 6: screen (the re-ranker; strong-reasoning route):**
```bash
python lit-screen/scripts/lit_screen.py --input OUT/stage5_merged.json --query "<research_question>" \
    --emit-tasks OUT/screen_tasks.json -o OUT/stage6_screened.json
# Read OUT/screen_tasks.json = {system_prompt, query, tasks:[{index, user_message}]}.
# Score each task following system_prompt. Fan out across parallel strong-reasoning workers in batches when authorized. Write
# OUT/screen_results.json = [{"index":N,"relevance_score":1-10,"rationale":"..","paper_type":"..","identification_strategy":"..","relationship":".."}].
python lit-screen/scripts/lit_screen.py --input OUT/stage5_merged.json --ingest-results OUT/screen_results.json -o OUT/stage6_screened.json
```

### Autonomous fallback (no agent)
```bash
python scripts/orchestrator.py <doc> --output-dir OUT
```
Runs every stage end-to-end as subprocesses; the reasoning stages call the
Anthropic API on Sonnet (DeepSeek for dedup). The keyless free index search (Stage
4e) runs here by default (`--no-freesearch` to skip); web search (Stage 4d) is
agent-only and not available in this runner. Use it for unattended runs or when
no agent is driving. It is the fallback, not the default.

### Undermind (Stage 1)

Undermind runs automatically from the extracted brief. The driver
(`undermind-search/scripts/undermind_search.py`) launches Playwright, signs in
with the credentials in `~/.lit-review-pipeline.env`, drives the Classic search
(sidebar **Classic** → **Search** → brief → the agent answers the clarifying
questions → **Generate Research Report**), waits for the report, and exports the
references (BibTeX by default). `undermind_ingest.py` then parses and enriches
them into `stage1_undermind.json` (+ `.bib`). First-time setup stores the
credentials with `python undermind-search/scripts/undermind_search.py --login`.

If credentials are missing or login/export fails, the stage degrades gracefully:
it prints `UNDERMIND_DEFERRED`, writes empty results, and the pipeline continues
on Google Scholar (the brief remains in `undermind_brief.txt`). When the Undermind
UI changes, re-confirm the locators by driving the live site with the windows-mcp
desktop tools, then update them in the driver (see `undermind-search/SKILL.md`).
The previous Playwright automation for older UIs is preserved under `_legacy/`.

### Scholar Labs (Stage 2)

Google Scholar Labs is **opt-in** (pass `--scholarlabs`; off by default because Google
rate-limits its Cite/BibTeX export under automation, so it frequently defers — Undermind
and Deep Research are the dependable deep-search channels). When enabled it runs from the
**Scholar Labs question** Stage 0
writes to `scholarlabs_query.txt` — a single detailed research question, which is
a different input from the Undermind brief. The driver
(`scholarlabs-search/scripts/scholarlabs_search.py`) drives Playwright: it signs
in to Google with a persistent profile (`~/.scholar-profile`), submits the
question, waits for the result cards, and reads each result's citation through the
standard Scholar **Cite → BibTeX** export; `scholarlabs_ingest.py` parses and
enriches them into `stage2_scholarlabs.json` (+ `.bib`). It returns ~10 highly
relevant papers that complement Undermind's broader set. First-time setup seeds
the Google session with
`python scholarlabs-search/scripts/scholarlabs_search.py --login` (sign in by
hand, including any 2FA).

**This stage runs off-screen, not headless.** Google serves headless/automated
Scholar an "unusual traffic" CAPTCHA, but a *real* headed Chrome passes — so the
orchestrator runs it with `--hidden`: a genuine headed window positioned far
off-screen, reusing the seeded session, so the stage works without anything
appearing in front of you. If the session lapses and a 2FA wall appears (which an
off-screen window can't clear), the stage defers with a desktop alert telling you
to re-run `--login` (visible) to re-authorize. A missing sign-in or CAPTCHA also
degrades gracefully (`SCHOLARLABS_DEFERRED`, empty results) and the pipeline
continues. Disable it with `--skip scholarlabs`. See `scholarlabs-search/SKILL.md`.

## Input parsing

`scripts/manuscript_parser.py` is bundled and ships with the skill, so a fresh
clone works with no dependency beyond `python-docx`:

- **`.docx`** — the body is walked in document order so paragraphs and tables are
  captured, and tracked-change text is included (technique adopted from the
  `word-docx` skill's `extract_text`). Footnotes and endnotes are read directly
  from the document XML so they are never lost.
- **`.tex`** — title, abstract, sections, and `\footnote{}` content are extracted;
  the bibliography and `\cite`/`\ref` keys are stripped so they do not pollute the
  topic profile. `\input`/`\include` targets are inlined.

Short inputs such as a bare abstract are handled: the text becomes a single block
and the extractor infers the framing.

## API keys

Stored in `~/.lit-review-pipeline.env` (auto-loaded; template in
`lit-review-pipeline.env.example`). Real environment variables take precedence.

| Variable | Needed for |
|----------|-----------|
| `ANTHROPIC_API_KEY` | Autonomous fallback only (`orchestrator.py`): Stage 0, dedup, screening, and Undermind clarifying answers. The agent-driven flow needs no Anthropic key. |
| `SEARCHAPI_API_KEY` | Google Scholar + SSRN/HeinOnline/forthcoming (required for search) |
| `GEMINI_API_KEY` | Stage 2b Gemini Deep Research (default-on alternative deep search) |
| `DEEPSEEK_API_KEY` | LLM fuzzy dedup + Crossref title matching (recommended) |
| `OPENALEX_API_KEY` | OpenAlex Premium for enrichment (optional) |
| `UNDERMIND_EMAIL` / `UNDERMIND_PASSWORD` | Undermind login (Stage 1 driver; set on first run via `--login`) |
| `SCHOLAR_EMAIL` / `SCHOLAR_PASSWORD` | Google login for Scholar Labs (Stage 2 driver; set on first run via `--login`) |

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `document` | — | Path to a `.tex`/`.docx` document (positional) |
| `--query` | — | Run from a raw query instead of a document (skips Stage 0) |
| `--output-dir` | `./lit-review-output` | Output directory |
| `--model` | `claude-sonnet-4-6` | Model for Stage 0 extraction |
| `--screen-model` | `claude-sonnet-4-6` | Model for Stage 6 screening |
| `--max-chars` | `30000` | Max document characters sent to the extractor |
| `--skip` | none | Skip default stages: `undermind`, `deepresearch`, `scholar`, `dedup`, `screen` |
| `--quick` | off | Fast run: SearchAPI Google Scholar only (skips the Undermind and Deep Research deep-search stages) |
| `--scholarlabs` | off | Opt in to the Google Scholar Labs deep search (Stage 2); off by default because Google rate-limits its Cite export under automation |
| `--ssrn --nber --heinonline --forthcoming` | off | Opt-in supplementary sources |
| `--citation-chain` | off | Opt-in Semantic Scholar citation chaining |
| `--no-llm` | off | DOI-only dedup (skip LLM pass) |
| `--verify / --no-verify` | on | Cross-check papers vs OpenAlex/Crossref/Semantic Scholar and drop those none can confirm (anti-hallucination); `--no-verify` keeps all (dropped papers saved to `stage5_merged_unverified.json`) |
| `--top-seeds` | `20` | Seeds for citation chaining |

## Output

```
{output-dir}/
├── search_plan.json / .md          # research question, Undermind brief, queries, themes
├── scholar_queries.json            # the Google Scholar query list
├── undermind_brief.txt             # the Undermind brief
├── scholarlabs_query.txt           # the Scholar Labs research question
├── stage1_undermind.json / .bib    # Undermind results (enriched)
├── stage2_scholarlabs.json / .bib  # Scholar Labs results (enriched)
├── stage2b_deepresearch.json / .bib # Gemini Deep Research results
├── stage4a_scholar.json / .ris
├── stage5_merged.json / .ris       # deduplicated, verified master list
├── stage5_merged_unverified.json   # papers dropped by verification (audit trail)
├── stage6_screened.json/.xlsx/.ris        # ALL screened papers, scored & ranked (no score filter)
├── stage6_screened.bib                     # screened papers (BibTeX), score >= 4 only
├── stage6_filtered.json/.xlsx              # score >= 4 subset (the shortlist)
├── dedup_log.json / dedup_report.md / verification.log
└── pipeline.log
```

## Skill structure

```
lit-review-orchestrator/
├── SKILL.md
├── AGENTS.md                         # Codex repository guidance
├── CLAUDE.md                         # Claude Code repository guidance
├── agents/openai.yaml                # Codex UI metadata and explicit invocation policy
├── docs/
│   ├── codex.md                      # Codex model routing and subagent rules
│   ├── claude-code.md                # Claude Code model routing and tool notes
│   └── images/                       # diagram sources and rendered images
├── requirements.txt
├── lit-review-pipeline.env.example
├── scripts/
│   ├── orchestrator.py            # this controller
│   ├── lit_review_gui.py          # interactive settings dialog (GUI front door)
│   ├── manuscript_parser.py       # bundled .docx/.tex parser
│   └── extract_search_plan.py     # Stage 0 extraction
├── undermind-search/              # Stage 1 (Playwright driver + ingest)
├── scholarlabs-search/            # Stage 2 (Playwright driver + ingest)
├── deepresearch-search/           # Stage 2b (Gemini Deep Research API)
├── supplementary-search/          # Google Scholar + opt-in sources
├── lit-dedup/                     # Stage 5
├── lit-screen/                    # Stage 6
├── examples/sample_manuscript.tex
└── _legacy/                       # previous Undermind Playwright automation
```

## Operational notes

- Run in the foreground; the dashboard streams stage status to the console and `pipeline.log`.
- Stages 5 (dedup) and 6 (screen) can be re-run standalone after editing any stage input file.
- Citation chaining needs DOI-bearing seeds, so it is most useful once Undermind enrichment is active.
- Stage 0 failure aborts the run (the pipeline cannot proceed without a search plan).
- The SSRN, HeinOnline, and forthcoming sources are Google Scholar searches with a
  `site:`/`source:` filter — subsets of Stage 4a — so they mainly force those venues
  to surface rather than adding a new index. NBER (`nber.org` API) and citation
  chaining (Semantic Scholar) hit independent indexes and add genuine coverage.
- `--quick` runs only the SearchAPI Google Scholar channel (no browser, no login),
  useful for a fast pass or when the Undermind/Scholar Labs logins are unavailable.
