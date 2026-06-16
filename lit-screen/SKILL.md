---
name: lit-screen
description: >
  Stage 6 of the lit review pipeline: screen paper abstracts against the
  research prompt. The orchestrator's agent-driven flow runs this re-ranker on
  Opus subagents; a standalone run uses the in-script Claude Sonnet API fallback.
  Rates relevance 1-10, tags each paper
  as theoretical/empirical, identifies methodology, and classifies relationship
  to user's work. Only use this skill when explicitly requested -- e.g., the
  user says "run lit-screen", "lit-screen", or "/lit-screen". Do NOT
  auto-trigger on general literature review requests.
---

# lit-screen (Stage 6 -- Abstract Screening)

Screen every paper's abstract against the original research prompt. In the
orchestrator's agent-driven flow the relevance judgment is produced by **Opus
subagents** through the `--emit-tasks` / `--ingest-results` seam (no API key); a
standalone run uses the in-script Claude Sonnet API path instead. Produces a
relevance score (1-10), rationale, and structured tags for each paper.

## Usage

```bash
python ~/.claude/skills/lit-screen/scripts/lit_screen.py \
  --input stage5_merged.json \
  --query "your research prompt here" \
  -o stage6_screened.json
```

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--input` | (required) | Input JSON from Stage 5 (dedup output) |
| `--query` | (required) | Research query/prompt to screen against |
| `-o, --output` | `stage6_screened.json` | Output JSON path |
| `--model` | `claude-sonnet-4-6` | Anthropic model ID (autonomous fallback) |
| `--concurrency` | `5` | Max simultaneous API requests (autonomous fallback) |
| `--emit-tasks PATH` | — | Agent-driven: write per-paper screening tasks and stop (no API) |
| `--ingest-results PATH` | — | Agent-driven: merge Opus screening results and write all outputs (no API) |

## Output Schema

Each paper gets these fields added:

```json
{
  "screening_score": 8,
  "screening_rationale": "Directly examines board composition changes...",
  "paper_type": "empirical",
  "identification_strategy": "DiD",
  "relationship": "direct competitor"
}
```

## Output Files

1. **JSON**: `stage6_screened.json` -- full paper list with screening fields
2. **JSON**: `stage6_filtered.json` -- papers with score >= 4 only
3. **XLSX**: `stage6_screened.xlsx` -- all papers, sorted by screening_score descending
4. **XLSX**: `stage6_filtered.xlsx` -- filtered papers (score >= 4), sorted by score descending
5. **RIS**: `stage6_screened.ris` -- for import into reference managers
6. **BIB**: `stage6_screened.bib` -- BibTeX entries for papers with score >= 5

## Environment Variables

| Var | Required | Description |
|-----|----------|-------------|
| `ANTHROPIC_API_KEY` | Fallback | Standalone-run screening (Sonnet API). The agent-driven flow screens with Opus subagents and needs no key. |

## Field Values

- **screening_score**: 1 (irrelevant) to 10 (highly relevant); 0 = no abstract
- **paper_type**: `theoretical` or `empirical`
- **identification_strategy**: `natural experiment`, `IV`, `DiD`, `RDD`, `structural`, `descriptive`, `N/A`
- **relationship**: `foundational/must-cite`, `same method different context`, `same context different method`, `direct competitor`, `methodological reference`, `tangential`
