"""
demo_fractal — multi-role collaboration demo on a Mandelbrot task.

Architecture
============

  Real Pluto server  ←→  4 registered agents (Orchestrator, Specialist,
                          Reviewer, QA), each a Python thread holding an
                          HTTP session token.

  Coordination       Real /locks/acquire + /agents/send + /agents/poll
                     traffic between the four sessions.

  LLM work           The Specialist shells out to ``copilot -p`` (real
                     non-interactive GitHub Copilot CLI calls) to actually
                     write the fractal source files inside the demo
                     workspace.  Other roles run deterministic checks
                     informed by their loaded role.md files; a future
                     extension can route them through copilot too.

  Output             - Real source/test files under
                     /tmp/pluto/demo/fractal_collaboration/
                     - Real Mandelbrot PNG + statistics JSON
                     - A trace JSON + a markdown report rendered into
                     docs/demos/fractal_collaboration_demo.md by
                     ``write_report.py``.

Skipped automatically when:
  * pytest is run without ``--run-demos`` (these talk to real services and
    take minutes).
  * ``copilot`` is not available on $PATH.
"""

from __future__ import annotations

import importlib.util
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
from pluto_test_server import PlutoTestServer  # noqa: E402

ROLES_DIR = os.path.join(_REPO, "library", "roles")
PROTOCOL_PATH = os.path.join(_REPO, "library", "protocol.md")

DEMO_ROOT = "/tmp/pluto/demo/fractal_collaboration"
REPORT_DIR = os.path.join(_REPO, "docs", "demos")

# Skip switch — real demos are slow + external; opt in with env var.
RUN_DEMOS = os.environ.get("PLUTO_RUN_DEMOS") == "1"


# ── Trace ────────────────────────────────────────────────────────────────────


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


# ── Pluto-bound role agent ───────────────────────────────────────────────────


class RoleAgent(threading.Thread):
    """Thin wrapper: register, long-poll, dispatch by payload['type']."""

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
        self.trace.add(self.agent_id, "send", to=to,
                       payload_type=payload.get("type"), payload=payload)
        self.client.send(to, payload)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        resp = self.client.register()
        if resp.get("status") != "ok":
            self.trace.add(self.agent_id, "note",
                           event="register_failed", resp=resp)
            return
        self.trace.add(self.agent_id, "note", event="registered")
        self.ready.set()
        try:
            while not self._stop.is_set():
                try:
                    msgs = self.client.long_poll(timeout=2)
                except Exception as exc:  # noqa: BLE001
                    self.trace.add(self.agent_id, "note",
                                   event="poll_error", err=repr(exc))
                    time.sleep(0.3)
                    continue
                for m in msgs:
                    p = m.get("payload", {})
                    self.trace.add(self.agent_id, "recv",
                                   frm=m.get("from"),
                                   payload_type=p.get("type"),
                                   payload=p)
                    h = self._handlers.get(p.get("type", ""))
                    if h:
                        try:
                            h(m)
                        except Exception as exc:  # noqa: BLE001
                            self.trace.add(self.agent_id, "note",
                                           event="handler_error",
                                           err=repr(exc))
        finally:
            try:
                self.client.unregister()
            except Exception:  # noqa: BLE001
                pass


# ── Copilot helper ───────────────────────────────────────────────────────────


def have_copilot() -> bool:
    return shutil.which("copilot") is not None


def run_copilot(prompt: str, *, workdir: str, model: str | None = None,
                timeout: int = 600) -> tuple[int, str, str]:
    cmd = ["copilot", "-p", prompt, "--allow-all-tools", "--allow-all-paths",
           "--add-dir", workdir]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def load_role(name: str) -> str:
    with open(os.path.join(ROLES_DIR, f"{name}.md"), encoding="utf-8") as f:
        return f.read()


# ── Hardcoded task list (what a role-following Orchestrator would emit) ──────


def build_task_list(workdir: str) -> list[dict]:
    src = os.path.join(workdir, "src", "fractals", "mandelbrot.py")
    runner = os.path.join(workdir, "scripts", "run_mandelbrot.py")
    tests = os.path.join(workdir, "tests", "test_mandelbrot.py")
    return [
        {
            "task_id": "t-001",
            "title": "Implement Mandelbrot iteration module",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{src}"],
            "dependencies": [],
            "definition_of_done": (
                "Module mandelbrot.py exposes iterate(c, max_iter)->int "
                "(escape time) and grid(width, height, x_min, x_max, y_min, "
                "y_max, max_iter)->2D list of escape times. iterate(0+0j, "
                "100) must return 100 (no escape)."
            ),
            "verification_hint":
                "python -c \"from src.fractals.mandelbrot import iterate; "
                "assert iterate(0+0j, 100) == 100; "
                "assert iterate(2+2j, 100) < 5\"",
        },
        {
            "task_id": "t-002",
            "title": "Implement run script: render image + statistics",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{runner}"],
            "dependencies": ["t-001"],
            "definition_of_done": (
                "Script run_mandelbrot.py renders an 800x600 PNG at "
                "outputs/mandelbrot.png AND writes outputs/stats.json with "
                "keys 'convergence_ratio' (float in [0,1]) and "
                "'mean_escape_time' (float). Uses only the standard library "
                "+ Pillow if available; otherwise emits a PGM file."
            ),
            "verification_hint":
                "python scripts/run_mandelbrot.py && "
                "python -c \"import json; s=json.load(open('outputs/"
                "stats.json')); assert 0<=s['convergence_ratio']<=1; "
                "assert s['mean_escape_time']>0\"",
        },
        {
            "task_id": "t-003",
            "title": "Unit tests for iteration + grid invariants",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{tests}"],
            "dependencies": ["t-001"],
            "definition_of_done": (
                "tests/test_mandelbrot.py contains at least 3 pytest tests: "
                "iterate_origin_no_escape, iterate_far_escapes_fast, "
                "grid_shape_matches_request. All pass."
            ),
            "verification_hint":
                "pytest tests/test_mandelbrot.py -q",
        },
    ]


# ── Specialist task handler (real copilot) ───────────────────────────────────


SPECIALIST_PROMPT_TMPL = """You are a Pluto Code Specialist. Your loaded role
is below. You have just received a `task_assigned` message and MUST act on it
exactly per the protocol.

=== ROLE ===
{role}

=== TASK ASSIGNED ===
{task_assigned}

=== CONSTRAINTS FOR THIS DEMO ===
- Workspace root: {workdir}
- Touch ONLY the files listed in task.files (relative paths under the
  workspace root; the file: prefix denotes a file resource).
- Use plain Python; only `import math`, `import json`, `import os`,
  `import sys`, and (only if available) `import PIL` are allowed.
- Do not invoke pytest or any long-running command yourself.
- After implementing the file(s), respond with one line of plain text:
  TASK_DONE <one-sentence summary>

Implement the task now. Edit/create the file(s) directly in this workspace.
"""


def specialist_handle_task(agent: RoleAgent, msg: dict, role_text: str,
                           workdir: str, model: str | None) -> None:
    payload = msg["payload"]
    task = payload["task"]
    task_id = task["task_id"]

    # Acquire write lock on every file in task.files.
    granted_refs: list[str] = []
    for resource in task.get("files", []):
        agent.trace.add(agent.agent_id, "lock",
                        op="acquire_request", resource=resource)
        resp = agent.client.acquire(resource, mode="write", ttl_ms=300_000)
        agent.trace.add(agent.agent_id, "lock", op="acquire_response",
                        resource=resource, resp=resp)
        if resp.get("status") == "ok":
            granted_refs.append(resp["lock_ref"])
        elif resp.get("status") == "wait":
            # In this demo no other agent contends, but be honest if it
            # happens — bail with an error result.
            agent.send("orchestrator-1", {
                "type": "task_result", "task_id": task_id,
                "status": "error",
                "summary": f"lock for {resource} queued; aborting",
                "details": {"wait_ref": resp.get("wait_ref")},
                "notes": [],
            })
            return

    # Make sure the parent directory exists on disk so copilot can write.
    for resource in task.get("files", []):
        path = resource.removeprefix("file:")
        os.makedirs(os.path.dirname(path), exist_ok=True)

    prompt = SPECIALIST_PROMPT_TMPL.format(
        role=role_text,
        task_assigned=json.dumps(payload, indent=2),
        workdir=workdir,
    )
    agent.trace.add(agent.agent_id, "shell", cmd="copilot -p ...",
                    task_id=task_id)
    rc, out, err = run_copilot(prompt, workdir=workdir, model=model,
                               timeout=600)
    agent.trace.add(agent.agent_id, "shell", op="copilot_done",
                    task_id=task_id, rc=rc,
                    stdout_tail=out[-500:], stderr_tail=err[-500:])

    # Release locks regardless of outcome.
    for ref in granted_refs:
        agent.client.release(ref)
        agent.trace.add(agent.agent_id, "release", lock_ref=ref)

    files_changed = [r for r in task.get("files", [])
                     if os.path.isfile(r.removeprefix("file:"))]
    status = "done" if rc == 0 and files_changed else "error"
    agent.send("orchestrator-1", {
        "type": "task_result",
        "task_id": task_id,
        "status": status,
        "summary": (out.splitlines() or ["<no output>"])[-1][:160],
        "details": {"files_changed": files_changed,
                    "copilot_rc": rc},
        "notes": [],
    })


# ── Reviewer (deterministic) ─────────────────────────────────────────────────


def reviewer_handle_review(agent: RoleAgent, msg: dict, workdir: str) -> None:
    payload = msg["payload"]
    task = payload["task"]
    task_id = task["task_id"]
    findings: list[dict] = []

    # Static check: every file in task.files exists and is non-empty.
    for resource in task.get("files", []):
        path = resource.removeprefix("file:")
        if not os.path.isfile(path):
            findings.append({"severity": "major", "file": resource,
                             "message": "file missing"})
            continue
        size = os.path.getsize(path)
        if size == 0:
            findings.append({"severity": "major", "file": resource,
                             "message": "file empty"})

    # Static check: verification_hint is concrete (we expect non-empty).
    if not task.get("verification_hint"):
        findings.append({"severity": "major", "file": "<task>",
                         "message": "no verification_hint"})

    status = "needs_changes" if findings else "approved"
    agent.send("orchestrator-1", {
        "type": "review", "task_id": task_id, "status": status,
        "findings": findings, "suggested_fixes": [],
    })


# ── QA (real pytest) ─────────────────────────────────────────────────────────


def qa_handle_qa(agent: RoleAgent, msg: dict, workdir: str) -> None:
    payload = msg["payload"]
    task_ids = payload.get("scope", {}).get("task_ids", [])
    test_path = os.path.join(workdir, "tests", "test_mandelbrot.py")
    rc, out, err = -1, "", ""
    if os.path.isfile(test_path):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", test_path, "-q",
             "--rootdir", workdir],
            cwd=workdir, capture_output=True, text=True, timeout=120,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    agent.trace.add(agent.agent_id, "shell", op="pytest_done",
                    rc=rc, stdout_tail=out[-1000:],
                    stderr_tail=err[-300:])

    # Parse statistics if present.
    stats_path = os.path.join(workdir, "outputs", "stats.json")
    metrics: dict[str, Any] = {"pytest_rc": rc}
    if os.path.isfile(stats_path):
        try:
            metrics.update(json.load(open(stats_path)))
        except Exception:  # noqa: BLE001
            pass

    status = "pass" if rc == 0 else "fail"
    failed = [] if rc == 0 else [{
        "name": "tests/test_mandelbrot.py",
        "output_tail": (out + "\n" + err)[-800:],
    }]
    agent.send("orchestrator-1", {
        "type": "qa_result",
        "scope": {"task_ids": task_ids, "branch": "v0.2.6"},
        "status": status,
        "failed_checks": failed,
        "metrics": metrics,
        "logs_ref": f"scratch:fractal_collaboration/qa.log",
    })


# ── Orchestrator drive loop ──────────────────────────────────────────────────


def orchestrate(orch: RoleAgent, tasks: list[dict], workdir: str,
                trace: Trace, deadline_s: float = 1200) -> dict[str, Any]:
    """Run the dependency-aware dispatch loop. Returns final task states."""
    states = {t["task_id"]: "pending" for t in tasks}
    by_id = {t["task_id"]: t for t in tasks}
    results: dict[str, dict] = {}
    reviews: dict[str, dict] = {}
    qa_done = threading.Event()
    qa_result_box: dict[str, Any] = {}

    cv = threading.Condition()

    def on_task_result(m):
        p = m["payload"]
        results[p["task_id"]] = p
        with cv:
            states[p["task_id"]] = "review_pending" if p["status"] == "done" \
                                   else "failed"
            cv.notify_all()

    def on_review(m):
        p = m["payload"]
        reviews[p["task_id"]] = p
        with cv:
            states[p["task_id"]] = "completed" if p["status"] == "approved" \
                                   else "needs_changes"
            cv.notify_all()

    def on_qa_result(m):
        qa_result_box.update(m["payload"])
        qa_done.set()

    orch.on("task_result", on_task_result)
    orch.on("review", on_review)
    orch.on("qa_result", on_qa_result)

    # Broadcast the task list (pure observability event).
    orch.client.broadcast({
        "type": "task_list", "version": 1, "updated_by": orch.agent_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tasks": tasks,
    })
    trace.add(orch.agent_id, "note", event="task_list_broadcast",
              count=len(tasks))

    # Dispatch loop.
    t_start = time.time()
    while time.time() - t_start < deadline_s:
        progressed = False
        with cv:
            for t in tasks:
                tid = t["task_id"]
                deps_ok = all(states[d] == "completed"
                              for d in t.get("dependencies", []))
                if states[tid] == "pending" and deps_ok:
                    states[tid] = "in_progress"
                    progressed = True
                    payload = {
                        "type": "task_assigned",
                        "task": t,
                        "constraints": [
                            "no writes outside task.files",
                            "use only stdlib + optional Pillow",
                        ],
                        "acceptance_criteria": [t["definition_of_done"]],
                        "verification_hints": [t["verification_hint"]],
                    }
                    orch.send("specialist-1", payload)
                elif states[tid] == "review_pending":
                    states[tid] = "in_review"
                    progressed = True
                    orch.send("reviewer-1", {
                        "type": "task_assigned_for_review",
                        "task": t,
                        "result": results[tid],
                    })
            cv.wait(timeout=1.0)

        # Are all tasks completed?
        if all(s == "completed" for s in states.values()):
            break
        if any(s in ("failed", "needs_changes") for s in states.values()):
            # In this minimal demo we abort on first failure.
            trace.add(orch.agent_id, "note", event="aborting_on_failure",
                      states=dict(states))
            break

    # Kick off QA over completed tasks.
    completed_ids = [tid for tid, s in states.items() if s == "completed"]
    if completed_ids:
        orch.send("qa-1", {
            "type": "qa_request",
            "scope": {"task_ids": completed_ids, "branch": "v0.2.6"},
        })
        qa_done.wait(timeout=180)

    return {
        "states": states, "results": results, "reviews": reviews,
        "qa": qa_result_box,
    }


# ── Reviewer wires a tiny adapter for the orchestrator's custom msg type ─────


def reviewer_adapter(agent: RoleAgent, workdir: str) -> Callable[[dict], None]:
    def handler(msg: dict) -> None:
        # Repackage to the standard review handler.
        reviewer_handle_review(agent, msg, workdir)
    return handler


def qa_adapter(agent: RoleAgent, workdir: str) -> Callable[[dict], None]:
    def handler(msg: dict) -> None:
        qa_handle_qa(agent, msg, workdir)
    return handler


# ── The actual unittest test ─────────────────────────────────────────────────


@unittest.skipUnless(RUN_DEMOS, "set PLUTO_RUN_DEMOS=1 to run real-server demos")
@unittest.skipUnless(have_copilot(), "copilot CLI not available on PATH")
class FractalCollaborationDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = PlutoTestServer()
        cls.server.start()
        # Reset workspace.
        if os.path.exists(DEMO_ROOT):
            shutil.rmtree(DEMO_ROOT)
        os.makedirs(DEMO_ROOT, exist_ok=True)
        os.makedirs(os.path.join(DEMO_ROOT, "src", "fractals"), exist_ok=True)
        os.makedirs(os.path.join(DEMO_ROOT, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(DEMO_ROOT, "tests"), exist_ok=True)
        os.makedirs(os.path.join(DEMO_ROOT, "outputs"), exist_ok=True)
        # Marker file so copilot recognises it as a workspace root.
        open(os.path.join(DEMO_ROOT, "src", "fractals", "__init__.py"), "w").close()
        open(os.path.join(DEMO_ROOT, "src", "__init__.py"), "w").close()
        open(os.path.join(DEMO_ROOT, "tests", "__init__.py"), "w").close()
        # Copy the protocol + role files into the workspace so any role-aware
        # tool the agent uses can read them locally.
        shutil.copy(PROTOCOL_PATH, os.path.join(DEMO_ROOT, "PROTOCOL.md"))
        for r in ("orchestrator", "specialist", "reviewer", "qa"):
            shutil.copy(os.path.join(ROLES_DIR, f"{r}.md"),
                        os.path.join(DEMO_ROOT, f"ROLE_{r}.md"))

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_fractal_collaboration(self):
        trace = Trace()
        host, port = "127.0.0.1", 9001  # HTTP port

        orch = RoleAgent("orchestrator-1", host, port, trace)
        spec = RoleAgent("specialist-1", host, port, trace)
        rev = RoleAgent("reviewer-1", host, port, trace)
        qa = RoleAgent("qa-1", host, port, trace)

        spec_role = load_role("specialist")
        spec.on("task_assigned",
                lambda m: specialist_handle_task(spec, m, spec_role,
                                                 DEMO_ROOT, model=None))
        rev.on("task_assigned_for_review", reviewer_adapter(rev, DEMO_ROOT))
        qa.on("qa_request", qa_adapter(qa, DEMO_ROOT))

        for a in (orch, spec, rev, qa):
            a.start()
        for a in (orch, spec, rev, qa):
            self.assertTrue(a.ready.wait(timeout=10),
                            f"{a.agent_id} did not register")

        tasks = build_task_list(DEMO_ROOT)
        result = orchestrate(orch, tasks, DEMO_ROOT, trace, deadline_s=1500)

        for a in (orch, spec, rev, qa):
            a.stop()
        for a in (orch, spec, rev, qa):
            a.join(timeout=5)

        # Persist trace + result for the report writer.
        os.makedirs(REPORT_DIR, exist_ok=True)
        out_dir = os.path.join(REPORT_DIR, "fractal_collaboration")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "trace.json"), "w") as f:
            json.dump({"trace": trace.dump(), "result": result,
                       "tasks": tasks, "workspace": DEMO_ROOT}, f, indent=2,
                      default=str)

        # Render the human report.
        from tests.demo_fractal.write_report import render
        render(os.path.join(out_dir, "trace.json"),
               os.path.join(REPORT_DIR, "fractal_collaboration_demo.md"))

        # Demo passes if all tasks reached completion AND QA is pass.
        self.assertEqual(set(result["states"].values()), {"completed"},
                         msg=f"states={result['states']}")
        self.assertEqual(result["qa"].get("status"), "pass",
                         msg=f"qa={result['qa']}")


if __name__ == "__main__":
    # Allow running directly: PLUTO_RUN_DEMOS=1 python -m tests.demo_fractal.test_fractal_collaboration
    unittest.main()
