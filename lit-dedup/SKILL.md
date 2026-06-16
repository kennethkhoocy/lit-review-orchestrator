---
name: lit-dedup
description: >
  Stage 5 of the lit review pipeline: merge and deduplicate papers from all
  prior stages (Undermind, Scholar Labs, supplementary search) into a single
  master list with provenance tracking. Two-pass dedup: exact DOI match then
  LLM fuzzy match via DeepSeek/Claude. Only use this skill when explicitly
  requested — e.g., the user says "run lit-dedup", "lit-dedup", or
  "/lit-dedup". Do NOT auto-trigger on general literature review requests.
---

# Lit-Dedup (Stage 5 — Merge & Deduplicate)

Merge multiple pipeline stage outputs into one deduplicated master list
with provenance tracking.

**Input**: one or more JSON files from Stages 1–4
**Output**: `merged_results.json` + `merged_results.ris` + `dedup_log.json`

## Quick Start

```bash
# Merge all JSON files in a directory
python scripts/lit_dedup.py --input-dir ./results/ -o merged_results.json

# Merge specific files
python scripts/lit_dedup.py --inputs stage1.json stage2.json stage4.json -o merged.json

# DOI-only dedup (skip LLM pass)
python scripts/lit_dedup.py --inputs *.json --no-llm -o merged.json

# Non-interactive (skip confirmation)
python scripts/lit_dedup.py --inputs *.json -o merged.json --yes
```

## Prerequisites

```bash
pip install aiohttp
```

## API Keys

- `DEEPSEEK_API_KEY` — primary (DeepSeek chat API for fuzzy matching)
- `ANTHROPIC_API_KEY` — fallback (Claude Sonnet via Anthropic API)

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--inputs` | — | Input JSON files (nargs='+') |
| `--input-dir` | — | Directory to glob *.json from |
| `-o, --output` | `merged_results.json` | Output JSON path |
| `--model` | `deepseek-chat` | LLM model override |
| `--concurrency` | `100` | Max simultaneous LLM requests |
| `--no-llm` | off | Skip Pass 2 (DOI-only dedup) |
| `--yes` | off | Skip HITL confirmation checkpoint |
| `--emit-pairs PATH` | — | Agent-driven: write candidate pairs to PATH and stop before the LLM pass (no API) |
| `--ingest-verdicts EMIT VERDICTS` | — | Agent-driven: merge the subagent pair verdicts and write all outputs (no API) |

## Two-Pass Dedup

1. **Pass 1 — DOI match**: normalize DOIs, group by exact match, merge
   using priority (published > forthcoming > working paper)
2. **Pass 2 — LLM fuzzy match**: pre-filter candidate pairs by author
   surname overlap or title Jaccard > 0.4, then judge each pair for a
   same-paper verdict

## Model routing (agent-driven default)

In the orchestrator's agent-driven flow the Pass-2 same-paper judgment is made by a
**subagent**, not the API — a **Sonnet subagent by default**, promoted to **Opus**
when the GUI's *Use Opus for all tasks* (`all_opus`) is set. Run `--emit-pairs` to
dump the candidate pairs, judge each `{i, j, a, b}` pair with the subagent (yes/no
duplicate), then `--ingest-verdicts` to run the union-find merge and write every
output. The DeepSeek/Sonnet API path is the autonomous fallback for standalone runs.
See the orchestrator SKILL.md ("How it runs" → "Model routing").

## Output Schema

Same as pipeline stages, with added fields:

```json
{
  "title": "Paper Title",
  "authors": "Author1, Author2",
  "year": "2024",
  "doi": "10.1234/...",
  "abstract": "...",
  "journal": "Journal Name",
  "url": "https://...",
  "sources": ["undermind", "scholarlabs", "ssrn"],
  "alt_url": "https://ssrn.com/..."
}
```

## Output Files

- `merged_results.json` — deduplicated master list
- `merged_results.ris` — RIS for Zotero import
- `dedup_log.json` — log of all merge decisions and stats
