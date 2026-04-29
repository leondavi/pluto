# Pluto Collaboration Protocol

Shared ontology and typed msg schemas every role in a Pluto team
must speak. No ad-hoc shapes: extend this doc first, then implement.

All msgs travel over `agents/send` / `agents/broadcast` and arrive
as the `payload` of `msg_recv` events.

## 1. Resource Naming (URN-like)

Stable string IDs used in lock requests and msg payloads.

| Resource | Format | Example |
|-|-|-|
| File | `file:/abs/path` | `file:/workspaces/pluto/src/x.py` |
| Directory | `dir:/abs/path` | `dir:/tmp/pluto/demo/fractal` |
| Dataset | `dataset:<name>@<version>` | `dataset:mnist@v3` |
| Experiment run | `experiment:<proj>/<run_id>` | `experiment:fractal/run-2026-04-23-01` |
| Model artifact | `model:<name>@<version>` | `model:mandelbrot-stats@v1` |
| Service | `service:<name>` | `service:feature-store` |
| GPU | `gpu:<host>:<index>` | `gpu:node-7:2` |
| Cluster slot | `cluster:<name>:<partition>` | `cluster:prod:a100` |
| Shared scratch | `scratch:<demo_name>` | `scratch:fractal_demo` |

Rules:
- IDs are case-sensitive; no whitespace.
- Absolute paths required for `file:` and `dir:`; no `~` or relative paths.
- Dataset/model versions required; never lock a bare `dataset:name`.

## 2. Task & Run IDs

- `task_id`: slug with optional dotted children. Example: `t-001`, `t-001.2`.
- `parent_task_id`: the immediate parent, or omitted for roots.
- `run_id`: free-form, conventionally `run-<ISO-date>-<n>`.

Task states: `pending | in_progress | blocked | completed | failed | cancelled`.

## 3. Shared Task List

A single JSON document bcast whenever it changes; optionally mirrored to
`scratch:<demo_name>/tasks.json`.

Schema:

```json
{"type":"task_list","version":3,"updated_by":"orchestrator","updated_at":"2026-04-23T10:15:00Z","tasks":[{"task_id":"t-001","parent_task_id":null,"title":"Implement Mandelbrot iteration","type":"code","owner":"specialist","state":"in_progress","files":["file:/.../src/fractals/mandelbrot.py"],"resources":[],"dependencies":[],"definition_of_done":"iterate(c,max_iter) returns escape-time int; unit test passes","verification_hint":"pytest tests/test_mandelbrot.py::test_iterate"}]}
```

## 4. Message Types

Every payload MUST include `type` (and `task_id` if applicable). Unknown
types MUST be ignored with a warning, never acted on.

### 4.1 `task_assigned` (Orchestrator -> Specialist / worker)

```json
{"type":"task_assigned","task":{"...":"full task object from §3"},"constraints":["no network calls","no changes outside listed files"],"acceptance_criteria":["unit test test_iterate passes"],"verification_hints":["pytest tests/test_mandelbrot.py -k iterate"]}
```

### 4.2 `task_clarification_request` (Worker -> Orchestrator)

Emitted when the assignment is ambiguous or under-specified. Worker MUST NOT
proceed until a clarifying `task_assigned` arrives.

```json
{"type":"task_clarification_request","task_id":"t-001","questions":["Should escape-time be capped at max_iter or returned as -1 on non-escape?"],"proposed_decomposition":[{"title":"Decide escape-time return convention","owner":"orchestrator"}]}
```

### 4.3 `task_result` (Worker -> Orchestrator)

```json
{"type":"task_result","task_id":"t-001","status":"done","summary":"Implemented iterate() + tests passing","details":{"files_changed":["file:/.../src/fractals/mandelbrot.py"]},"notes":["observed: run_mandelbrot.py has an unrelated TODO"]}
```

`status`: `done | error | cancelled`.

### 4.4 `review` (Reviewer -> Orchestrator)

```json
{"type":"review","task_id":"t-001","status":"approved","findings":[],"suggested_fixes":[]}
```

`status`: `approved | needs_changes`.

### 4.5 `decomposition_feedback` (Reviewer/QA -> Orchestrator)

Used when a task is under-specified or overlapping with another.

```json
{"type":"decomposition_feedback","task_id":"t-001","issue":"ambiguous","description":"No definition_of_done for color-mapping output.","suggested_split":[{"title":"Define color-mapping output format"},{"title":"Implement mapping function"}]}
```

### 4.6 `qa_result` (QA -> Orchestrator)

```json
{"type":"qa_result","scope":{"task_ids":["t-001","t-002"],"branch":"v0.2.6"},"status":"pass","failed_checks":[],"metrics":{"tests_passed":12,"duration_s":3.4},"logs_ref":"scratch:fractal_demo/qa.log"}
```

### 4.7 `experiment_result` (Experiment Runner -> Orchestrator)

```json
{"type":"experiment_result","run_id":"run-2026-04-23-01","task_id":"t-007","status":"completed","artifacts":["file:/.../outputs/mandelbrot.png"],"metrics":{"convergence_ratio":0.273,"mean_escape_time":18.4}}
```

### 4.8 `evaluation_report` (Evaluator -> Orchestrator)

```json
{"type":"evaluation_report","task_id":"t-009","baseline":"run-A","candidate":"run-B","metrics_delta":{"accuracy":0.012,"latency_ms":-3.1},"verdict":"candidate_better"}
```

### 4.9 `deploy_result` (Deployer -> Orchestrator)

```json
{"type":"deploy_result","task_id":"t-010","environment":"staging","status":"success","rollback_handle":"deploy-2026-04-23-01"}
```

### 4.10 `remote_task` / `remote_result` (Orchestrator <-> SSH Bridge)

```json
{"type":"remote_task","task_id":"t-011","profile":"gpu-nodes","intent":"launch training","allowed_commands":["python train.py --config=..."],"cwd":"/home/ml/fractal","timeout_s":3600}
```

```json
{"type":"remote_result","task_id":"t-011","status":"ok","exit_code":0,"stdout_tail":"...","stderr_tail":""}
```

### 4.11 `scope_mismatch` (any worker -> Orchestrator)

```json
{"type":"scope_mismatch","task_id":"t-004","observed_need":"requires editing file:/.../utils/io.py which is not in scope","refuse_reason":"not in my lock set","proposed_new_tasks":[{"title":"Refactor io.py","owner":"specialist"}]}
```

## 5. Locking Discipline

1. **Write-before-edit invariant:** no agent writes to a `file:` or `dir:`
   resource without a confirmed `write` lock.
2. `POST /lock` → if the response body contains `ref`, the lock is queued.
   Wait for a `lock_granted` event. Do **not** spin-poll.
3. Always pass an explicit `ttl_ms`; never rely on defaults.
4. Release every lock you acquire, even on error paths.
5. Orchestrator may pre-acquire locks on behalf of a worker and pass the
   lock handles in `task_assigned.constraints`, but default is "worker locks
   its own files".

## 6. Ambiguity Rule (applies to all roles)

Ambiguity is a **first-class error condition**, not something to silently
route around. Any role receiving an ambiguous task MUST:

1. Stop before making any irreversible change.
2. Emit `task_clarification_request` (or `scope_mismatch` if scope is the
   issue) to the Orchestrator.
3. Optionally propose a refined decomposition.
4. Resume only after a new `task_assigned` arrives.

A task is ambiguous if **any** of the following holds:

- No clear, independently-verifiable `definition_of_done`.
- Inputs or outputs are not concretely listed.
- Scope requires touching resources outside the `files` / `resources` lists.
- Two valid interpretations exist with different observable outputs.
