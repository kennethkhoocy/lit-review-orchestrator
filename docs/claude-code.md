# Claude Code Appendix

Use this appendix when running `lit-review-orchestrator` from Claude Code. The
shared workflow is documented in `SKILL.md`; this file supplies the Claude Code
model and tool routing for the agent-driven path.

## Invocation

Install the repository under `~/.claude/skills/lit-review-orchestrator`, restart
Claude Code if needed, then invoke the skill explicitly:

```text
lit-review-orchestrator
```

The skill is intended for explicit invocation. The user should provide a `.tex`
or `.docx` manuscript, an abstract file, or a raw research query.

## Agent-Driven Routing

| Pipeline role | Claude Code routing |
| --- | --- |
| Main orchestration | Main Claude Code session on Opus 4.8 |
| Stage 0 search-plan extraction | Opus 4.8 |
| Stage 4d keyless web search | Opus 4.8 subagents, one per batch |
| Stage 6 relevance screening | Opus 4.8 subagents |
| Stage 4a query writing | Sonnet 4.6 subagents |
| Undermind clarifying answers | Sonnet 4.6 subagents |
| Stage 5 dedup judgments | Sonnet 4.6 subagents; escalate uncertain cases to Opus 4.8 when needed |

When the GUI returns `"all_opus": true`, run every subagent on Opus 4.8. When it
is false or absent, use the split above.

## Tool Notes

- Use the Claude Code Task/Agent tool for delegated reasoning and batch fan-out.
- Use WebSearch and WebFetch for Stage 4d web-search batches.
- Keep the emit/ingest seam intact: scripts emit prompt or task files, the agent
  writes JSON results, and the same scripts ingest those results.
- The autonomous fallback remains `python scripts/orchestrator.py <doc>
  --output-dir OUT`, which uses API-backed reasoning for unattended runs.
