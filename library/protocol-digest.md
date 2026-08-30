# Pluto Protocol — Digest

Compact operational summary of the shared collaboration protocol. The
full text (schemas, examples, injection frames) is available as the MCP
resource `pluto://protocol` (or the `/pluto-protocol` prompt) — fetch it
when you need exact message schemas; do NOT read it from disk.

## Resources & IDs

Every coordinatable resource has a stable, case-sensitive string ID used
in locks and payloads: `file:/abs/path`, `dir:/abs/path`,
`dataset:<name>@<version>`, `experiment:<proj>/<run_id>`,
`model:<name>@<version>`, `service:<name>`, `gpu:<host>:<index>`,
`cluster:<name>:<partition>`, `scratch:<demo_name>`. Absolute paths
only; dataset/model versions required. Task ids are slugs with dotted
children (`t-001`, `t-001.2`); task states:
`pending | in_progress | blocked | completed | failed | cancelled`.

## Message types

Every payload MUST include `"type"` (plus `"task_id"` where applicable).
Ignore unknown types with a warning.

| type | direction | key fields |
|---|---|---|
| `task_assigned` | Orchestrator → worker | `task` (full object), `constraints`, `acceptance_criteria`, optional `spec_ref` |
| `task_clarification_request` | worker → Orchestrator | `task_id`, `questions[]`, optional `proposed_decomposition` |
| `task_result` | worker → Orchestrator | `task_id`, `status: done\|error\|cancelled`, `summary`, `details.files_changed` |
| `review` | Reviewer → Orchestrator | `task_id`, `status: approved\|needs_changes`, `findings[]`, `suggested_fixes[]` |
| `decomposition_feedback` | Reviewer/QA → Orchestrator | `task_id`, `issue`, `description`, `suggested_split[]` |
| `qa_result` | QA → Orchestrator | `scope{task_ids,branch}`, `status`, `failed_checks[]`, `metrics`, `logs_ref` |
| `experiment_result` | Runner → Orchestrator | `run_id`, `task_id`, `status`, `artifacts[]`, `metrics` |
| `evaluation_report` | Evaluator → Orchestrator | `task_id`, `baseline`, `candidate`, `metrics_delta`, `verdict` |
| `deploy_result` | Deployer → Orchestrator | `task_id`, `environment`, `status`, `rollback_handle` |
| `remote_task` / `remote_result` | Orchestrator ↔ SSH Bridge | `task_id`, `profile`, `allowed_commands[]` / `exit_code`, `stdout_tail` |
| `scope_mismatch` | any worker → Orchestrator | `task_id`, `observed_need`, `refuse_reason`, `proposed_new_tasks[]` |
| `spec_contract` | Orchestrator → all (broadcast) | `spec_id`, `version`, `constraints{}` — session-wide rules |

## Locking discipline

- Write-before-edit invariant: no write to a `file:`/`dir:` resource
  without a confirmed `write` lock.
- If acquire returns a `ref` (instead of `status: ok`) the lock is
  QUEUED — do not spin-poll; wait for the `lock_granted` event and work
  on a different parallel-safe task meanwhile.
- Always pass an explicit `ttl_ms`. Release every lock you acquire, on
  every code path including errors.

## Ambiguity rule

Ambiguity is a first-class error: stop before any irreversible change,
emit `task_clarification_request` (or `scope_mismatch` if scope is the
issue), and resume only after a new `task_assigned`. A task is ambiguous
if it lacks a verifiable `definition_of_done`, inputs/outputs aren't
concrete, it needs resources outside its lists, or two valid readings
differ observably.

## Spec contracts

The Orchestrator broadcasts `spec_contract` once per session; workers
MUST cache it by `spec_id` and apply its constraints to every
`task_assigned` carrying a matching `spec_ref` (additive — task-local
constraints tighten, never relax). Receiving a `spec_ref` you haven't
seen → emit `task_clarification_request`; do not proceed. This factoring
replaces ~1–2K tokens of repeated boilerplate per dispatch.

Full protocol: MCP resource `pluto://protocol` / prompt `/pluto-protocol`.
