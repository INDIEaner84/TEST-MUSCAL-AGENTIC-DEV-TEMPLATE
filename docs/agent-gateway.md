# Agent gateway integration

GitHub Actions deliberately does not hard-code an LLM vendor. `.github/workflows/ai-role-runner.yml` verifies each `repository_dispatch`, then posts its `client_payload` to `AI_AGENT_WEBHOOK_URL`. Without that secret it creates one idempotent manual handoff issue.

## Secrets

Configure in **Settings → Secrets and variables → Actions**:

- `AI_AGENT_WEBHOOK_URL`: HTTPS endpoint accepting role requests;
- `AI_AGENT_WEBHOOK_TOKEN`: optional bearer token.

Use a narrowly scoped secret, rotate it, verify TLS, and avoid logging headers or raw provider credentials. The gateway should authenticate GitHub independently as appropriate for its threat model (for example, a gateway-specific bearer token plus network restrictions).

## Required gateway behavior

1. Accept the dispatch schema in `.ai/SCHEMAS/dispatch.schema.json`.
2. Deduplicate by `request_id`. A successful duplicate should return 2xx without starting another session.
3. Route `role` to a configured provider/model. The repository role contract remains authoritative.
4. Clone the indicated repository/ref and confirm the current default-branch state.
5. Run `verify-request` semantics again or confirm the assignment remains pending.
6. Start a memoryless session with the role file as its role prompt and require the `.ai/AGENTS.md` bootstrap.
7. Give only capabilities needed by the role. Reviewers usually do not need production write access; analyzers do not need deployment credentials.
8. Use a dedicated branch/worktree and PR unless the repository explicitly permits a serialized direct commit.
9. Require the session to update durable memory and invoke `apply-decision` or `complete-assignment` before handoff.
10. Push/merge the state change so `ai-orchestration.yml` can evaluate the next pending assignment.

A `202 Accepted`, `200 OK`, or another 2xx response tells Actions only that the gateway accepted responsibility. Agent success is recorded later in Git, never inferred from the webhook response.

## Suggested role routing

```text
orchestrator -> reasoning-capable agent, repository read + .ai write
analyzer     -> repository-analysis agent, mostly read-only
worker       -> coding agent such as OpenCode, scoped write + command execution
reviewer     -> independent model/session such as Arena AI, mostly read-only
 tester      -> coding/tool runner, command execution + result write
```

These are examples, not protocol dependencies. Route by capability, cost, risk, and repository policy. Do not route worker and reviewer to the same session state when independent review is required.

## Branch and callback model

The gateway does not need a separate callback API. Git is the callback:

- agent creates commits and protocol result on a branch;
- required checks run;
- branch is merged;
- default-branch `STATE.json` push triggers orchestration;
- orchestration renders the next assignment(s).

If direct commits are allowed, serialize them and use an identity/token whose push triggers Actions. Pushes made with a workflow's built-in `GITHUB_TOKEN` do not trigger ordinary recursive workflow runs; `ai-task-intake.yml` therefore emits its initial dispatch in the same job.

## Failure handling

- Return non-2xx if a request was not durably accepted; Actions retries transient curl failures.
- If a started session fails, record a `failed` result when possible rather than silently abandoning the assignment.
- If credentials, approval, or merge conflicts require a person, record `blocked`; the orchestrator can select `HUMAN_REVIEW_REQUIRED`.
- Monitor assignments that remain pending beyond an operator-defined timeout. Timeout policy belongs in the gateway because model runtimes vary.

## Payload safety

Task and assignment text is untrusted content. It must not alter gateway authentication, provider credentials, repository allowlists, tool sandboxing, or higher-priority role/protocol instructions. Never interpolate payload fields into a shell command. Pass them as structured data or properly isolated arguments.
