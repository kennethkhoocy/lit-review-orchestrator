# Codex Appendix

Use this appendix when running `lit-review-orchestrator` from Codex. The shared
workflow is documented in `SKILL.md`; this file supplies Codex-specific routing,
subagent constraints, and installation notes.

## Invocation

Install the repository as a Codex skill, restart Codex if needed, then invoke the
skill explicitly:

```text
$lit-review-orchestrator
```

Codex reads `AGENTS.md` as repository guidance. Claude Code reads `CLAUDE.md`.

## Subagent Rule

Codex subagents require explicit user authorization. If the user invokes the
skill without authorizing parallel agent work, run the agent-driven workflow in
the main Codex session and ask before spawning subagents for Stage 4d, Stage 5,
or Stage 6 batches. If the user says to use parallel Codex subagents, one agent
per independent batch is appropriate.

## Codex Keyless Profile

Where the Claude Code workflow uses Opus 4.8 for the keyless path, Codex should
use `gpt-5.5` with `model_reasoning_effort="xhigh"`.

| Pipeline role | Codex routing |
| --- | --- |
| Main orchestration | Parent Codex session, preferably `gpt-5.5` |
| Stage 0 search-plan extraction | `gpt-5.5` with `xhigh` reasoning |
| Stage 4d keyless web search | `gpt-5.5` subagents with `xhigh` reasoning |
| Stage 6 relevance screening | `gpt-5.5` subagents with `xhigh` reasoning |
| Stage 4a query writing | `gpt-5.4-mini` with `medium` reasoning |
| Undermind clarifying answers | `gpt-5.4-mini` with `medium` reasoning |
| Stage 5 dedup judgments | `gpt-5.4-mini` with `high` reasoning; escalate low-confidence pairs to `gpt-5.5` |

The GUI field `"all_opus"` was originally named for Claude Code. In Codex runs,
treat `"all_opus": true` as a high-accuracy routing request: use `gpt-5.5` with
`xhigh` reasoning for all delegated reasoning stages. When it is false or absent,
use the split above.

## Tool Notes

- Use Codex subagents only after explicit user authorization.
- For Stage 4d, use the available Codex web-search or browser capabilities to
  gather real open-web paper metadata, then write the batch JSON requested by
  `websearch-search/scripts/websearch_ingest.py`.
- Keep verification on for keyless web-search results.
- Preserve the emit/ingest seam: scripts emit prompt or task files, Codex writes
  JSON results, and the same scripts ingest those results.
