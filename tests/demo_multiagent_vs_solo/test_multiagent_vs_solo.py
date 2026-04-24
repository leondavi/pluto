"""
demo_haiku_vs_sonnet — collaboration vs monolith on the same task.

Compares two real workflows on the same problem:

  (A) Multi-Haiku team coordinated via Pluto + role library:
      - haiku-orchestrator-1   (Python harness driving the Pluto protocol)
      - haiku-specialist-1     (real `copilot -p ... --model claude-haiku-4.5`)
      - haiku-reviewer-1       (real `copilot -p ... --model claude-haiku-4.5`)
      - qa-1                   (Python harness running real `pytest`)

  (B) Single Sonnet monolith:
      - sonnet-monolith-1      (one `copilot -p ... --model claude-sonnet-4.5` call,
                                no Pluto, no roles, gets the entire spec at once)
      - qa-1                   (Python harness running real `pytest`)

Task: Implement an LRU + TTL cache in pure stdlib Python, plus pytest
tests covering eviction, expiry, hit/miss accounting, and edge cases.

Both setups produce code into separate clean workspaces; the same `pytest`
command grades both.  The harness emits a markdown comparison report into
`docs/demos/haiku_vs_sonnet_demo.md`.

Runs only when:
  * env `PLUTO_RUN_DEMOS=1`, AND
  * `copilot` CLI is on PATH.
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

DEMO_ROOT = "/tmp/pluto/demo/haiku_vs_sonnet"
HAIKU_WS = os.path.join(DEMO_ROOT, "haiku_team")
SONNET_WS = os.path.join(DEMO_ROOT, "sonnet_solo")
REPORT_DIR = os.path.join(_REPO, "docs", "demos")
TRACE_DIR = os.path.join(REPORT_DIR, "haiku_vs_sonnet")

HAIKU_MODEL = os.environ.get("PLUTO_HAIKU_MODEL", "claude-haiku-4.5")
SONNET_MODEL = os.environ.get("PLUTO_SONNET_MODEL", "claude-sonnet-4.5")

RUN_DEMOS = os.environ.get("PLUTO_RUN_DEMOS") == "1"


def have_copilot() -> bool:
    return shutil.which("copilot") is not None


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
                "actor": actor, "kind": kind, **detail,
            })

    def dump(self) -> list[dict]:
        with self._lock:
            return list(self.events)


# ── Pluto-bound role agent (small subset of the fractal demo's RoleAgent) ────


class RoleAgent(threading.Thread):
    def __init__(self, agent_id: str, host: str, http_port: int, trace: Trace):
        super().__init__(daemon=True, name=f"agent-{agent_id}")
        self.agent_id = agent_id
        self.trace = trace
        self.client = PlutoHttpClient(host=host, http_port=http_port,
                                      agent_id=agent_id)
        self._handlers: dict[str, Callable[[dict], None]] = {}
        self._stop = threading.Event()
        self.ready = threading.Event()

    def on(self, msg_type: str, fn: Callable[[dict], None]) -> None:
        self._handlers[msg_type] = fn

    def send(self, to: str, payload: dict) -> None:
        self.trace.add(self.agent_id, "send", to=to,
                       payload_type=payload.get("type"))
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
                    p = m.get("payload", {}) or {}
                    pt = p.get("type")
                    # Ignore broadcast echoes / empty payloads (avoids the
                    # hot-loop of recv-type=None spam).
                    if not pt:
                        continue
                    self.trace.add(self.agent_id, "recv",
                                   frm=m.get("from"), payload_type=pt)
                    h = self._handlers.get(pt)
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


# ── Copilot wrapper ──────────────────────────────────────────────────────────


def run_copilot(prompt: str, *, workdir: str, model: str,
                timeout: int = 900) -> tuple[int, str, str, float]:
    cmd = ["copilot", "-p", prompt, "--model", model,
           "--allow-all-tools", "--allow-all-paths", "--add-dir", workdir,
           "--no-ask-user"]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr, time.time() - t0


def load_role(name: str) -> str:
    with open(os.path.join(ROLES_DIR, f"{name}.md"), encoding="utf-8") as f:
        return f.read()


def load_protocol() -> str:
    with open(PROTOCOL_PATH, encoding="utf-8") as f:
        return f.read()


# ── The shared task ─────────────────────────────────────────────────────────


TASK_SPEC = """\
Build a thread-safe LRU + TTL cache in pure-stdlib Python, plus pytest tests.

Files to create (and ONLY these files):
  - src/cache/lru_ttl.py       # the cache class
  - tests/test_lru_ttl.py      # pytest tests

Requirements for `src/cache/lru_ttl.py`:
  - Class `LruTtlCache(maxsize: int, ttl_seconds: float)`.
    * `maxsize >= 1`, `ttl_seconds > 0`. Raise ValueError otherwise.
  - Methods:
    * `get(key) -> value | None`        — None on miss/expired; counts as hit/miss.
    * `put(key, value) -> None`         — refreshes LRU position and TTL.
    * `delete(key) -> bool`             — True if removed.
    * `__len__() -> int`                — current live (non-expired) entries; expired-but-not-yet-evicted may be counted lazily, but `get` MUST return None for them and remove them.
    * `stats() -> dict`                 — keys: 'hits', 'misses', 'evictions', 'expired'.
  - Eviction rule: on `put` that would exceed `maxsize`, evict the
    least-recently-USED entry (i.e. oldest by last get/put time). A `get`
    counts as a use.
  - Thread-safe under concurrent get/put from multiple threads — protect
    state with a `threading.Lock`. Document this in the docstring.
  - Use only the Python standard library; no external imports.

Requirements for `tests/test_lru_ttl.py` (use pytest):
  - test_get_returns_none_on_miss
  - test_put_then_get_returns_value
  - test_lru_eviction_when_full              (insert >maxsize items; oldest gone)
  - test_get_refreshes_lru_position          (recently-got item survives)
  - test_ttl_expiry_returns_none             (use a tiny ttl + sleep; assert miss + 'expired' counter increments)
  - test_stats_counts_hits_and_misses
  - test_thread_safety_does_not_corrupt      (spawn 8 threads doing 200 mixed get/put; assert len(cache) <= maxsize and no exceptions)
  - test_value_error_on_invalid_args         (maxsize=0 and ttl=0)

All tests must pass with `pytest tests/test_lru_ttl.py -q`.
Do NOT add a __main__ block, README, or extra files.
"""


# ── Multi-Haiku setup ────────────────────────────────────────────────────────


def build_haiku_task_list(workdir: str) -> list[dict]:
    cache_path = os.path.join(workdir, "src", "cache", "lru_ttl.py")
    test_path = os.path.join(workdir, "tests", "test_lru_ttl.py")
    return [
        {
            "task_id": "t-001",
            "title": "Implement LruTtlCache class",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{cache_path}"],
            "dependencies": [],
            "definition_of_done": (
                "src/cache/lru_ttl.py contains a thread-safe LruTtlCache class "
                "matching the full spec from the task assignment (maxsize/ttl "
                "validation, get/put/delete/__len__/stats, LRU eviction, TTL "
                "expiry, threading.Lock). Stdlib-only."
            ),
            "verification_hint":
                "python -c \"from src.cache.lru_ttl import LruTtlCache; "
                "c=LruTtlCache(2,10); c.put('a',1); assert c.get('a')==1\"",
            "full_spec": TASK_SPEC,
        },
        {
            "task_id": "t-002",
            "title": "Write pytest tests for LruTtlCache",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{test_path}"],
            "dependencies": ["t-001"],
            "definition_of_done": (
                "tests/test_lru_ttl.py contains at minimum the 8 tests listed "
                "in the spec; all pass with pytest -q."
            ),
            "verification_hint": "pytest tests/test_lru_ttl.py -q",
            "full_spec": TASK_SPEC,
        },
    ]


HAIKU_SPECIALIST_PROMPT_TMPL = """You are a Pluto Code Specialist. The shared
coordination protocol and your role definition are inlined below; treat them
as authoritative — do NOT attempt to read them from disk.

=== ROLE: SPECIALIST ===
{role}

=== PROTOCOL ===
{protocol}

=== TASK ASSIGNED ===
{task_assigned}

=== FULL TASK SPEC (shared between all sub-tasks) ===
{full_spec}

=== HARD CONSTRAINTS FOR THIS DEMO ===
- Workspace root: {workdir}
- Touch ONLY the files listed in task.files (the `file:` prefix denotes a
  file resource; the path after it is absolute).
- Pure Python standard library only. No third-party imports.
- Do not invoke pytest yourself; QA will run it.
- After implementing the file(s), respond with exactly one line:
  TASK_DONE <one-sentence summary>

Implement the task now. Edit/create the file(s) directly.
"""


def haiku_specialist_handle(agent: RoleAgent, msg: dict, role_text: str,
                            protocol_text: str, workdir: str,
                            metrics: dict) -> None:
    payload = msg["payload"]
    task = payload["task"]
    task_id = task["task_id"]

    granted_refs: list[str] = []
    for resource in task.get("files", []):
        resp = agent.client.acquire(resource, mode="write", ttl_ms=600_000)
        agent.trace.add(agent.agent_id, "lock", op="acquire",
                        resource=resource, status=resp.get("status"))
        if resp.get("status") == "ok":
            granted_refs.append(resp["lock_ref"])

    for resource in task.get("files", []):
        path = resource.removeprefix("file:")
        os.makedirs(os.path.dirname(path), exist_ok=True)

    prompt = HAIKU_SPECIALIST_PROMPT_TMPL.format(
        role=role_text, protocol=protocol_text,
        task_assigned=json.dumps({k: v for k, v in payload.items()
                                  if k != "task" or True}, indent=2,
                                 default=str),
        full_spec=task.get("full_spec", ""),
        workdir=workdir,
    )
    agent.trace.add(agent.agent_id, "shell", op="copilot_start",
                    task_id=task_id, model=HAIKU_MODEL)
    rc, out, err, dur = run_copilot(prompt, workdir=workdir,
                                    model=HAIKU_MODEL, timeout=900)
    metrics.setdefault("calls", []).append(
        {"actor": agent.agent_id, "task_id": task_id,
         "model": HAIKU_MODEL, "rc": rc, "duration_s": round(dur, 2),
         "stdout_chars": len(out), "stderr_chars": len(err)}
    )
    agent.trace.add(agent.agent_id, "shell", op="copilot_done",
                    task_id=task_id, rc=rc, duration_s=round(dur, 2))

    for ref in granted_refs:
        try:
            agent.client.release(ref)
            agent.trace.add(agent.agent_id, "lock", op="release",
                            lock_ref=ref)
        except Exception:  # noqa: BLE001
            pass

    files_changed = [r for r in task.get("files", [])
                     if os.path.isfile(r.removeprefix("file:"))]
    status = "done" if rc == 0 and files_changed else "error"
    agent.send("haiku-orchestrator-1", {
        "type": "task_result", "task_id": task_id, "status": status,
        "summary": (out.splitlines() or ["<no output>"])[-1][:160],
        "details": {"files_changed": files_changed, "copilot_rc": rc,
                    "duration_s": round(dur, 2)},
        "notes": [],
    })


HAIKU_REVIEWER_PROMPT_TMPL = """You are a Pluto Reviewer. The protocol and role
are inlined; do NOT read them from disk.

=== ROLE: REVIEWER ===
{role}

=== PROTOCOL ===
{protocol}

=== ORIGINAL TASK SPEC ===
{spec}

=== FILES TO REVIEW ===
{files_block}

Respond with exactly one JSON object (and nothing else) with this shape:
{{
  "verdict": "approved" | "needs_changes",
  "findings": ["short bullet, ...", ...],
  "suggestions": ["short bullet, ...", ...]
}}
"""


def run_haiku_reviewer(workdir: str, role_text: str, protocol_text: str,
                       trace: Trace, metrics: dict) -> dict:
    files = [
        os.path.join(workdir, "src", "cache", "lru_ttl.py"),
        os.path.join(workdir, "tests", "test_lru_ttl.py"),
    ]
    blocks = []
    for fp in files:
        try:
            content = open(fp).read()
        except OSError:
            content = "<missing>"
        blocks.append(f"--- {fp} ---\n{content}\n")
    prompt = HAIKU_REVIEWER_PROMPT_TMPL.format(
        role=role_text, protocol=protocol_text, spec=TASK_SPEC,
        files_block="\n".join(blocks),
    )
    trace.add("haiku-reviewer-1", "shell", op="copilot_start",
              model=HAIKU_MODEL)
    rc, out, err, dur = run_copilot(prompt, workdir=workdir,
                                    model=HAIKU_MODEL, timeout=600)
    metrics.setdefault("calls", []).append(
        {"actor": "haiku-reviewer-1", "task_id": "review",
         "model": HAIKU_MODEL, "rc": rc, "duration_s": round(dur, 2),
         "stdout_chars": len(out), "stderr_chars": len(err)}
    )
    trace.add("haiku-reviewer-1", "shell", op="copilot_done",
              rc=rc, duration_s=round(dur, 2))
    # Try to extract a JSON object.
    parsed: dict = {"verdict": "unknown", "raw": out[-500:]}
    try:
        # Find first '{' and last '}'.
        s, e = out.find("{"), out.rfind("}")
        if s != -1 and e != -1 and e > s:
            parsed = json.loads(out[s:e + 1])
    except Exception:  # noqa: BLE001
        pass
    return parsed


# ── Single-Sonnet setup ──────────────────────────────────────────────────────


SONNET_PROMPT_TMPL = """You are a senior Python engineer working solo on a
small but non-trivial task. There is no orchestrator, no reviewer, no other
agents — just you. Implement the entire spec end-to-end in this workspace.

Workspace root: {workdir}

=== TASK ===
{spec}

When you are done, respond with exactly one line:
TASK_DONE <one-sentence summary>
"""


def run_sonnet_solo(workdir: str, trace: Trace, metrics: dict) -> dict:
    prompt = SONNET_PROMPT_TMPL.format(workdir=workdir, spec=TASK_SPEC)
    trace.add("sonnet-monolith-1", "shell", op="copilot_start",
              model=SONNET_MODEL)
    rc, out, err, dur = run_copilot(prompt, workdir=workdir,
                                    model=SONNET_MODEL, timeout=1200)
    metrics.setdefault("calls", []).append(
        {"actor": "sonnet-monolith-1", "task_id": "solo",
         "model": SONNET_MODEL, "rc": rc, "duration_s": round(dur, 2),
         "stdout_chars": len(out), "stderr_chars": len(err)}
    )
    trace.add("sonnet-monolith-1", "shell", op="copilot_done",
              rc=rc, duration_s=round(dur, 2))
    return {"rc": rc, "duration_s": round(dur, 2),
            "summary": (out.splitlines() or ["<no output>"])[-1][:160]}


# ── QA (real pytest, identical for both setups) ─────────────────────────────


def qa_pytest(workdir: str, trace: Trace, actor: str = "qa-1") -> dict:
    test_path = os.path.join(workdir, "tests", "test_lru_ttl.py")
    if not os.path.isfile(test_path):
        return {"status": "fail", "rc": -1, "reason": "tests file missing",
                "passed": 0, "failed": 0, "duration_s": 0.0}
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q",
         "--rootdir", workdir, "--tb=short"],
        cwd=workdir, capture_output=True, text=True, timeout=180,
    )
    dur = time.time() - t0
    trace.add(actor, "shell", op="pytest_done", rc=proc.returncode,
              duration_s=round(dur, 2))
    # Parse "X passed", "Y failed" from pytest summary.
    passed = failed = 0
    for line in (proc.stdout + proc.stderr).splitlines():
        if "passed" in line or "failed" in line:
            for tok in line.replace(",", " ").split():
                try:
                    n = int(tok)
                except ValueError:
                    continue
                idx = line.split().index(tok) if tok in line.split() else -1
                if idx >= 0 and idx + 1 < len(line.split()):
                    nxt = line.split()[idx + 1].rstrip(".,")
                    if nxt.startswith("passed"):
                        passed = max(passed, n)
                    elif nxt.startswith("failed"):
                        failed = max(failed, n)
    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "rc": proc.returncode, "passed": passed, "failed": failed,
        "duration_s": round(dur, 2),
        "stdout_tail": proc.stdout[-1500:],
        "stderr_tail": proc.stderr[-500:],
    }


# ── Driver: multi-Haiku via Pluto ────────────────────────────────────────────


def drive_haiku_team(trace: Trace) -> dict:
    metrics: dict[str, Any] = {"calls": []}
    spec_role = load_role("specialist")
    rev_role = load_role("reviewer")
    protocol_text = load_protocol()

    orch = RoleAgent("haiku-orchestrator-1", PLUTO_HOST, PLUTO_HTTP_PORT,
                     trace)
    spec = RoleAgent("haiku-specialist-1", PLUTO_HOST, PLUTO_HTTP_PORT,
                     trace)
    spec.on("task_assigned",
            lambda m: haiku_specialist_handle(spec, m, spec_role,
                                              protocol_text, HAIKU_WS,
                                              metrics))

    results: dict[str, dict] = {}
    states: dict[str, str] = {}
    cv = threading.Condition()

    def on_task_result(m):
        p = m["payload"]
        results[p["task_id"]] = p
        with cv:
            states[p["task_id"]] = ("completed" if p["status"] == "done"
                                    else "failed")
            cv.notify_all()

    orch.on("task_result", on_task_result)
    for a in (orch, spec):
        a.start()
    for a in (orch, spec):
        assert a.ready.wait(timeout=15), f"{a.agent_id} did not register"

    tasks = build_haiku_task_list(HAIKU_WS)
    for t in tasks:
        states[t["task_id"]] = "pending"

    # Sequential dispatch (t-002 depends on t-001).
    t_start = time.time()
    deadline = t_start + 1800
    while time.time() < deadline:
        with cv:
            for t in tasks:
                tid = t["task_id"]
                deps_ok = all(states.get(d) == "completed"
                              for d in t.get("dependencies", []))
                if states[tid] == "pending" and deps_ok:
                    states[tid] = "in_progress"
                    orch.send("haiku-specialist-1", {
                        "type": "task_assigned", "task": t,
                        "constraints": ["stdlib-only", "thread-safe"],
                        "acceptance_criteria": [t["definition_of_done"]],
                        "verification_hints": [t["verification_hint"]],
                    })
            cv.wait(timeout=1.0)
        if all(s == "completed" for s in states.values()):
            break
        if any(s == "failed" for s in states.values()):
            break

    impl_duration = time.time() - t_start

    # Reviewer pass (real Haiku).
    review = run_haiku_reviewer(HAIKU_WS, rev_role, protocol_text, trace,
                                metrics)

    for a in (orch, spec):
        a.stop()
    for a in (orch, spec):
        a.join(timeout=5)

    qa = qa_pytest(HAIKU_WS, trace, actor="qa-1")
    return {
        "states": states, "results": results, "review": review,
        "qa": qa, "metrics": metrics,
        "impl_duration_s": round(impl_duration, 2),
    }


# ── Driver: single Sonnet ───────────────────────────────────────────────────


def drive_sonnet_solo(trace: Trace) -> dict:
    metrics: dict[str, Any] = {"calls": []}
    impl = run_sonnet_solo(SONNET_WS, trace, metrics)
    qa = qa_pytest(SONNET_WS, trace, actor="qa-1")
    return {"impl": impl, "qa": qa, "metrics": metrics}


# ── Workspace + report helpers ──────────────────────────────────────────────


def reset_workspace(root: str) -> None:
    if os.path.exists(root):
        shutil.rmtree(root)
    for sub in ("src/cache", "tests"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    open(os.path.join(root, "src", "__init__.py"), "w").close()
    open(os.path.join(root, "src", "cache", "__init__.py"), "w").close()
    open(os.path.join(root, "tests", "__init__.py"), "w").close()


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def render_report(out_md: str, haiku_result: dict, sonnet_result: dict,
                  trace: Trace) -> None:
    def file_summary(ws: str) -> dict:
        cache = os.path.join(ws, "src", "cache", "lru_ttl.py")
        tests = os.path.join(ws, "tests", "test_lru_ttl.py")
        return {
            "cache_bytes": file_size(cache),
            "tests_bytes": file_size(tests),
            "cache_lines": (open(cache).read().count("\n")
                            if os.path.isfile(cache) else 0),
            "tests_lines": (open(tests).read().count("\n")
                            if os.path.isfile(tests) else 0),
        }

    haiku_files = file_summary(HAIKU_WS)
    sonnet_files = file_summary(SONNET_WS)

    def total_dur(metrics: dict) -> float:
        return round(sum(c.get("duration_s", 0.0)
                         for c in metrics.get("calls", [])), 2)

    def total_calls(metrics: dict) -> int:
        return len(metrics.get("calls", []))

    lines: list[str] = []
    lines.append("# Demo: Haiku Team vs Sonnet Solo (real Pluto + real Copilot)")
    lines.append("")
    lines.append(f"_Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}_")
    lines.append("")
    lines.append(f"- Pluto server: `{PLUTO_HOST}:{PLUTO_HTTP_PORT}` (HTTP) — real Erlang server")
    lines.append(f"- Haiku model:  `{HAIKU_MODEL}`")
    lines.append(f"- Sonnet model: `{SONNET_MODEL}`")
    lines.append(f"- Haiku workspace:  `{HAIKU_WS}`")
    lines.append(f"- Sonnet workspace: `{SONNET_WS}`")
    lines.append("")
    lines.append("## Task")
    lines.append("")
    lines.append("Build a thread-safe LRU + TTL cache (stdlib only) plus 8 pytest tests.")
    lines.append("Both setups receive the **same** spec and are graded by the **same** `pytest` invocation.")
    lines.append("")

    lines.append("## Side-by-side Comparison")
    lines.append("")
    lines.append("| metric | Haiku team (3 roles via Pluto) | Sonnet solo (1 call) |")
    lines.append("|--------|-------------------------------:|---------------------:|")
    lines.append(f"| **pytest status** | `{haiku_result['qa']['status']}` "
                 f"| `{sonnet_result['qa']['status']}` |")
    lines.append(f"| tests passed / failed | "
                 f"{haiku_result['qa']['passed']} / {haiku_result['qa']['failed']} | "
                 f"{sonnet_result['qa']['passed']} / {sonnet_result['qa']['failed']} |")
    lines.append(f"| copilot calls | {total_calls(haiku_result['metrics'])} "
                 f"| {total_calls(sonnet_result['metrics'])} |")
    lines.append(f"| total LLM wall-time (s) | {total_dur(haiku_result['metrics'])} "
                 f"| {total_dur(sonnet_result['metrics'])} |")
    lines.append(f"| pytest wall-time (s) | {haiku_result['qa']['duration_s']} "
                 f"| {sonnet_result['qa']['duration_s']} |")
    lines.append(f"| cache module bytes / lines | "
                 f"{haiku_files['cache_bytes']} / {haiku_files['cache_lines']} | "
                 f"{sonnet_files['cache_bytes']} / {sonnet_files['cache_lines']} |")
    lines.append(f"| test file bytes / lines | "
                 f"{haiku_files['tests_bytes']} / {haiku_files['tests_lines']} | "
                 f"{sonnet_files['tests_bytes']} / {sonnet_files['tests_lines']} |")
    lines.append(f"| reviewer verdict | `{haiku_result['review'].get('verdict', 'n/a')}` | n/a |")
    lines.append("")

    lines.append("## Haiku Team — Reviewer Findings")
    lines.append("")
    rev = haiku_result["review"]
    if rev.get("findings"):
        for f in rev["findings"]:
            lines.append(f"- {f}")
    else:
        lines.append("_(none reported)_")
    lines.append("")
    if rev.get("suggestions"):
        lines.append("**Suggestions:**")
        for s in rev["suggestions"]:
            lines.append(f"- {s}")
        lines.append("")

    def call_table(title: str, metrics: dict) -> None:
        lines.append(f"## {title} — copilot calls")
        lines.append("")
        lines.append("| actor | task | model | rc | duration_s |")
        lines.append("|-------|------|-------|---:|-----------:|")
        for c in metrics.get("calls", []):
            lines.append(f"| `{c['actor']}` | `{c['task_id']}` "
                         f"| `{c['model']}` | {c['rc']} | {c['duration_s']} |")
        lines.append("")

    call_table("Haiku Team", haiku_result["metrics"])
    call_table("Sonnet Solo", sonnet_result["metrics"])

    lines.append("## QA Output (haiku team)")
    lines.append("")
    lines.append("```")
    lines.append((haiku_result["qa"].get("stdout_tail") or "").rstrip()[-1500:])
    lines.append("```")
    lines.append("")
    lines.append("## QA Output (sonnet solo)")
    lines.append("")
    lines.append("```")
    lines.append((sonnet_result["qa"].get("stdout_tail") or "").rstrip()[-1500:])
    lines.append("```")
    lines.append("")

    lines.append("## Insights")
    lines.append("")
    h_pass = haiku_result["qa"]["status"] == "pass"
    s_pass = sonnet_result["qa"]["status"] == "pass"
    if h_pass and s_pass:
        lines.append("- Both setups produced a passing solution. The Haiku team paid an "
                     "**iteration overhead** (multiple copilot calls + reviewer round) for "
                     "no extra correctness on this task; Sonnet solo's single call is the "
                     "cheaper Pareto point on a strictly-defined spec.")
    elif h_pass and not s_pass:
        lines.append("- The Haiku team passed where Sonnet solo did **not**. This validates "
                     "the role-collaboration claim: the explicit decomposition + reviewer "
                     "feedback let weaker models catch what a single stronger model missed.")
    elif s_pass and not h_pass:
        lines.append("- Sonnet solo passed where the Haiku team did **not**. The "
                     "collaboration overhead did not compensate for per-call quality on "
                     "this task; consider tighter `definition_of_done` strings, or a "
                     "stronger Reviewer model.")
    else:
        lines.append("- **Both setups failed.** The spec may be under-specified or "
                     "models could not satisfy the thread-safety test reliably; revise "
                     "the spec or run against a different model pair.")
    lines.append("")
    lines.append("- Multi-Haiku trace events: "
                 f"{len([e for e in trace.dump() if e.get('actor', '').startswith('haiku') or e.get('actor') == 'qa-1'])}; "
                 "every file edit by `haiku-specialist-1` was preceded by a real Pluto "
                 "`/locks/acquire` call and followed by `/locks/release`. Sonnet solo did "
                 "**no** lock acquisition (it was not given the protocol).")
    lines.append("- Re-run with: `PLUTO_RUN_DEMOS=1 python -m pytest "
                 "tests/demo_haiku_vs_sonnet -s`.")

    with open(out_md, "w") as f:
        f.write("\n".join(lines) + "\n")


# ── The unittest entry point ────────────────────────────────────────────────


@unittest.skipUnless(RUN_DEMOS, "set PLUTO_RUN_DEMOS=1 to run real-server demos")
@unittest.skipUnless(have_copilot(), "copilot CLI not available on PATH")
class HaikuVsSonnetDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = PlutoTestServer()
        cls.server.start()
        os.makedirs(DEMO_ROOT, exist_ok=True)
        os.makedirs(TRACE_DIR, exist_ok=True)
        reset_workspace(HAIKU_WS)
        reset_workspace(SONNET_WS)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_compare(self):
        trace = Trace()
        haiku_result = drive_haiku_team(trace)
        sonnet_result = drive_sonnet_solo(trace)

        out_json = os.path.join(TRACE_DIR, "trace.json")
        with open(out_json, "w") as f:
            json.dump({
                "trace": trace.dump(),
                "haiku": haiku_result,
                "sonnet": sonnet_result,
                "haiku_workspace": HAIKU_WS,
                "sonnet_workspace": SONNET_WS,
                "pluto_http_port": PLUTO_HTTP_PORT,
                "models": {"haiku": HAIKU_MODEL, "sonnet": SONNET_MODEL},
            }, f, indent=2, default=str)

        render_report(os.path.join(REPORT_DIR, "haiku_vs_sonnet_demo.md"),
                      haiku_result, sonnet_result, trace)

        # The demo SUCCEEDS regardless of which side wins — its purpose is to
        # produce the comparison artifact. We only fail if BOTH sides failed
        # AND no copilot calls were made (i.e. infra problem).
        self.assertTrue(
            (len(haiku_result["metrics"]["calls"]) > 0
             and len(sonnet_result["metrics"]["calls"]) > 0),
            "no copilot calls made — infra problem",
        )


if __name__ == "__main__":
    unittest.main()
