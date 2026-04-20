"""
AgentWrapper — Launch real GitHub Copilot CLI agents coordinating through Pluto.

Each agent is a separate `copilot -p` invocation that receives a task prompt
and autonomously writes + executes a Python script using PlutoClient to
coordinate with other agents through the real Pluto server.

Usage:
    wrapper = AgentWrapper(host="127.0.0.1", port=9000)
    results = wrapper.run_copilot_agents(
        agents=[
            {"agent_id": "editor-1", "task": "Acquire lock, write file, signal editor-2."},
            {"agent_id": "editor-2", "task": "Wait for signal, append file, confirm."},
        ],
        work_dir="/tmp/pluto_test",
    )
    assert results["success"]
"""

import os
import shutil
import subprocess
import threading
import time

_SRC_PY = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_PROJECT = os.path.abspath(os.path.join(_SRC_PY, ".."))

# PlutoClient API reference embedded in every agent prompt.
# Uses __PLACEHOLDER__ markers (not {braces}) to avoid conflicts with
# Python code examples that contain dict literals.
_API_REFERENCE = r'''## PlutoClient API Reference

```python
import sys, os, threading, time
sys.path.insert(0, "__SRC_PY__")
from pluto_client import PlutoClient

# --- Message handling (set up BEFORE client.connect) ---
messages = []
msg_event = threading.Event()
_msg_lock = threading.Lock()

def on_msg(event):
    with _msg_lock:
        messages.append(event)
    msg_event.set()

def wait_msg(from_agent, timeout=120):
    """Block until a message from `from_agent` arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _msg_lock:
            for i, m in enumerate(messages):
                if m.get("from") == from_agent:
                    return messages.pop(i)
        msg_event.clear()
        msg_event.wait(timeout=1)
    raise TimeoutError(f"No message from {from_agent} within {timeout}s")

# --- Connect & register ---
client = PlutoClient(host="__HOST__", port=__PORT__, agent_id="__AGENT_ID__")
client.on_message(on_msg)
client.connect()

# --- Lock a resource (blocks until granted) ---
lock_ref = client.acquire("resource-name", mode="write", ttl_ms=30000)
# ... do work while holding the lock ...
client.release(lock_ref)

# --- Send a direct message to another agent ---
client.send("other-agent-id", {"type": "hello", "data": "..."})

# --- Wait for a message from a specific agent ---
msg = wait_msg("other-agent-id", timeout=120)

# --- Broadcast to ALL connected agents ---
client.broadcast({"type": "announcement"})

# --- File I/O (standard Python — use the work directory) ---
os.makedirs("__WORK_DIR__", exist_ok=True)
with open(os.path.join("__WORK_DIR__", "output.txt"), "w") as f:
    f.write("content\n")
with open(os.path.join("__WORK_DIR__", "output.txt"), "a") as f:
    f.write("appended line\n")

# --- Always disconnect when done ---
client.disconnect()
```
'''


class AgentWrapper:
    """Launch real Copilot CLI agents that coordinate through Pluto."""

    def __init__(self, host="127.0.0.1", port=9000):
        self.host = host
        self.port = port

    # ── Public API ────────────────────────────────────────────────────────

    def run_copilot_agents(self, agents, work_dir, timeout=180):
        """
        Launch real Copilot CLI agents to perform coordinated tasks.

        Args:
            agents: list of dicts, each with:
                - agent_id  (str):  Pluto registration name
                - task      (str):  natural-language task description
                - start_delay_s (float, optional): seconds to wait before launch
            work_dir: directory where agents create output files
            timeout:  max seconds per agent subprocess

        Returns dict:
            success — True if every agent exited with rc=0
            agents  — per-agent dicts with stdout, stderr, returncode
        """
        os.makedirs(work_dir, exist_ok=True)

        agent_results = []
        results_lock = threading.Lock()
        threads = []

        for cfg in agents:
            agent_id = cfg["agent_id"]
            task = cfg["task"]
            delay = cfg.get("start_delay_s", 0)
            prompt = self._build_prompt(agent_id, task, work_dir)

            def _run(aid=agent_id, p=prompt, d=delay):
                if d > 0:
                    time.sleep(d)
                result = self._launch_copilot(aid, p, work_dir, timeout)
                with results_lock:
                    agent_results.append(result)

            t = threading.Thread(target=_run, daemon=True)
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join(timeout=timeout + 60)

        return {
            "success": all(r["success"] for r in agent_results),
            "agents": sorted(agent_results, key=lambda r: r["agent_id"]),
        }

    # ── Internals ─────────────────────────────────────────────────────────

    def _build_prompt(self, agent_id, task, work_dir):
        """Build the full prompt sent to the Copilot CLI."""
        api_ref = (_API_REFERENCE
                   .replace("__SRC_PY__", _SRC_PY)
                   .replace("__HOST__", self.host)
                   .replace("__PORT__", str(self.port))
                   .replace("__AGENT_ID__", agent_id)
                   .replace("__WORK_DIR__", work_dir))

        return (
            f"You are AI agent '{agent_id}'. Create and execute a Python "
            f"script that performs the task below, coordinating with other "
            f"agents through the Pluto server.\n\n"
            f"## Connection\n"
            f"- Pluto server: {self.host}:{self.port}\n"
            f"- Work directory: {work_dir}\n"
            f"- PlutoClient path: {_SRC_PY}/pluto_client.py\n\n"
            f"{api_ref}\n\n"
            f"## Your Task\n\n{task}\n\n"
            f"## Rules\n"
            f"- Write a COMPLETE Python script and execute it.\n"
            f"- Use PlutoClient for ALL coordination (locks, messaging).\n"
            f"- Only create/modify files inside {work_dir}.\n"
            f"- Always call client.disconnect() at the end.\n"
            f"- Print progress to stdout so your work is visible.\n"
        )

    def _launch_copilot(self, agent_id, prompt, work_dir, timeout):
        """Launch a single Copilot CLI subprocess."""
        env = os.environ.copy()
        env["PYTHONPATH"] = _SRC_PY + ":" + env.get("PYTHONPATH", "")

        cmd = [
            "copilot",
            "-p", prompt,
            "--allow-all",
            "--no-ask-user",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=_PROJECT,
                env=env,
            )
            return {
                "agent_id": agent_id,
                "success": proc.returncode == 0,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "agent_id": agent_id,
                "success": False,
                "stdout": "",
                "stderr": f"Timeout after {timeout}s",
                "returncode": -1,
            }
        except Exception as exc:
            return {
                "agent_id": agent_id,
                "success": False,
                "stdout": "",
                "stderr": str(exc),
                "returncode": -1,
            }
