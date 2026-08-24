# Tasks

Each durable task has one Markdown file named `<task_id>.md`. `STATE.json.task_file` points to the active one. Do not infer the active task from the newest filename.

Create a task locally:

```bash
python3 scripts/ai/protocol.py start-task \
  --id feature-001 \
  --title 'Add the requested capability' \
  --workflow feature \
  --description 'Outcome, users, and important constraints.'
```

Or run the **AI Task Intake** workflow in GitHub Actions. Use `auto` to let the first orchestrator decision select the workflow, or choose `cleanup`, `feature`, `bugfix`, `refactor`, or `security` explicitly.

Before orchestration, replace generic acceptance criteria and notes in the generated task file with task-specific, observable requirements. Detailed discussion may remain in a linked issue, but the task file must contain enough durable information for a memoryless session to proceed safely.

Task files are retained after completion. Do not rewrite old tasks to describe a new objective; create a new ID instead.
