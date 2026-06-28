# lit-review-orchestrator

> Turns a document that describes your article — a full manuscript, an abstract, or a
> proposal — into a deduplicated, relevance-ranked bibliography in which every entry has
> been confirmed to exist in a scholarly index. One command runs the whole pipeline:
> extract a search plan from the document, search several deep-research engines at once,
> merge and deduplicate, drop anything that cannot be verified, and screen what remains
> for relevance.

A literature search built on language models has one failure mode that ordinary
search engines do not: the model can return a reference that reads perfectly —
plausible authors, a plausible title, a plausible year, even a syntactically valid
DOI — for a paper that was never written. Deep-research agents that summarise the
literature in prose are especially prone to this, because the citation is generated
rather than retrieved. A bibliography that contains three fabricated entries among
two hundred real ones is worse than no bibliography, since the fabrications are
indistinguishable from the rest until a reader tries to follow one.

`lit-review-orchestrator` is built around removing that failure mode. It draws on
several search channels for breadth, then puts every candidate through a
verification pass (Stage 5b) that confirms the paper exists in OpenAlex, Crossref,
or Semantic Scholar before it is allowed into the final list. A reference that no
index can confirm is dropped and recorded in a separate audit file rather than
silently kept. The DOI check is guarded against the subtle case of a fabricated
title carrying a real-but-unrelated DOI, and the whole pass is designed to fail
safe when an index is merely unreachable rather than the paper being absent.

The pipeline is driven by a document rather than a bare query. Given a `.tex` or
`.docx` file, it reads the text and derives the research question, an Undermind
brief, a Scholar Labs question, and a set of Google Scholar queries, so the search
reflects what the article is actually about instead of a hand-typed keyword string.
Several deep-search channels then run concurrently and their results are fused into
one master list. Every reasoning step in the default mode runs at the agent layer
with no Anthropic API key required. Claude Code and Codex use the same emit/ingest
workflow with different model maps: Claude Code routes the strongest stages to
Opus 4.8, while Codex routes the keyless high-reasoning stages to `gpt-5.5` with
`xhigh` reasoning. An autonomous fallback on the Sonnet/DeepSeek API exists for
unattended runs.

## How it differs from other literature-review tools

- **Document-driven search planning.** The search plan is extracted from your
  manuscript or abstract, so you do not have to translate your article into search terms;
  Stage 0 produces the research question, channel-specific briefs, and a query list from
  the text itself.
- **Several deep-search engines fused into one list.** Undermind, Gemini Deep
  Research, Google Scholar (SearchAPI), and the opt-in Google Scholar Labs each
  contribute candidates that are merged, enriched, and deduplicated, so coverage does
  not depend on any single engine's recall.
- **Verification by default.** Stage 5b cross-checks every candidate against three
  scholarly indexes and drops what none can confirm. This is on unless you explicitly
  pass `--no-verify`.
- **Agent-layer reasoning with no Anthropic API.** The LLM steps (plan extraction,
  dedup judgments, relevance screening, keyless web search, and Undermind's
  clarifying answers) are handed to the agent through an emit/ingest seam. Claude
  Code uses the routing in `docs/claude-code.md`; Codex uses the routing in
  `docs/codex.md`, including `gpt-5.5` with `xhigh` reasoning for the keyless
  high-reasoning path. The API path survives as an unattended fallback.
- **Ranked, screened output.** What survives verification is scored for relevance
  against the research question and delivered as a ranked spreadsheet alongside RIS
  and BibTeX.

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using the skill](#using-the-skill)
  - [Interactive entry: the GUI](#interactive-entry-the-gui)
  - [The agent-driven run](#the-agent-driven-run)
  - [The autonomous fallback](#the-autonomous-fallback)
- [Architecture](#architecture)
  - [Pipeline overview](#pipeline-overview)
  - [The agent-driven seam](#the-agent-driven-seam)
  - [Verification (Stage 5b)](#verification-stage-5b)
  - [GUI entry handshake](#gui-entry-handshake)
- [Pipeline stages](#pipeline-stages)
- [The verification guarantee in detail](#the-verification-guarantee-in-detail)
- [Search channels](#search-channels)
- [CLI flags](#cli-flags)
- [Output](#output)
- [Skill structure](#skill-structure)

---

## Requirements

Python 3.10+ and the packages in `requirements.txt` (`pip install -r requirements.txt`,
then `playwright install chromium` for the Undermind and Scholar Labs browser drivers).

API keys live in `~/.lit-review-pipeline.env` (auto-loaded via python-dotenv; copy
`lit-review-pipeline.env.example`), and a real shell environment variable always wins over
the file. The default **agent-driven** flow runs the reasoning stages at the agent layer
inside the conversation, so **no Anthropic key is needed**. Claude Code model routing is
documented in `docs/claude-code.md`; Codex model routing is documented in `docs/codex.md`.
Verification and enrichment draw on the free OpenAlex / Crossref / Semantic Scholar pools.

### Search — recommended, but no key is strictly required

In the agent-driven flow you can run with **zero search accounts**: two keyless fallback
channels (agent **web search**, using the host platform's web-search or browser tools, and
**free index search** over OpenAlex / Crossref / Semantic Scholar) find real papers with
no key, and Stage 5b verifies them. The keyed channels below are recommended for broader,
higher-quality coverage; add whichever you have.

| Variable | Search channel it unlocks |
|----------|---------------------------|
| _(none)_ | **Web search (Stage 4d)** + **free index search (Stage 4e)** — keyless agent-driven fallback |
| `SEARCHAPI_API_KEY` | Google Scholar (Stage 4a) — the default keyed channel and the only one in `--quick`; also the engine behind SSRN / HeinOnline / forthcoming |
| `GEMINI_API_KEY` | Gemini Deep Research (Stage 2b) — default-on |
| `UNDERMIND_EMAIL` / `UNDERMIND_PASSWORD` | Undermind deep search (Stage 1) — default-on; captured on first run via `--login` |

### Optional — extras and the unattended fallback

Everything runs without these; each adds source coverage, metadata quality, or the no-agent mode.

| Variable | What it adds |
|----------|--------------|
| `DEEPSEEK_API_KEY` | Fuzzy title-matching when resolving DOIs during enrichment (any path), plus the LLM dedup pass in the autonomous fallback. Without it, enrichment falls back to exact title matches. |
| `OPENALEX_API_KEY` | OpenAlex Premium enrichment (higher rate limits) |
| `SCHOLAR_EMAIL` / `SCHOLAR_PASSWORD` | Google Scholar Labs deep search (Stage 2, opt-in via `--scholarlabs`); captured on first run via `--login` |
| `ANTHROPIC_API_KEY` | **Autonomous fallback only** (`orchestrator.py` run unattended): Stage 0, dedup, screening, and Undermind clarifying answers. The agent-driven default needs no Anthropic key. |

---

## Quick start

### Claude Code skill

Clone the repo into your Claude Code skills directory:

```bash
git clone https://github.com/kennethkhoocy/lit-review-orchestrator.git ~/.claude/skills/lit-review-orchestrator
pip install -r ~/.claude/skills/lit-review-orchestrator/requirements.txt
cp ~/.claude/skills/lit-review-orchestrator/lit-review-pipeline.env.example ~/.lit-review-pipeline.env
```
Then ask Claude Code for a literature review (or hand it a manuscript): the skill triggers,
the settings dialog opens, and the agent-driven pipeline runs from your choices.

### Codex skill

Clone the repo into your Codex skills directory:

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/kennethkhoocy/lit-review-orchestrator.git ~/.agents/skills/lit-review-orchestrator
pip install -r ~/.agents/skills/lit-review-orchestrator/requirements.txt
cp ~/.agents/skills/lit-review-orchestrator/lit-review-pipeline.env.example ~/.lit-review-pipeline.env
```
Restart Codex or start a new session, then invoke the skill explicitly:

```text
$lit-review-orchestrator
```

Codex reads `AGENTS.md` as repository guidance. Claude Code reads `CLAUDE.md`.
The shared pipeline behavior lives in `SKILL.md`; platform routing lives in
`docs/codex.md` and `docs/claude-code.md`.

### Command-line fallback

Clone it anywhere, install the dependencies, and call
the orchestrator directly:

```bash
git clone https://github.com/kennethkhoocy/lit-review-orchestrator.git
cd lit-review-orchestrator
pip install -r requirements.txt                                  # one-time
cp lit-review-pipeline.env.example ~/.lit-review-pipeline.env    # then fill in keys
```

The commands below are the **autonomous fallback** — they run every stage end-to-end as
subprocesses with the reasoning steps on the Sonnet/DeepSeek API:

```bash
# From a full manuscript
python scripts/orchestrator.py paper.docx --output-dir ~/lit-reviews/mypaper

# From just an abstract (any .tex/.docx describing the article works)
python scripts/orchestrator.py abstract.tex --output-dir ~/lit-reviews/mypaper

# Add opt-in sources; DOI-only dedup
python scripts/orchestrator.py paper.tex --ssrn --nber --no-llm --output-dir out

# Escape hatch: run from a raw query string (skips Stage 0 extraction)
python scripts/orchestrator.py --query "dual-class shares cost of equity" --output-dir out
```

---

## Using the skill

### Interactive entry: the GUI

When the skill is triggered interactively, a Tkinter settings dialog opens first and
hands your choices back to the agent session. It runs nothing itself and calls no API; it
is a settings collector.

```bash
python scripts/lit_review_gui.py --config-out OUT/gui_config.json   # blocks until Run/Cancel
```

The dialog has a **Browse** field for the document, an output folder that defaults to
the document's own directory, an optional raw-query box, the **search channels** as
checkboxes — keyed (Undermind, Deep Research, and Google Scholar checked; Scholar Labs
unchecked, since it is opt-in) and keyless (free index search and web search, both on) —
**supplementary sources** (SSRN on by default, NBER and HeinOnline off) with citation
chaining, a **Processing** group (Deduplicate, Verify sources, Screen, DOI-only), and an
**Advanced** group (Quick mode, Max chars, and a legacy *Use Opus for all tasks* toggle,
off by default). On **Run** it writes the chosen configuration to `--config-out` and
echoes it to stdout between `===LITREVIEW_CONFIG_BEGIN===` and `===LITREVIEW_CONFIG_END===`, exiting 0;
**Cancel** or closing the window exits 2 and the run is aborted. The configuration is JSON:

```json
{"document":"…","query":"","output_dir":"…",
 "channels":{"undermind":true,"deepresearch":true,"scholar":true,"scholarlabs":false,"freesearch":true,"websearch":true},
 "supplementary":{"ssrn":true,"nber":false,"heinonline":false},
 "citation_chain":false,"top_seeds":20,
 "dedup":true,"verify":true,"screen":true,"no_llm":false,"quick":false,"max_chars":30000,"all_opus":false}
```

The agent parses that JSON and maps it onto the stages: a channel set `false` is skipped,
`output_dir` is honoured, Scholar Labs / supplementary / citation run when `true`,
`--no-verify` is passed to dedup when `verify` is false, `--no-llm` when `no_llm` is
true, dedup and screen are skipped when false, `max_chars` is passed to extraction, and
when `all_opus` is true the high-accuracy platform profile is used for delegated
reasoning (see *The agent-driven run* below).

### The agent-driven run

This is the default. Every deterministic stage runs as a subprocess, and the reasoning
stages (plan extraction, dedup judgments, relevance screening, the keyless web search, and
Undermind's clarifying answers) are performed by the agent — the orchestrator and the
subagents it spawns — with no Anthropic API key. Claude Code uses the routing in
`docs/claude-code.md`; Codex uses the routing in `docs/codex.md`, where the keyless
high-reasoning route uses `gpt-5.5` with `xhigh` reasoning and lower-stakes batch
judgments use cheaper Codex workers. In Codex, spawn subagents only when the user
explicitly authorizes parallel agent work. Checking *Use Opus for all tasks* in the GUI
(`"all_opus": true`) requests the high-accuracy profile for the active platform. A
Python subprocess cannot spawn subagents, so each reasoning script exposes an
**emit/ingest seam**: the script does the deterministic work (parsing, candidate-pair
generation, validation, enrichment, merge, and all file output) and hands only the LLM step
out to the agent in the middle, reading the agent's answer back to finish. The full
stage-by-stage commands are in [`SKILL.md`](SKILL.md).

### The autonomous fallback

```bash
python scripts/orchestrator.py <doc> --output-dir OUT
```

Runs every stage end-to-end as subprocesses, with the reasoning stages calling the
Anthropic API on Sonnet (DeepSeek for dedup). Use it for unattended runs or when no
agent is driving. It is the fallback, retained so nothing is lost when no agent is
present.

---

## Architecture

*The diagrams below are hosted PNGs, rendered from the Excalidraw sources in
[`docs/images/`](docs/images). Node fill colour encodes role: **blue** = input or
agent reasoning, **gray** = deterministic (non-LLM) stages and drivers, **yellow** =
artefacts (prompt/result files, the dropped-papers audit), **purple** = data (papers),
**green** = terminal output, and a **peach diamond** = a decision. A dashed border marks
an opt-in source, a fallback path, or an abort.*

### Pipeline overview

![Pipeline overview: a document or raw query feeds Stage 0 extraction on the host platform's strong-reasoning route, which fans out to four concurrent search channels — Undermind, Gemini Deep Research, Google Scholar (SearchAPI), and the opt-in Scholar Labs — while a keyless group (web search, Stage 4d, and free index search, Stage 4e) and the opt-in supplementary sources and citation chaining also feed the merge; Stage 5 merges and deduplicates, Stage 5b verifies and drops unconfirmed papers to an audit file, Stage 6 screens for relevance, and the result is a ranked master list in xlsx, ris, and bib.](https://kennethkhoocy.github.io/files/lit-review-orchestrator/pipeline-overview.png?v=368de28b)

Stage 0 reads the document and derives the search plan. Stages 1, 2b, and 4a (Undermind,
Deep Research, and Google Scholar) run concurrently; Scholar Labs joins them only when
opted in, and supplementary sources and citation chaining feed the merge when enabled.
The keyless channels — web search (Stage 4d) and free index search (Stage 4e) — feed the
merge as well and are the no-key fallback that needs no search account. Stage 5 fuses
everything and deduplicates by DOI and by an LLM fuzzy pass. Stage 5b
verifies what survives, dropping any paper no index can confirm into
`stage5_merged_unverified.json`. Stage 6 scores the remainder for relevance, and the
ranked list is written in three formats.

### The agent-driven seam

![The agent-driven emit/ingest seam: a deterministic stage script parses input and generates a tasks file, emits it as a prompt/tasks JSON artefact, the LLM step is performed by the routed platform model with zero Anthropic API, the result is written to a verdicts/results JSON artefact, and the same script resumes to validate and merge into the stage output; a dashed autonomous fallback shows that without an agent, the Sonnet/DeepSeek API performs the same LLM step in-process.](https://kennethkhoocy.github.io/files/lit-review-orchestrator/agent-driven-seam.png?v=35d20ea3)

Each reasoning stage is split at the point where judgment is needed. The script handles
everything deterministic and writes a tasks file (`extract_prompt.txt`,
`dedup_pairs.json`, `screen_tasks.json`); the agent reads it, performs the reasoning with
the platform route documented in `docs/claude-code.md` or `docs/codex.md`, writes the
results back (`plan.json`, `dedup_verdicts.json`, `screen_results.json`); the script
then ingests the results and produces the stage output. Because the seam is a pair of
files, the same scripts support the autonomous fallback unchanged: when no agent is
present, the in-script Sonnet/DeepSeek path fills the seam instead.

### Verification (Stage 5b)

![Stage 5b verification flow: each paper with its title, authors, year, and optional DOI enters a decision on whether it has a DOI; with a DOI it takes the DOI path (resolve the DOI and apply a title-guard that catches a fabricated title carrying a real-but-unrelated DOI), without one it takes the title path (search OpenAlex, then Crossref, then Semantic Scholar for a corroborated match); both feed a decision on whether any index confirms a match, with three outcomes — a match is verified and kept in stage5_merged.json, an index that responded with no match means the paper is unverified and dropped to stage5_merged_unverified.json, and no index responding is treated as an error and the paper is kept (outage-safe); a degraded-run guard keeps all papers if none verify or more than thirty percent cannot be checked.](https://kennethkhoocy.github.io/files/lit-review-orchestrator/verification.png)

Verification runs over every paper before DOI deduplication, so it covers both the
autonomous and the agent-driven paths. A paper with a DOI is confirmed by resolving the
DOI **and** checking that the resolved record's title matches the paper, which is what
catches a fabricated title that happens to carry a real DOI. A paper without a DOI is
confirmed only by a corroborated title match — a near-exact title, or a strong title
backed by author-surname overlap, since a common title and a plausible year alone are
not enough. The three outcomes are kept distinct so that an index outage is never
mistaken for a missing paper: a paper an index actively reports as absent is dropped,
while a paper no index could answer for is tagged and kept. The degraded-run guard is a
final safety net — if nothing verifies, or more than thirty percent of papers could not
be checked, the run keeps everything rather than emptying the list.

### GUI entry handshake

![GUI entry handshake: the user triggers the skill, the Tkinter settings dialog (lit_review_gui.py) opens and collects the document, output directory, channels, supplementary sources, and the verify/screen/dedup options; a Run-or-Cancel decision either aborts on cancel or close (exit 2) or, on Run (exit 0), emits a config JSON via stdout sentinels and a config-out file; the host agent then parses the config and drives the agent-driven pipeline.](https://kennethkhoocy.github.io/files/lit-review-orchestrator/gui-entry.png)

The GUI is a front door that collects settings and returns them to the session. On Run
it emits the configuration both to a file and to stdout between sentinel markers, exiting
0; on Cancel or close it exits 2 and the run is aborted. The host agent reads the JSON
and drives the agent-driven stages accordingly. The dialog itself performs no search and
calls no API.

---

## Pipeline stages

| Stage | Name | Default | What it does |
|-------|------|---------|--------------|
| 0 | Extract | on | Parse the document; the agent derives the research question, an Undermind brief, a Scholar Labs question, and Google Scholar queries |
| 1 | Undermind | on | Automated Undermind.ai Classic deep search from the brief (Playwright; signs in with stored credentials) |
| 2 | Scholar Labs | opt-in | Google Scholar Labs deep search via `--scholarlabs` (Playwright; stored Google login). Off by default — Google rate-limits its Cite/BibTeX export under automation |
| 2b | Deep Research | on | Gemini Deep Research Agent (Interactions API; `GEMINI_API_KEY`) — an API-driven deep search |
| 4a | Google Scholar | on | SearchAPI.io Google Scholar, driven by the extracted queries |
| 4b | Supplementary | off | SSRN / NBER / HeinOnline / forthcoming (`--ssrn --nber --heinonline --forthcoming`) |
| 4c | Citation chain | off | Semantic Scholar (`--citation-chain`; needs DOI-bearing seeds) |
| 5 | Dedup | on | Merge all outputs; metadata enrichment plus DOI and LLM fuzzy deduplication |
| 5b | Verify | on | Cross-check every paper against OpenAlex / Crossref / Semantic Scholar and **drop** any none can confirm; if an index outage leaves >30% of papers uncheckable, it keeps everything and warns instead of dropping. `--no-verify` keeps all; dropped papers are saved to `stage5_merged_unverified.json` |
| 6 | Screen | on | Abstract relevance screening against the research question, with a 1–10 score and structured fields |

Stage 0 runs first; Stages 1, 2b, and 4a run concurrently (Scholar Labs joins them only
with `--scholarlabs`, supplementary only when opted in); 4c follows; then 5, 5b, and 6.

---

## The verification guarantee in detail

Stage 5b lives in `lit-dedup/scripts/lit_dedup.py` and is gated by `--verify/--no-verify`
(default on; the orchestrator threads the flag through). Its contract:

- **A paper is kept only if an index confirms it.** Confirmation is either a DOI that
  resolves to a record whose title matches the paper, or a corroborated title-search
  match in OpenAlex, Crossref, or Semantic Scholar.
- **The DOI path is title-guarded.** Resolving a DOI is not sufficient on its own; the
  resolved record's title must plausibly match the paper. A fabricated title that
  carries a real-but-unrelated DOI therefore fails verification.
- **Title corroboration is strict.** A title match requires high string similarity, or a
  strong-but-not-exact title together with author-surname overlap. A matching year on its
  own does not corroborate.
- **Outage is distinguished from absence.** A paper that an index answers for and reports
  as absent is dropped (recorded in `stage5_merged_unverified.json`). A paper that no
  index could answer for — every request errored — is tagged `_verify_error` and kept,
  so a network or index outage never quietly deletes real work.
- **Only an index outage keeps everything.** When more than thirty percent of papers could
  not be checked at all, the pass treats the run as a likely index outage, keeps the full
  set, and warns instead of dropping. Where the indexes respond and confirm nothing, those
  papers are dropped like any other unconfirmed result.
- **The audit trail is preserved.** Every dropped paper is written to
  `stage5_merged_unverified.json`, and the decisions are logged to `verification.log`.

Both the verification pass and the GUI were reviewed with an independent Codex audit, and
the findings — the DOI title-guard, the tightened title corroboration, the
outage-versus-absence distinction, and the field carry-forward — were fixed and tested.

---

## Search channels

- **Undermind (Stage 1)** drives the Undermind.ai Classic deep search through Playwright,
  signing in with stored credentials, answering the clarifying questions through the
  platform worker route and a file handshake, generating the report, and exporting the
  references. If login or
  export fails it degrades gracefully (`UNDERMIND_DEFERRED`, empty results) and the
  pipeline continues.
- **Deep Research (Stage 2b)** is the Gemini Deep Research Agent via the Interactions API,
  a pure subprocess that needs only `GEMINI_API_KEY`. It is the dependable API-driven deep
  channel and is on by default.
- **Google Scholar (Stage 4a)** is SearchAPI.io Google Scholar driven by the extracted
  queries; passing your own queries bypasses the in-script condense step.
- **Scholar Labs (Stage 2)** is opt-in (`--scholarlabs`). It runs a real headed Chrome
  positioned off-screen, because Google serves automated/headless Scholar an "unusual
  traffic" CAPTCHA that a genuine headed window passes. Google rate-limits its Cite/BibTeX
  export under automation, so it frequently defers, which is why it is off by default.
- **Supplementary (Stage 4b)** — SSRN, HeinOnline, and forthcoming are Google Scholar
  searches with a `site:`/`source:` filter, so they mainly force those venues to surface;
  NBER (the `nber.org` API) and citation chaining (Semantic Scholar) hit independent
  indexes and add genuine coverage.
- **Web search (Stage 4d, keyless)** discovers papers on the open web as a keyless
  agent-driven fallback and as an add-on alongside the keyed channels.
  `websearch_ingest.py --emit-tasks` writes a batched task plan; platform workers use the
  host web-search or browser tools to write partial candidate files, and
  `websearch_ingest.py --results` merges and dedups them into the pipeline
  (`source="websearch"`). It needs no search account or API key. Keep verification on,
  since web hits still need confirming. For a few queries you can skip the fan-out and
  ingest a single results file. See `websearch-search/SKILL.md`.
- **Free index search (Stage 4e, keyless)** runs the extracted queries against the free
  OpenAlex / Crossref / Semantic Scholar keyword-search endpoints and normalizes the real
  records into the pipeline. No key needed (optional keys only raise rate limits). It is the
  keyless backbone of the agent-driven no-account mode and pairs with web search; unlike
  web search, it also runs in the autonomous orchestrator (on by default; `--no-freesearch`
  to skip). See `freesearch-search/SKILL.md`.

---

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `document` | — | Path to a `.tex`/`.docx` document (positional) |
| `--query` | — | Run from a raw query instead of a document (skips Stage 0) |
| `--output-dir` | `./lit-review-output` | Output directory |
| `--max-chars` | `30000` | Max document characters sent to the extractor |
| `--skip` | none | Skip default stages: `undermind`, `deepresearch`, `scholar`, `dedup`, `screen` |
| `--quick` | off | Fast run: SearchAPI Google Scholar only (skips the deep-search stages) |
| `--scholarlabs` | off | Opt in to the Google Scholar Labs deep search (Stage 2) |
| `--ssrn --nber --heinonline --forthcoming` | off | Opt-in supplementary sources |
| `--citation-chain` | off | Opt-in Semantic Scholar citation chaining |
| `--freesearch / --no-freesearch` | on | Keyless free-index search (Stage 4e); web search (Stage 4d) is agent-only, not in this runner |
| `--no-llm` | off | DOI-only dedup (skip the LLM pass) |
| `--verify / --no-verify` | on | Cross-check papers and drop those none can confirm; `--no-verify` keeps all |
| `--top-seeds` | `20` | Seeds for citation chaining |

---

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

---

## Skill structure

```
lit-review-orchestrator/
├── SKILL.md                        # canonical invocation reference
├── AGENTS.md                       # Codex repository guidance
├── CLAUDE.md                       # Claude Code repository guidance
├── README.md                       # this document
├── agents/openai.yaml              # Codex UI metadata and explicit invocation policy
├── docs/
│   ├── codex.md                    # Codex model routing and subagent rules
│   ├── claude-code.md              # Claude Code model routing and tool notes
│   └── images/                     # Excalidraw diagram sources (.png + .excalidraw.json)
├── requirements.txt
├── lit-review-pipeline.env.example
├── scripts/
│   ├── orchestrator.py             # the controller (autonomous fallback)
│   ├── lit_review_gui.py           # interactive settings dialog (GUI front door)
│   ├── manuscript_parser.py        # bundled .docx/.tex parser
│   └── extract_search_plan.py      # Stage 0 extraction
├── undermind-search/               # Stage 1 (Playwright driver + ingest)
├── scholarlabs-search/             # Stage 2 (Playwright driver + ingest)
├── deepresearch-search/            # Stage 2b (Gemini Deep Research API)
├── supplementary-search/           # Google Scholar + opt-in sources
├── lit-dedup/                      # Stage 5 + 5b verification
├── lit-screen/                     # Stage 6
├── examples/sample_manuscript.tex
└── _legacy/                        # previous Undermind Playwright automation
```

The four architecture diagrams above are hosted at
`https://kennethkhoocy.github.io/files/lit-review-orchestrator/`, with editable
Excalidraw sources in [`docs/images/`](docs/images).
