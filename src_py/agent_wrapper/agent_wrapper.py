"""
AgentWrapper — Orchestrate multiple agents running coordinated flows via Pluto.

Supports three agent backends:
  - "script":  Execute a JSON flow directly using FlowRunner + PlutoClient.
  - "claude":  Launch the Claude CLI with a task prompt.
  - "copilot": Launch the GitHub Copilot CLI with a task prompt.

Usage:
    wrapper = AgentWrapper(host="127.0.0.1", port=9000)
    results = wrapper.run_system_flow("sys_shared_edit.json")
    assert results["success"]
"""

import json
import os
import subprocess
import tempfile
import threading
import time

from .flow_runner import FlowRunner


class AgentWrapper:
    """Orchestrate a set of agents described by a system flow JSON file."""

    def __init__(self, host="127.0.0.1", port=9000):
        self.host = host
        self.port = port

    # ── Public API ────────────────────────────────────────────────────────

    def run_system_flow(self, sys_flow_path):
        """
        Load and execute a system flow JSON file.

        Returns a dict:
            success    — True if all agents completed and all assertions passed.
            agents     — Per-agent results (agent_id, success, error, log).
            assertions — Results for each assertion in the system flow.
        """
        with open(sys_flow_path) as f:
            sys_flow = json.load(f)

        flow_dir = os.path.dirname(os.path.abspath(sys_flow_path))
        work_dir = self._resolve_work_dir(
            sys_flow.get("work_dir", "${TEMP_DIR}/pluto_test"),
        )
        os.makedirs(work_dir, exist_ok=True)

        timeout = sys_flow.get("timeout_s", 60)
        agent_configs = sys_flow.get("agents", [])

        runners = []
        threads = []

        for cfg in agent_configs:
            flow_path = os.path.join(flow_dir, cfg["flow"])
            with open(flow_path) as f:
                flow_data = json.load(f)

            runner = FlowRunner(flow_data, self.host, self.port, work_dir)
            runners.append(runner)

            delay = cfg.get("start_delay_s", 0)

            def _run(r=runner, d=delay):
                if d > 0:
                    time.sleep(d)
                r.run()

            t = threading.Thread(target=_run, daemon=True)
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join(timeout=timeout)

        return self._evaluate(sys_flow, runners, work_dir)

    def launch_cli_agent(self, agent_id, task_description, backend="claude"):
        """
        Launch a CLI-based AI agent with a coordination task.

        The agent receives a prompt instructing it to use PlutoClient.
        Returns the subprocess.Popen handle.
        """
        prompt = self._build_cli_prompt(agent_id, task_description)

        if backend == "claude":
            cmd = ["claude", "--print", prompt]
        elif backend == "copilot":
            cmd = ["gh", "copilot", "suggest", prompt]
        else:
            raise ValueError(f"Unknown backend: {backend}")

        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _resolve_work_dir(self, raw):
        return raw.replace("${TEMP_DIR}", tempfile.gettempdir())

    def _build_cli_prompt(self, agent_id, task):
        return (
            f"You are agent '{agent_id}'. Connect to the Pluto coordination "
            f"server at {self.host}:{self.port} using the PlutoClient Python "
            f"library. Register as '{agent_id}', then perform the following "
            f"task:\n\n{task}\n\n"
            f"Use client.acquire() to lock shared resources before modifying "
            f"them and client.release() when done. Use client.send() to "
            f"notify other agents."
        )

    def _evaluate(self, sys_flow, runners, work_dir):
        results = {
            "success": all(r.success for r in runners),
            "agents": [
                {
                    "agent_id": r.agent_id,
                    "success": r.success,
                    "error": r.error,
                    "log": r.log,
                }
                for r in runners
            ],
            "assertions": [],
        }

        for assertion in sys_flow.get("assertions", []):
            atype = assertion["type"]

            if atype == "file_contains":
                path = assertion["path"].replace("${WORK_DIR}", work_dir)
                try:
                    with open(path) as f:
                        content = f.read()
                    for expected in assertion.get("expected", []):
                        passed = expected in content
                        results["assertions"].append(
                            {"type": atype, "expected": expected, "passed": passed},
                        )
                        if not passed:
                            results["success"] = False
                except FileNotFoundError:
                    results["assertions"].append(
                        {"type": atype, "passed": False,
                         "error": f"File not found: {path}"},
                    )
                    results["success"] = False

            elif atype == "all_agents_completed":
                passed = all(r.success for r in runners)
                results["assertions"].append({"type": atype, "passed": passed})
                if not passed:
                    results["success"] = False

        return results
