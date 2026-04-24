# Fractal Collaboration Demo

A multi-agent pipeline (Orchestrator → Specialist → Reviewer → QA) renders
an 800×600 Mandelbrot PNG and computes two statistics over the escape-time
grid, coordinated entirely through Pluto.

---

## Introduction

The Mandelbrot set is defined over the complex plane: for each pixel $(x, y)$,
map it to $c = x_{re} + y_{im}i$ and repeatedly apply $z \leftarrow z^2 + c$
starting from $z = 0$. A point *escapes* when $|z| > 2$; the **escape time**
is the iteration count at which that happens, capped at `max_iter`. Points
that never escape (they belong to the set itself) return `max_iter`.

Two aggregate statistics summarise the grid:

- **`convergence_ratio`** — fraction of pixels that never escaped, in $[0, 1]$.
- **`mean_escape_time`** — average escape time across all pixels.

This task is deliberately structured so that two outputs (`run_mandelbrot.py`
and `test_mandelbrot.py`) both depend on a single core module
(`mandelbrot.py`), creating a natural dependency DAG for the Orchestrator to
enforce.

---

## Setup

| Component | Detail |
|---|---|
| Pluto server | Real Erlang/OTP node on `127.0.0.1:9201` (HTTP) |
| Workspace | `/tmp/pluto/demo/fractal_collaboration` |
| **orchestrator-1** | Python harness driving the protocol |
| **specialist-1** | Real `copilot -p ... --model <default>` |
| **reviewer-1** | Real `copilot -p ... --model <default>`, JSON verdict |
| **qa-1** | Real `pytest` runner |

---

## Task Decomposition

The Orchestrator decomposed the work into **three tasks** with an explicit
dependency DAG:

| Task | File | Depends on | What it does |
|------|------|-----------|--------------|
| `t-001` | `src/fractals/mandelbrot.py` | — | Core math: `iterate(c, max_iter) → int` and `grid(w, h, x_min, x_max, y_min, y_max, max_iter) → 2-D list` |
| `t-002` | `scripts/run_mandelbrot.py` | `t-001` | Render script: calls `grid()`, saves an 800×600 PNG via Pillow (or PGM fallback), writes `outputs/stats.json` |
| `t-003` | `tests/test_mandelbrot.py` | `t-001` | Pytest suite: at minimum `iterate_origin_no_escape`, `iterate_far_escapes_fast`, `grid_shape_matches_request` |

`t-002` and `t-003` both depend on `t-001` (they import the same module),
so the Orchestrator blocked them until `t-001` was reviewed and approved.
Each file was protected by a Pluto **write lock** for its full duration;
the Specialist could not start writing until `acquire` returned `ok`.

### Task definitions

**`t-001`** — Implement Mandelbrot iteration module
- **owner:** `specialist` | **type:** `code` | **dependencies:** none
- **files:** `src/fractals/mandelbrot.py`
- **definition_of_done:** Module exposes `iterate(c, max_iter)->int` and `grid(…)->2D list`. `iterate(0+0j, 100)` must return 100.
- **verification_hint:** `python -c "from src.fractals.mandelbrot import iterate; assert iterate(0+0j, 100) == 100; assert iterate(2+2j, 100) < 5"`

**`t-002`** — Implement run script: render image + statistics
- **owner:** `specialist` | **type:** `code` | **dependencies:** `t-001`
- **files:** `scripts/run_mandelbrot.py`
- **definition_of_done:** Script renders an 800×600 PNG at `outputs/mandelbrot.png` and writes `outputs/stats.json` with `convergence_ratio` (float in [0,1]) and `mean_escape_time` (float).
- **verification_hint:** `python scripts/run_mandelbrot.py && python -c "import json; s=json.load(open('outputs/stats.json')); assert 0<=s['convergence_ratio']<=1; assert s['mean_escape_time']>0"`

**`t-003`** — Unit tests for iteration + grid invariants
- **owner:** `specialist` | **type:** `code` | **dependencies:** `t-001`
- **files:** `tests/test_mandelbrot.py`
- **definition_of_done:** Contains at least 3 pytest tests: `iterate_origin_no_escape`, `iterate_far_escapes_fast`, `grid_shape_matches_request`. All pass.
- **verification_hint:** `pytest tests/test_mandelbrot.py -q`

---

## Results (run on 2026-04-24)

| Metric | Value |
|--------|-------|
| Total wall-time | 1m 42s |
| Tasks completed | 3 / 3 (`t-001`, `t-002`, `t-003`) |
| Reviewer verdict | `approved` for all |
| pytest | `0` (3 tests pass) |
| `convergence_ratio` | `0.181` |
| `mean_escape_time` | `5.19` |
| PNG | `/tmp/pluto/demo/fractal_collaboration/outputs/mandelbrot.png` |

### Message trace (chronological, filtered)

_`payload_type=None` and duplicate consecutive recv events suppressed._

| t (s) | Actor | Kind | Summary |
|------:|-------|------|---------|
|  0.01 | `qa-1` | `note` | `registered` |
|  0.01 | `reviewer-1` | `note` | `registered` |
|  0.01 | `orchestrator-1` | `note` | `registered` |
|  0.01 | `specialist-1` | `note` | `registered` |
|  0.01 | `orchestrator-1` | `note` | `task_list_broadcast` |
|  0.01 | `orchestrator-1` | `send` | → **specialist-1** type=`task_assigned` |
|  0.01 | `specialist-1` | `recv` | ← **orchestrator-1** type=`task_assigned` |
|  0.01 | `specialist-1` | `lock` | `acquire_request` mandelbrot.py |
|  0.01 | `specialist-1` | `lock` | `acquire_response` mandelbrot.py |
|  0.01 | `specialist-1` | `shell` | `copilot -p ...` |
| 24.34 | `specialist-1` | `shell` | `copilot_done` rc=0 |
| 24.34 | `specialist-1` | `release` | lock_ref=`LOCK-5` |
| 24.34 | `specialist-1` | `send` | → **orchestrator-1** type=`task_result` |
| 24.34 | `orchestrator-1` | `recv` | ← **specialist-1** type=`task_result` |
| 24.34 | `orchestrator-1` | `send` | → **reviewer-1** type=`task_assigned_for_review` |
| 24.35 | `reviewer-1` | `recv` | ← **orchestrator-1** type=`task_assigned_for_review` |
| 24.35 | `reviewer-1` | `shell` | `copilot_start` |
| 57.66 | `reviewer-1` | `shell` | `copilot_done` rc=0 |
| 57.66 | `reviewer-1` | `send` | → **orchestrator-1** type=`review` |
| 57.66 | `orchestrator-1` | `recv` | ← **reviewer-1** type=`review` |
| 57.66 | `orchestrator-1` | `send` | → **specialist-1** type=`task_assigned` (t-002) |
| 57.66 | `specialist-1` | `recv` | ← **orchestrator-1** type=`task_assigned` |
| 57.66 | `specialist-1` | `lock` | `acquire_request` run_mandelbrot.py |
| 57.66 | `specialist-1` | `lock` | `acquire_response` run_mandelbrot.py |
| 57.66 | `specialist-1` | `shell` | `copilot -p ...` |
| 103.09 | `specialist-1` | `shell` | `copilot_done` rc=0 |
| 103.09 | `specialist-1` | `release` | lock_ref=`LOCK-6` |
| 103.09 | `specialist-1` | `send` | → **orchestrator-1** type=`task_result` |
| 103.09 | `specialist-1` | `recv` | ← **orchestrator-1** type=`task_assigned` (t-003) |
| 103.09 | `specialist-1` | `lock` | `acquire_request` test_mandelbrot.py |
| 103.09 | `orchestrator-1` | `recv` | ← **specialist-1** type=`task_result` |
| 103.09 | `specialist-1` | `lock` | `acquire_response` test_mandelbrot.py |
| 103.09 | `orchestrator-1` | `send` | → **reviewer-1** type=`task_assigned_for_review` |
| 103.09 | `specialist-1` | `shell` | `copilot -p ...` |
| 103.09 | `reviewer-1` | `recv` | ← **orchestrator-1** type=`task_assigned_for_review` |
| 103.09 | `reviewer-1` | `shell` | `copilot_start` |
| 127.48 | `specialist-1` | `shell` | `copilot_done` rc=0 |
| 127.48 | `specialist-1` | `release` | lock_ref=`LOCK-7` |
| 127.48 | `specialist-1` | `send` | → **orchestrator-1** type=`task_result` |
| 127.48 | `orchestrator-1` | `recv` | ← **specialist-1** type=`task_result` |
| 127.48 | `orchestrator-1` | `send` | → **reviewer-1** type=`task_assigned_for_review` |
| 139.87 | `reviewer-1` | `shell` | `copilot_done` rc=0 |
| 139.87 | `reviewer-1` | `send` | → **orchestrator-1** type=`review` |
| 139.87 | `reviewer-1` | `recv` | ← **orchestrator-1** type=`task_assigned_for_review` |
| 139.87 | `reviewer-1` | `shell` | `copilot_start` |
| 139.87 | `orchestrator-1` | `recv` | ← **reviewer-1** type=`review` |
| 162.08 | `reviewer-1` | `shell` | `copilot_done` rc=0 |
| 162.08 | `reviewer-1` | `send` | → **orchestrator-1** type=`review` |
| 162.08 | `orchestrator-1` | `recv` | ← **reviewer-1** type=`review` |
| 162.08 | `orchestrator-1` | `send` | → **qa-1** type=`qa_request` |
| 162.08 | `qa-1` | `recv` | ← **orchestrator-1** type=`qa_request` |
| 162.29 | `qa-1` | `shell` | `pytest_done` rc=0 |
| 162.29 | `qa-1` | `send` | → **orchestrator-1** type=`qa_result` |
| 162.30 | `orchestrator-1` | `recv` | ← **qa-1** type=`qa_result` |

---

## Insights

- **Role discipline held end-to-end.** The Specialist (real Copilot) wrote
  all 3 files, but *only* files listed in `task.files` and *only* while
  holding the corresponding write lock. The Orchestrator never touched the
  filesystem.
- **Dependency gating worked.** `t-002` and `t-003` were dispatched only
  after `t-001` was reviewed and approved — the Orchestrator blocked on the
  reviewer's `verdict` before sending the next `task_assigned`.
- **Lock contention was zero.** Because the tasks were correctly serialised
  by dependency, every `acquire` returned `status: ok` immediately.
- **The numbers are plausible.** A `convergence_ratio` of ~0.18 is expected
  for the default view $[-2.5, 1.0] \times [-1.2, 1.2]$: roughly 18% of
  that rectangle lies inside the Mandelbrot set cardioid + bulbs. A
  `mean_escape_time` of 5.19 reflects that most escaping points diverge
  quickly near the boundary edges.
- **Known noise (not blocking).** Pluto's HTTP `long_poll` re-delivered
  already-acked messages with empty payloads, producing ~300k `recv
  type=None` events over 100 s. The report writer filters these; the
  underlying server-side dedup is a follow-up item.

---

## How to re-run

```bash
# Restart Pluto on the demo ports
./PlutoServer.sh --kill
./PlutoServer.sh --daemon

PLUTO_RUN_DEMOS=1 python -m pytest tests/demo_fractal -s
```

Source: [tests/demo_fractal](../../tests/demo_fractal)
