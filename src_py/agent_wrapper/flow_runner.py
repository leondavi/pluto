"""
Execute a JSON agent flow using the PlutoClient.

Each flow is a list of steps (acquire, release, write_file, send, etc.)
that an agent performs sequentially while coordinating through Pluto.
"""

import json
import os
import sys
import threading
import time

# Ensure the parent src_py directory is importable for pluto_client.
_parent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from pluto_client import PlutoClient, PlutoError


class FlowRunner:
    """
    Execute a sequence of steps from an agent flow JSON structure.

    Supported actions:
        acquire, release, write_file, append_file, read_file,
        send, broadcast, wait_message, sleep.
    """

    def __init__(self, flow_data, host, port, work_dir):
        self.flow = flow_data
        self.agent_id = flow_data["agent_id"]
        self.host = host
        self.port = port
        self.work_dir = work_dir
        self.client = None
        self.current_lock_ref = None
        self.messages = []
        self._messages_lock = threading.Lock()
        self._msg_event = threading.Event()
        self.log = []
        self.success = False
        self.error = None

    def run(self):
        """Execute all flow steps. Returns True on success."""
        try:
            os.makedirs(self.work_dir, exist_ok=True)
            self.client = PlutoClient(
                host=self.host,
                port=self.port,
                agent_id=self.agent_id,
                timeout=15.0,
            )
            self.client.on_message(self._on_message)
            self.client.on_lock_granted(self._on_lock_granted)
            self.client.connect()
            self._log(f"Connected as {self.agent_id}")

            for i, step in enumerate(self.flow.get("steps", [])):
                self._log(f"Step {i + 1}: {step['action']}")
                self._exec_step(step)

            self.success = True
            self._log("Flow completed successfully")
        except Exception as exc:
            self.error = str(exc)
            self._log(f"Error: {exc}")
        finally:
            if self.client:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
        return self.success

    # ── Step dispatch ─────────────────────────────────────────────────────

    def _exec_step(self, step):
        action = step["action"]
        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            raise ValueError(f"Unknown action: {action}")
        handler(step)

    def _action_acquire(self, step):
        resource = step["resource"]
        mode = step.get("mode", "write")
        ttl_ms = step.get("ttl_ms", 30000)
        ref = self.client.acquire(resource, mode=mode, ttl_ms=ttl_ms)
        self.current_lock_ref = ref
        self._log(f"Acquired {resource} -> {ref}")

    def _action_release(self, step):
        if self.current_lock_ref:
            self.client.release(self.current_lock_ref)
            self._log(f"Released {self.current_lock_ref}")
            self.current_lock_ref = None

    def _action_write_file(self, step):
        path = self._resolve(step["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(step["content"])
        self._log(f"Wrote {path}")

    def _action_append_file(self, step):
        path = self._resolve(step["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(step["content"])
        self._log(f"Appended to {path}")

    def _action_read_file(self, step):
        path = self._resolve(step["path"])
        with open(path, "r") as f:
            content = f.read()
        self._log(f"Read {path} ({len(content)} bytes)")

    def _action_send(self, step):
        self.client.send(step["to"], step["payload"])
        self._log(f"Sent message to {step['to']}")

    def _action_broadcast(self, step):
        self.client.broadcast(step["payload"])
        self._log("Broadcast sent")

    def _action_wait_message(self, step):
        timeout = step.get("timeout", 15)
        expected_from = step.get("from")
        msg = self._wait_for_message(expected_from, timeout)
        self._log(f"Received message from {msg.get('from')}")

    def _action_sleep(self, step):
        seconds = step.get("seconds", 1)
        time.sleep(seconds)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _resolve(self, path):
        return path.replace("${WORK_DIR}", self.work_dir)

    def _on_message(self, event):
        with self._messages_lock:
            self.messages.append(event)
        self._msg_event.set()

    def _on_lock_granted(self, event):
        self.current_lock_ref = event.get("lock_ref")
        self._msg_event.set()

    def _wait_for_message(self, from_agent, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._messages_lock:
                for msg in list(self.messages):
                    if from_agent is None or msg.get("from") == from_agent:
                        self.messages.remove(msg)
                        return msg
            self._msg_event.clear()
            remaining = deadline - time.time()
            if remaining > 0:
                self._msg_event.wait(timeout=min(remaining, 0.5))
        raise TimeoutError(f"Timeout waiting for message from {from_agent}")

    def _log(self, msg):
        self.log.append(f"[{self.agent_id}] {msg}")
