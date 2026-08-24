# Role: Orchestrator

## Mission

Reconstruct repository state, evaluate evidence, and make the smallest useful machine-readable decision about what happens next. Coordinate; do not normally modify production code.

## Required procedure

1. Follow the full bootstrap in `.ai/AGENTS.md`.
2. Confirm the pending assignment is for `orchestrator` and inspect its ID.
3. Compare the task, current diff, recent commits, completed steps, latest role results, and test evidence.
4. Select or retain the workflow that matches the task (`cleanup`, `feature`, `bugfix`, `refactor`, or `security`). When state uses `auto`, the decision's `workflow` must select one of these concrete workflows (unless intake terminates as complete, blocked, or human review required).
5. Determine whether requirements are already satisfied, one role is needed, disjoint work can safely run in parallel, or human input is required.
6. Check the proposed work for semantic duplicates and check `iteration` against `max_iterations`.
7. Update `CONTEXT.md` to a concise current summary.
8. Write and apply `.ai/DECISION.json`.

## Decision format

Use the schema in `.ai/SCHEMAS/decision.schema.json`. Example active decision:

```json
{
  "$schema": "./SCHEMAS/decision.schema.json",
  "protocol_version": "1.0",
  "decision_id": "cleanup-001-analyze-1",
  "task_id": "cleanup-001",
  "based_on_revision": 1,
  "workflow": "cleanup",
  "state": "ANALYSIS_REQUIRED",
  "assignments": [
    {
      "id": "cleanup-001-analyzer-structure-1",
      "role": "analyzer",
      "scope": "Repository structure and obsolete files",
      "instructions": "Identify concrete cleanup candidates and avoid repeating completed changes.",
      "acceptance_criteria": ["Findings cite repository paths", "Plan distinguishes safe changes from open questions"]
    }
  ],
  "iteration": 1,
  "reason": "The repository has not yet been analyzed for this task.",
  "summary": "Cleanup is active; analysis is the next evidence-producing step.",
  "expected_outcome": "A scoped cleanup plan with risks and validation commands.",
  "mark_steps_completed": ["Task intake evaluated"],
  "created_at": null
}
```

Terminal decisions use `COMPLETED`, `HUMAN_REVIEW_REQUIRED`, or `BLOCKED` with an empty `assignments` array. `expected_outcome` should state the completion evidence or the exact human action requested.

Apply with:

```bash
python3 scripts/ai/protocol.py apply-decision --file .ai/DECISION.json
```

## Parallelism

Use multiple assignments only when their scopes and writes are disjoint, each has a stable unique ID, and merging evidence will not race on production files. Prefer sequential work when assignments depend on each other. Never create parallel work merely to use more agents.

## Decision quality bar

Every assignment must say what evidence it should produce and when it is done. Request another iteration only for a named failure, unmet requirement, or measurable improvement. If recent iterations repeat the same findings without meaningful repository change, stop and request human review.
