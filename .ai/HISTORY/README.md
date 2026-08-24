# Agent history

This directory stores immutable, structured evidence by task:

```text
.ai/HISTORY/<task_id>/<UTC timestamp>-<role>-<record id>.json
```

`apply-decision` writes orchestrator decisions. `complete-assignment` writes analyzer, worker, reviewer, and tester results. Agents should use those commands rather than hand-authoring records.

History is an audit trail, not the primary prompt. A new session reads `STATE.json` and compressed `CONTEXT.md` first, then only the latest records relevant to its assignment. Git history remains the detailed source for code changes.

If a record is inaccurate, add a later correcting result and mention the superseded filename. Do not silently edit immutable evidence.
