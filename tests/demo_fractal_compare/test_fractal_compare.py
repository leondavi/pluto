"""
demo_fractal_compare — Haiku team vs Sonnet team on a richer fractal toolkit.

Both setups are fully Pluto-coordinated (real server, real locks, real
messaging). The task has 5 sub-tasks with a dependency DAG that includes
two parallel-eligible entries:

  t-001  src/fractals/mandelbrot.py    deps: []
  t-002  src/fractals/julia.py         deps: []       ← parallel with t-001
  t-003  src/fractals/stats.py         deps: [t-001, t-002]
  t-004  scripts/render_fractals.py    deps: [t-001, t-002, t-003]
  t-005  tests/test_fractals.py        deps: [t-001, t-002, t-003]

Two teams run sequentially on the same real Pluto server:
  (A) Haiku team:  claude-haiku-4.5  as Specialist and Reviewer
  (B) Sonnet team: claude-sonnet-4.5 as Specialist and Reviewer

Both are graded against the identical canonical suite (10 cases).

Output: /tmp/pluto/demo/fractal_compare/
  haiku_team/             ← workspace written by haiku-specialist-1
  sonnet_team/            ← workspace written by sonnet-specialist-1
  trace.json              ← combined event log
  fractal_compare_demo.md ← comparison report

Opt in: PLUTO_RUN_DEMOS=1  (also requires `copilot` on PATH).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from typing import Any, Callable

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "src_py"))
sys.path.insert(0, os.path.join(_REPO, "tests"))

from pluto_client import PlutoHttpClient  # noqa: E402
from pluto_test_server import (  # noqa: E402
    PlutoTestServer, PLUTO_HOST, PLUTO_HTTP_PORT,
)

ROLES_DIR = os.path.join(_REPO, "library", "roles")
PROTOCOL_PATH = os.path.join(_REPO, "library", "protocol.md")
CANONICAL_TESTS = os.path.join(
    os.path.dirname(__file__), "canonical_test_fractals.py"
)

DEMO_NAME = "fractal_compare"
DEMO_ROOT = f"/tmp/pluto/demo/{DEMO_NAME}"
HAIKU_WS = os.path.join(DEMO_ROOT, "haiku_team")
SONNET_WS = os.path.join(DEMO_ROOT, "sonnet_team")

HAIKU_MODEL = os.environ.get("PLUTO_HAIKU_MODEL", "claude-haiku-4.5")
SONNET_MODEL = os.environ.get("PLUTO_SONNET_MODEL", "claude-sonnet-4.5")

RUN_DEMOS = os.environ.get("PLUTO_RUN_DEMOS") == "1"


def have_copilot() -> bool:
    return shutil.which("copilot") is not None


# ── Trace ─────────────────────────────────────────────────────────────────────


class Trace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.t0 = time.time()

    def add(self, actor: str, kind: str, **detail: Any) -> None:
        with self._lock:
            self.events.append({
                "t": round(time.time() - self.t0, 3),
                "actor": actor,
                "kind": kind,
                **detail,
            })

    def dump(self) -> list[dict]:
        with self._lock:
            return list(self.events)


# ── Pluto-bound role agent ────────────────────────────────────────────────────


class RoleAgent(threading.Thread):
    def __init__(self, agent_id: str, host: str, http_port: int, trace: Trace):
        super().__init__(daemon=True, name=f"agent-{agent_id}")
        self.agent_id = agent_id
        self.trace = trace
        self.client = PlutoHttpClient(
            host=host, http_port=http_port, agent_id=agent_id
        )
        self._handlers: dict[str, Callable[[dict], None]] = {}
        self._stop = threading.Event()
        self.ready = threading.Event()

    def on(self, msg_type: str, fn: Callable[[dict], None]) -> None:
        self._handlers[msg_type] = fn

    def send(self, to: str, payload: dict) -> None:
        self.trace.add(
            self.agent_id, "send", to=to, payload_type=payload.get("type")
        )
        self.client.send(to, payload)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        resp = self.client.register()
        if resp.get("status") != "ok":
            self.trace.add(
                self.agent_id, "note", event="register_failed", resp=resp
            )
            return
        self.trace.add(self.agent_id, "note", event="registered")
        self.ready.set()
        try:
            while not self._stop.is_set():
                try:
                    msgs = self.client.long_poll(timeout=2)
                except Exception as exc:  # noqa: BLE001
                    self.trace.add(
                        self.agent_id, "note",
                        event="poll_error", err=repr(exc)
                    )
                    time.sleep(0.3)
                    continue
                for m in msgs:
                    p = m.get("payload", {}) or {}
                    pt = p.get("type")
                    if not pt:
                        continue
                    self.trace.add(
                        self.agent_id, "recv",
                        frm=m.get("from"), payload_type=pt
                    )
                    h = self._handlers.get(pt)
                    if h:
                        try:
                            h(m)
                        except Exception as exc:  # noqa: BLE001
                            self.trace.add(
                                self.agent_id, "note",
                                event="handler_error", err=repr(exc)
                            )
        finally:
            try:
                self.client.unregister()
            except Exception:  # noqa: BLE001
                pass


# ── Copilot wrapper ───────────────────────────────────────────────────────────


def run_copilot(
    prompt: str, *, workdir: str, model: str, timeout: int = 900
) -> tuple[int, str, str, float]:
    cmd = [
        "copilot", "-p", prompt,
        "--model", model,
        "--allow-all-tools", "--allow-all-paths",
        "--add-dir", workdir,
        "--no-ask-user",
    ]
    t0 = time.time()
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, proc.stdout, proc.stderr, time.time() - t0


def load_role(name: str) -> str:
    with open(os.path.join(ROLES_DIR, f"{name}.md"), encoding="utf-8") as f:
        return f.read()


def load_protocol() -> str:
    with open(PROTOCOL_PATH, encoding="utf-8") as f:
        return f.read()


# ── Task definitions ──────────────────────────────────────────────────────────

TASK_OVERVIEW = """\
You are implementing a fractal toolkit across 5 tasks:
  t-001  src/fractals/mandelbrot.py  — Mandelbrot iteration module
  t-002  src/fractals/julia.py       — Julia set iteration module
  t-003  src/fractals/stats.py       — Statistics module for fractal grids
  t-004  scripts/render_fractals.py  — Render both fractals, write stats JSON
  t-005  tests/test_fractals.py      — Pytest suite for all three modules

You will receive ONE task at a time. Implement ONLY the file listed for that
task. Do not pre-emptively create other files."""

MANDELBROT_DOD = (
    "src/fractals/mandelbrot.py must expose:\n"
    "  iterate(c: complex, max_iter: int) -> int\n"
    "    Iterate z -> z**2 + c starting from z=0+0j. Return the count at\n"
    "    which abs(z) exceeds 2.0, or max_iter if it never does.\n"
    "  grid(width, height, x_min, x_max, y_min, y_max, max_iter) -> list[list[int]]\n"
    "    height rows, width cols. Map pixel (col, row) to\n"
    "    c = complex(x_min + col*(x_max-x_min)/(width-1),\n"
    "                y_min + row*(y_max-y_min)/(height-1)).\n"
    "    Return grid of escape times.\n"
    "Stdlib-only. iterate(0+0j, 100) must return 100."
)

JULIA_DOD = (
    "src/fractals/julia.py must expose:\n"
    "  iterate(z: complex, c: complex, max_iter: int) -> int\n"
    "    Iterate z -> z**2 + c (c is a fixed parameter, NOT the pixel).\n"
    "    Return the count at which abs(z) exceeds 2.0, or max_iter.\n"
    "  grid(width, height, x_min, x_max, y_min, y_max, c, max_iter) -> list[list[int]]\n"
    "    height rows, width cols. Map pixel (col, row) to the starting\n"
    "    z = complex(x_min + col*(x_max-x_min)/(width-1),\n"
    "                y_min + row*(y_max-y_min)/(height-1)),\n"
    "    then call iterate(z, c, max_iter).\n"
    "Stdlib-only. iterate(2+2j, -0.7+0.27j, 100) must return < 5."
)

STATS_DOD = (
    "src/fractals/stats.py must expose:\n"
    "  convergence_ratio(grid: list[list[int]], max_iter: int) -> float\n"
    "    Fraction of cells where value == max_iter, in [0.0, 1.0].\n"
    "  mean_escape_time(grid: list[list[int]]) -> float\n"
    "    Arithmetic mean of all cell values.\n"
    "  histogram(grid: list[list[int]], bins: int) -> list[int]\n"
    "    Divide the value range [min_val, max_val] into `bins` equal-width\n"
    "    buckets; return a list of `bins` counts summing to total cell count.\n"
    "Stdlib-only."
)

RENDER_DOD = (
    "scripts/render_fractals.py must, when run from the workspace root:\n"
    "  1. Render a 400x300 Mandelbrot set (x in [-2.5,1.0], y in [-1.2,1.2],\n"
    "     max_iter=80) and save to outputs/mandelbrot.png using Pillow if\n"
    "     available; otherwise write a greyscale PGM file.\n"
    "  2. Render a 400x300 Julia set with c=-0.7+0.27j (x in [-1.5,1.5],\n"
    "     y in [-1.5,1.5], max_iter=80) and save to outputs/julia.png.\n"
    "  3. Write outputs/stats.json with keys: mandelbrot_convergence_ratio,\n"
    "     mandelbrot_mean_escape_time, julia_convergence_ratio,\n"
    "     julia_mean_escape_time (all floats).\n"
    "Uses src.fractals.mandelbrot, src.fractals.julia, src.fractals.stats."
)

TESTS_DOD = (
    "tests/test_fractals.py must contain at least 6 pytest tests:\n"
    "  test_mandelbrot_origin_no_escape  — iterate(0+0j, 100) == 100\n"
    "  test_mandelbrot_far_escapes_fast  — iterate(2+2j, 100) < 5\n"
    "  test_mandelbrot_grid_shape        — grid(8,6,...) has 6 rows of 8\n"
    "  test_julia_iterate                — returns int in [1, max_iter]\n"
    "  test_julia_grid_shape             — grid(8,6,...) has 6 rows of 8\n"
    "  test_stats_convergence_ratio      — convergence_ratio([[100,50],[25,100]],100)==0.5\n"
    "All 6 must pass."
)


def build_task_list(workdir: str) -> list[dict]:
    mb = os.path.join(workdir, "src", "fractals", "mandelbrot.py")
    ju = os.path.join(workdir, "src", "fractals", "julia.py")
    st = os.path.join(workdir, "src", "fractals", "stats.py")
    rn = os.path.join(workdir, "scripts", "render_fractals.py")
    ts = os.path.join(workdir, "tests", "test_fractals.py")
    return [
        {
            "task_id": "t-001",
            "title": "Implement Mandelbrot iteration module",
            "type": "code", "owner": "specialist",
            "files": [f"file:{mb}"],
            "dependencies": [],
            "definition_of_done": MANDELBROT_DOD,
            "verification_hint": (
                "python -c 'from src.fractals.mandelbrot import iterate; "
                "assert iterate(0+0j,100)==100; assert iterate(2+2j,100)<5'"
            ),
        },
        {
            "task_id": "t-002",
            "title": "Implement Julia set iteration module",
            "type": "code", "owner": "specialist",
            "files": [f"file:{ju}"],
            "dependencies": [],
            "definition_of_done": JULIA_DOD,
            "verification_hint": (
                "python -c 'from src.fractals.julia import iterate; "
                "assert iterate(2+2j,-0.7+0.27j,100)<5'"
            ),
        },
        {
            "task_id": "t-003",
            "title": "Implement fractal statistics module",
            "type": "code", "owner": "specialist",
            "files": [f"file:{st}"],
            "dependencies": ["t-001", "t-002"],
            "definition_of_done": STATS_DOD,
            "verification_hint": (
                "python -c 'from src.fractals.stats import convergence_ratio; "
                "assert convergence_ratio([[100,50],[25,100]],100)==0.5'"
            ),
        },
        {
            "task_id": "t-004",
            "title": "Implement render script (Mandelbrot + Julia + stats)",
            "type": "code", "owner": "specialist",
            "files": [f"file:{rn}"],
            "dependencies": ["t-001", "t-002", "t-003"],
            "definition_of_done": RENDER_DOD,
            "verification_hint": "python scripts/render_fractals.py",
        },
        {
            "task_id": "t-005",
            "title": "Implement pytest suite for the fractal toolkit",
            "type": "code", "owner": "specialist",
            "files": [f"file:{ts}"],
            "dependencies": ["t-001", "t-002", "t-003"],
            "definition_of_done": TESTS_DOD,
            "verification_hint": "pytest tests/test_fractals.py -q",
        },
    ]


# ── Specialist ────────────────────────────────────────────────────────────────

SPECIALIST_PROMPT_TMPL = """\
You are a Pluto Code Specialist. Your role and the coordination protocol are
inlined below — do NOT attempt to read them from disk.

=== ROLE: SPECIALIST ===
{role}

=== PROTOCOL ===
{protocol}

=== OVERALL PROJECT ===
{overview}

=== TASK ASSIGNED ===
{task_json}

=== CONSTRAINTS ===
- Workspace root: {workdir}
- Write ONLY the file(s) listed in task.files (the path after 'file:' is absolute).
- Python standard library only (plus Pillow/PIL if available for image output).
- Do NOT invoke pytest or the render script yourself.
- After implementing, respond with exactly one line:
  TASK_DONE <one-sentence summary>
"""


def specialist_handler(
    agent: RoleAgent,
    msg: dict,
    orch_id: str,
    role_text: str,
    protocol_text: str,
    workdir: str,
    model: str,
    metrics: dict,
) -> None:
    payload = msg["payload"]
    task = payload["task"]
    task_id = task["task_id"]

    # Acquire write locks.
    granted_refs: list[str] = []
    for resource in task.get("files", []):
        resp = agent.client.acquire(resource, mode="write", ttl_ms=600_000)
        agent.trace.add(
            agent.agent_id, "lock",
            op="acquire", resource=resource, status=resp.get("status")
        )
        if resp.get("status") == "ok":
            granted_refs.append(resp["lock_ref"])

    # Ensure parent directories exist.
    for resource in task.get("files", []):
        path = resource.removeprefix("file:")
        os.makedirs(os.path.dirname(path), exist_ok=True)

    prompt = SPECIALIST_PROMPT_TMPL.format(
        role=role_text,
        protocol=protocol_text,
        overview=TASK_OVERVIEW,
        task_json=json.dumps(payload, indent=2, default=str),
        workdir=workdir,
    )
    agent.trace.add(
        agent.agent_id, "shell", op="copilot_start",
        task_id=task_id, model=model
    )
    rc, out, err, dur = run_copilot(
        prompt, workdir=workdir, model=model, timeout=900
    )
    metrics.setdefault("calls", []).append({
        "actor": agent.agent_id, "task_id": task_id,
        "model": model, "rc": rc, "duration_s": round(dur, 2),
    })
    agent.trace.add(
        agent.agent_id, "shell", op="copilot_done",
        task_id=task_id, rc=rc, duration_s=round(dur, 2)
    )

    # Release locks.
    for ref in granted_refs:
        try:
            agent.client.release(ref)
            agent.trace.add(agent.agent_id, "lock", op="release", lock_ref=ref)
        except Exception:  # noqa: BLE001
            pass

    files_changed = [
        r for r in task.get("files", [])
        if os.path.isfile(r.removeprefix("file:"))
    ]
    status = "done" if rc == 0 and files_changed else "error"
    agent.send(orch_id, {
        "type": "task_result",
        "task_id": task_id,
        "status": status,
        "summary": (out.splitlines() or ["<no output>"])[-1][:160],
        "details": {"files_changed": files_changed, "copilot_rc": rc,
                    "duration_s": round(dur, 2)},
        "notes": [],
    })


# ── Reviewer ──────────────────────────────────────────────────────────────────

REVIEWER_PROMPT_TMPL = """\
You are a Pluto Reviewer. Your role and protocol are inlined below.

=== ROLE: REVIEWER ===
{role}

=== PROTOCOL ===
{protocol}

=== TASK BEING REVIEWED ===
{task_json}

=== FILES UNDER REVIEW ===
{files_block}

Review the file(s) against the definition_of_done. Be strict but fair.
Respond with EXACTLY ONE JSON object (no markdown fence):
{{
  "verdict": "approved" | "needs_changes",
  "findings": ["short note ...", ...],
  "suggested_fixes": ["short note ...", ...]
}}
"""


def reviewer_handler(
    agent: RoleAgent,
    msg: dict,
    orch_id: str,
    workdir: str,
    role_text: str,
    protocol_text: str,
    model: str,
    metrics: dict,
) -> None:
    payload = msg["payload"]
    task = payload["task"]
    task_id = task["task_id"]

    blocks: list[str] = []
    fast_fail: list[dict] = []
    for resource in task.get("files", []):
        path = resource.removeprefix("file:")
        if not os.path.isfile(path):
            fast_fail.append({"severity": "major", "file": resource,
                               "message": "file missing on disk"})
            continue
        try:
            content = open(path, encoding="utf-8").read()
        except OSError as exc:
            content = f"<unreadable: {exc}>"
        blocks.append(f"--- {path} ---\n{content}\n")

    if fast_fail:
        agent.send(orch_id, {
            "type": "review", "task_id": task_id, "status": "needs_changes",
            "findings": fast_fail, "suggested_fixes": [],
        })
        return

    prompt = REVIEWER_PROMPT_TMPL.format(
        role=role_text,
        protocol=protocol_text,
        task_json=json.dumps(task, indent=2, default=str),
        files_block="\n".join(blocks),
    )
    agent.trace.add(
        agent.agent_id, "shell", op="copilot_start",
        task_id=task_id, model=model
    )
    rc, out, err, dur = run_copilot(
        prompt, workdir=workdir, model=model, timeout=600
    )
    metrics.setdefault("calls", []).append({
        "actor": agent.agent_id, "task_id": task_id,
        "model": model, "rc": rc, "duration_s": round(dur, 2),
    })
    agent.trace.add(
        agent.agent_id, "shell", op="copilot_done",
        task_id=task_id, rc=rc, duration_s=round(dur, 2)
    )

    verdict = "approved"
    findings: list = []
    fixes: list = []
    try:
        s, e = out.find("{"), out.rfind("}")
        if s != -1 and e > s:
            obj = json.loads(out[s:e + 1])
            verdict = obj.get("verdict", "approved")
            findings = obj.get("findings") or []
            fixes = obj.get("suggested_fixes") or []
    except Exception:  # noqa: BLE001
        verdict = "needs_changes"
        findings = [{"severity": "minor",
                     "message": "reviewer output not valid JSON",
                     "raw": out[-300:]}]

    if verdict not in ("approved", "needs_changes"):
        verdict = "needs_changes"

    agent.send(orch_id, {
        "type": "review", "task_id": task_id, "status": verdict,
        "findings": findings, "suggested_fixes": fixes,
    })


# ── QA ────────────────────────────────────────────────────────────────────────


def install_canonical_tests(workdir: str) -> None:
    dst = os.path.join(workdir, "tests", "test_canonical_fractals.py")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(CANONICAL_TESTS, dst)


def qa_run(workdir: str, trace: Trace, actor: str) -> dict:
    install_canonical_tests(workdir)

    # Best-effort: run the render script if present (don't fail QA on it).
    render_script = os.path.join(workdir, "scripts", "render_fractals.py")
    render_rc = -1
    if os.path.isfile(render_script):
        try:
            r = subprocess.run(
                [sys.executable, render_script],
                cwd=workdir, capture_output=True, text=True, timeout=120,
            )
            render_rc = r.returncode
        except Exception:  # noqa: BLE001
            pass

    test_path = os.path.join(workdir, "tests", "test_canonical_fractals.py")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q",
         "--rootdir", workdir, "--tb=short"],
        cwd=workdir, capture_output=True, text=True, timeout=180,
    )
    dur = time.time() - t0
    trace.add(actor, "shell", op="pytest_done",
              rc=proc.returncode, duration_s=round(dur, 2))

    passed = failed = 0
    for line in (proc.stdout + proc.stderr).splitlines():
        if "passed" in line or "failed" in line:
            parts = line.replace(",", " ").split()
            for i, tok in enumerate(parts):
                try:
                    n = int(tok)
                except ValueError:
                    continue
                if i + 1 < len(parts):
                    nxt = parts[i + 1].rstrip(".,")
                    if nxt.startswith("passed"):
                        passed = max(passed, n)
                    elif nxt.startswith("failed"):
                        failed = max(failed, n)

    stats_path = os.path.join(workdir, "outputs", "stats.json")
    output_stats: dict = {}
    if os.path.isfile(stats_path):
        try:
            output_stats = json.load(open(stats_path))
        except Exception:  # noqa: BLE001
            pass

    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "rc": proc.returncode,
        "passed": passed,
        "failed": failed,
        "duration_s": round(dur, 2),
        "render_rc": render_rc,
        "output_stats": output_stats,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-500:],
    }


# ── Orchestrator loop ─────────────────────────────────────────────────────────


def orchestrate(
    orch: RoleAgent,
    specialist_id: str,
    reviewer_id: str,
    tasks: list[dict],
    trace: Trace,
    deadline_s: float = 3600,
) -> dict[str, Any]:
    """Dependency-aware dispatch loop. Returns states + result payloads."""
    states: dict[str, str] = {t["task_id"]: "pending" for t in tasks}
    results: dict[str, dict] = {}
    reviews: dict[str, dict] = {}
    cv = threading.Condition()

    def on_task_result(m: dict) -> None:
        p = m["payload"]
        tid = p["task_id"]
        results[tid] = p
        with cv:
            states[tid] = (
                "review_pending" if p["status"] == "done" else "failed"
            )
            cv.notify_all()

    def on_review(m: dict) -> None:
        p = m["payload"]
        tid = p["task_id"]
        reviews[tid] = p
        with cv:
            states[tid] = (
                "completed" if p["status"] == "approved" else "needs_changes"
            )
            cv.notify_all()

    orch.on("task_result", on_task_result)
    orch.on("review", on_review)

    # Broadcast task list for observability.
    orch.client.broadcast({
        "type": "task_list", "version": 1, "updated_by": orch.agent_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tasks": tasks,
    })
    trace.add(orch.agent_id, "note", event="task_list_broadcast",
              count=len(tasks))

    t_start = time.time()
    terminal = {"completed", "failed", "needs_changes"}

    while time.time() - t_start < deadline_s:
        with cv:
            for t in tasks:
                tid = t["task_id"]
                deps_ok = all(
                    states.get(d) == "completed"
                    for d in t.get("dependencies", [])
                )
                if states[tid] == "pending" and deps_ok:
                    states[tid] = "in_progress"
                    orch.send(specialist_id, {
                        "type": "task_assigned",
                        "task": t,
                        "constraints": [
                            "stdlib-only",
                            "write only the listed file(s)",
                        ],
                        "acceptance_criteria": [t["definition_of_done"]],
                        "verification_hints": [t["verification_hint"]],
                    })
                elif states[tid] == "review_pending":
                    states[tid] = "in_review"
                    orch.send(reviewer_id, {
                        "type": "task_assigned_for_review",
                        "task": t,
                        "result": results.get(tid, {}),
                    })
            cv.wait(timeout=1.0)

        if all(s in terminal for s in states.values()):
            break

    return {"states": states, "results": results, "reviews": reviews}


# ── Drive a full team end-to-end ──────────────────────────────────────────────


def drive_team(
    prefix: str, model: str, workdir: str, trace: Trace
) -> dict[str, Any]:
    """Register all agents, run orchestration + QA. Return combined result."""
    metrics: dict[str, Any] = {"calls": []}
    spec_role = load_role("specialist")
    rev_role = load_role("reviewer")
    protocol_text = load_protocol()

    orch_id = f"{prefix}-orchestrator-1"
    spec_id = f"{prefix}-specialist-1"
    rev_id = f"{prefix}-reviewer-1"
    qa_id = f"{prefix}-qa-1"

    orch = RoleAgent(orch_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)
    spec = RoleAgent(spec_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)
    rev = RoleAgent(rev_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)

    spec.on(
        "task_assigned",
        lambda m: specialist_handler(
            spec, m, orch_id, spec_role, protocol_text, workdir, model, metrics
        ),
    )
    rev.on(
        "task_assigned_for_review",
        lambda m: reviewer_handler(
            rev, m, orch_id, workdir, rev_role, protocol_text, model, metrics
        ),
    )

    tasks = build_task_list(workdir)

    for a in (orch, spec, rev):
        a.start()
    for a in (orch, spec, rev):
        assert a.ready.wait(timeout=15), f"{a.agent_id} failed to register"

    t_start = time.time()
    orch_result = orchestrate(orch, spec_id, rev_id, tasks, trace,
                              deadline_s=3600)
    impl_duration = time.time() - t_start

    for a in (orch, spec, rev):
        a.stop()
    for a in (orch, spec, rev):
        a.join(timeout=5)

    qa = qa_run(workdir, trace, qa_id)

    return {
        "prefix": prefix,
        "model": model,
        "workspace": workdir,
        "states": orch_result["states"],
        "results": orch_result["results"],
        "reviews": orch_result["reviews"],
        "qa": qa,
        "metrics": metrics,
        "impl_duration_s": round(impl_duration, 2),
    }


# ── Workspace setup ───────────────────────────────────────────────────────────


def reset_workspace(workdir: str) -> None:
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    for sub in ("src/fractals", "scripts", "tests", "outputs"):
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)
    for pkg in ("src", "src/fractals", "tests"):
        open(os.path.join(workdir, pkg, "__init__.py"), "w").close()
    # Copy protocol + roles so agents can find them locally if needed.
    shutil.copy(PROTOCOL_PATH, os.path.join(workdir, "PROTOCOL.md"))
    for r in ("specialist", "reviewer"):
        shutil.copy(
            os.path.join(ROLES_DIR, f"{r}.md"),
            os.path.join(workdir, f"ROLE_{r}.md"),
        )


# ── Test class ────────────────────────────────────────────────────────────────


@unittest.skipUnless(RUN_DEMOS, "set PLUTO_RUN_DEMOS=1 to run real-server demos")
@unittest.skipUnless(have_copilot(), "copilot CLI not available on PATH")
class FractalCompareDemo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = PlutoTestServer()
        cls.server.start()
        os.makedirs(DEMO_ROOT, exist_ok=True)
        reset_workspace(HAIKU_WS)
        reset_workspace(SONNET_WS)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_fractal_compare(self):
        trace = Trace()

        haiku_result = drive_team("haiku", HAIKU_MODEL, HAIKU_WS, trace)
        sonnet_result = drive_team("sonnet", SONNET_MODEL, SONNET_WS, trace)

        # Persist trace + results.
        out_json = os.path.join(DEMO_ROOT, "trace.json")
        with open(out_json, "w") as f:
            json.dump(
                {
                    "trace": trace.dump(),
                    "haiku": haiku_result,
                    "sonnet": sonnet_result,
                    "pluto_http_port": PLUTO_HTTP_PORT,
                    "models": {"haiku": HAIKU_MODEL, "sonnet": SONNET_MODEL},
                },
                f, indent=2, default=str,
            )

        # Render report.
        from tests.demo_fractal_compare.write_report import render
        render(out_json, os.path.join(DEMO_ROOT, "fractal_compare_demo.md"))

        # Pass as long as at least one team made copilot calls (infra sanity).
        total_calls = (
            len(haiku_result["metrics"]["calls"])
            + len(sonnet_result["metrics"]["calls"])
        )
        self.assertGreater(total_calls, 0, "no copilot calls made — infra problem")


if __name__ == "__main__":
    unittest.main()
