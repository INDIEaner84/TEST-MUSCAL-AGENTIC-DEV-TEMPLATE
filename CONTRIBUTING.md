# Contributing

1. Create a focused branch or worktree.
2. Preserve the public protocol compatibility of `.ai/STATE.json`, dispatches, and history records, or document and test a version change.
3. Run:

   ```bash
   make ai-validate
   make ai-test
   ```

4. Explain changes to workflow semantics, loop prevention, concurrency, or security boundaries in the pull request.
5. Never commit agent-provider credentials, webhook tokens, private prompts, or task data that should not be public.

Workflow definitions under `.ai/WORKFLOWS/` use JSON syntax while retaining `.yml` names. Keep them valid JSON so the dependency-free validator can read them.

New roles require coordinated updates to the CLI role set, schemas, configuration, role contract, GitHub routing, documentation, and tests. New states require workflow transition tests and explicit terminal/assignment semantics.
