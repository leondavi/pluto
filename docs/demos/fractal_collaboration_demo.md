# Demo: Fractal Collaboration (real Pluto + real Copilot)

_Generated: 2026-04-24T02:34:38+00:00_

## Setup

- Pluto server: real Erlang server on `127.0.0.1:9201` (HTTP)
- Workspace:    `/tmp/pluto/demo/fractal_collaboration`
- Roles loaded from `library/roles/`:
  - **orchestrator-1** (Python harness driving the protocol)
  - **specialist-1**   (real `copilot -p ... --model <default>`)
  - **reviewer-1**     (deterministic static checks)
  - **qa-1**           (real `pytest` runner)

## Task Decomposition

### `t-001` — Implement Mandelbrot iteration module
- **owner:** `specialist`  
- **type:**  `code`  
- **dependencies:** `none`  
- **files:** `['file:/tmp/pluto/demo/fractal_collaboration/src/fractals/mandelbrot.py']`  
- **definition_of_done:** Module mandelbrot.py exposes iterate(c, max_iter)->int (escape time) and grid(width, height, x_min, x_max, y_min, y_max, max_iter)->2D list of escape times. iterate(0+0j, 100) must return 100 (no escape).
- **verification_hint:** `python -c "from src.fractals.mandelbrot import iterate; assert iterate(0+0j, 100) == 100; assert iterate(2+2j, 100) < 5"`

### `t-002` — Implement run script: render image + statistics
- **owner:** `specialist`  
- **type:**  `code`  
- **dependencies:** `['t-001']`  
- **files:** `['file:/tmp/pluto/demo/fractal_collaboration/scripts/run_mandelbrot.py']`  
- **definition_of_done:** Script run_mandelbrot.py renders an 800x600 PNG at outputs/mandelbrot.png AND writes outputs/stats.json with keys 'convergence_ratio' (float in [0,1]) and 'mean_escape_time' (float). Uses only the standard library + Pillow if available; otherwise emits a PGM file.
- **verification_hint:** `python scripts/run_mandelbrot.py && python -c "import json; s=json.load(open('outputs/stats.json')); assert 0<=s['convergence_ratio']<=1; assert s['mean_escape_time']>0"`

### `t-003` — Unit tests for iteration + grid invariants
- **owner:** `specialist`  
- **type:**  `code`  
- **dependencies:** `['t-001']`  
- **files:** `['file:/tmp/pluto/demo/fractal_collaboration/tests/test_mandelbrot.py']`  
- **definition_of_done:** tests/test_mandelbrot.py contains at least 3 pytest tests: iterate_origin_no_escape, iterate_far_escapes_fast, grid_shape_matches_request. All pass.
- **verification_hint:** `pytest tests/test_mandelbrot.py -q`

## Final State

| task_id | state |
|---------|-------|
| `t-001` | `completed` |
| `t-002` | `completed` |
| `t-003` | `completed` |

## QA Result

- **status:** `pass`
- **metrics:**
  - `convergence_ratio`: `0.1806375`
  - `mean_escape_time`: `5.187289915432221`
  - `pytest_rc`: `0`

## Message Trace (chronological, filtered)

_Filtering: payload_type=None and duplicate consecutive recv events are suppressed; capped at 200 rows._

| t (s) | actor | kind | summary |
|------:|-------|------|---------|
|  0.01 | `reviewer-1` | `note` | `registered`  |
|  0.01 | `specialist-1` | `note` | `registered`  |
|  0.01 | `orchestrator-1` | `note` | `registered`  |
|  0.01 | `qa-1` | `note` | `registered`  |
|  0.01 | `orchestrator-1` | `note` | `task_list_broadcast`  |
|  0.01 | `orchestrator-1` | `send` | → **specialist-1** type=`task_assigned` |
|  0.01 | `specialist-1` | `recv` | ← **orchestrator-1** type=`task_assigned` |
|  0.01 | `specialist-1` | `lock` | `acquire_request` file:/tmp/pluto/demo/fractal_collaboration/src/fractals/mandelbrot.py |
|  0.02 | `specialist-1` | `lock` | `acquire_response` file:/tmp/pluto/demo/fractal_collaboration/src/fractals/mandelbrot.py |
|  0.02 | `specialist-1` | `shell` | `copilot -p ...` |
| 32.11 | `specialist-1` | `shell` | `copilot_done` rc=0 |
| 32.11 | `specialist-1` | `send` | → **orchestrator-1** type=`task_result` |
| 32.11 | `orchestrator-1` | `recv` | ← **specialist-1** type=`task_result` |
| 32.11 | `orchestrator-1` | `send` | → **reviewer-1** type=`task_assigned_for_review` |
| 32.11 | `reviewer-1` | `recv` | ← **orchestrator-1** type=`task_assigned_for_review` |
| 32.11 | `reviewer-1` | `send` | → **orchestrator-1** type=`review` |
| 32.11 | `orchestrator-1` | `recv` | ← **reviewer-1** type=`review` |
| 32.11 | `orchestrator-1` | `send` | → **specialist-1** type=`task_assigned` |
| 32.12 | `specialist-1` | `recv` | ← **orchestrator-1** type=`task_assigned` |
| 32.12 | `specialist-1` | `lock` | `acquire_request` file:/tmp/pluto/demo/fractal_collaboration/scripts/run_mandelbrot.py |
| 32.12 | `specialist-1` | `lock` | `acquire_response` file:/tmp/pluto/demo/fractal_collaboration/scripts/run_mandelbrot.py |
| 32.12 | `specialist-1` | `shell` | `copilot -p ...` |
| 70.51 | `specialist-1` | `shell` | `copilot_done` rc=0 |
| 70.51 | `specialist-1` | `release` | lock_ref=`LOCK-1` |
| 70.51 | `specialist-1` | `send` | → **orchestrator-1** type=`task_result` |
| 70.51 | `orchestrator-1` | `recv` | ← **specialist-1** type=`task_result` |
| 70.52 | `orchestrator-1` | `send` | → **reviewer-1** type=`task_assigned_for_review` |
| 70.52 | `specialist-1` | `recv` | ← **orchestrator-1** type=`task_assigned` |
| 70.52 | `specialist-1` | `lock` | `acquire_request` file:/tmp/pluto/demo/fractal_collaboration/tests/test_mandelbrot.py |
| 70.52 | `specialist-1` | `lock` | `acquire_response` file:/tmp/pluto/demo/fractal_collaboration/tests/test_mandelbrot.py |
| 70.52 | `specialist-1` | `shell` | `copilot -p ...` |
| 70.52 | `reviewer-1` | `recv` | ← **orchestrator-1** type=`task_assigned_for_review` |
| 70.52 | `reviewer-1` | `send` | → **orchestrator-1** type=`review` |
| 70.52 | `orchestrator-1` | `recv` | ← **reviewer-1** type=`review` |
| 99.47 | `specialist-1` | `shell` | `copilot_done` rc=0 |
| 99.47 | `specialist-1` | `release` | lock_ref=`LOCK-2` |
| 99.47 | `specialist-1` | `send` | → **orchestrator-1** type=`task_result` |
| 99.48 | `orchestrator-1` | `recv` | ← **specialist-1** type=`task_result` |
| 99.48 | `orchestrator-1` | `send` | → **reviewer-1** type=`task_assigned_for_review` |
| 99.48 | `reviewer-1` | `recv` | ← **orchestrator-1** type=`task_assigned_for_review` |
| 99.48 | `reviewer-1` | `send` | → **orchestrator-1** type=`review` |
| 99.48 | `orchestrator-1` | `recv` | ← **reviewer-1** type=`review` |
| 99.48 | `orchestrator-1` | `send` | → **qa-1** type=`qa_request` |
| 99.48 | `qa-1` | `recv` | ← **orchestrator-1** type=`qa_request` |
| 99.75 | `qa-1` | `shell` | `pytest_done` rc=0 |
| 99.75 | `qa-1` | `send` | → **orchestrator-1** type=`qa_result` |
| 99.76 | `orchestrator-1` | `recv` | ← **qa-1** type=`qa_result` |
## Notes

- Every file mutation by the Specialist was preceded by a real
  Pluto `write` lock acquisition over `/locks/acquire` and
  followed by `/locks/release`.
- The Orchestrator never wrote files itself — it only published
  the task list, dispatched `task_assigned`, and consumed
  `task_result` / `review` / `qa_result`.
- Re-run with: `PLUTO_RUN_DEMOS=1 python -m pytest tests/demo_fractal -s`
