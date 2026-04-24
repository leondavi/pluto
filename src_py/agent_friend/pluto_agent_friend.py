#!/usr/bin/env python3
"""
pluto_agent_friend.py — PTY-based I/O wrapper for AI agent CLIs.

PlutoAgentFriend sits between the user's terminal and an agent CLI process,
transparently proxying all I/O while injecting Pluto coordination messages
when the agent is idle.

Architecture
============

    User terminal  ←→  TerminalProxy  ←→  Agent CLI (via PTY)
                            │
                    AgentStateDetector   (parses stdout → BUSY / ASKING / READY)
                            │
                    PlutoConnection      (peek/ack the Pluto inbox)
                            │
                    MessageFormatter     (formats messages as natural-language)

Since v0.2.44 the implementation is split across several modules inside
the ``agent_friend`` package for readability:

    constants.py          Shared ANSI / escape / timing constants
    terminal_proxy.py     TerminalProxy (low-level PTY management)
    state_detector.py     AgentStateDetector
    message_formatter.py  MessageFormatter
    pluto_connection.py   PlutoConnection (peek/ack + in-flight buffer)
    frameworks.py         Framework detection + config helpers
    pluto_agent_friend.py (this file) — PlutoAgentFriend orchestrator + CLI

Injection Modes
===============

    auto    — Inject as soon as the agent is READY and no user input pending.
    confirm — Show notification; auto-inject after 10 s if still ready.
    manual  — Show notification only; at-most-once (messages are acked).

Usage
=====

    python3 pluto_agent_friend.py --agent-id coder-1 -- claude
    python3 pluto_agent_friend.py --agent-id coder-1 --framework copilot --mode confirm
    python3 pluto_agent_friend.py --agent-id coder-1 --ready-pattern '^> $' -- aider
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

# ── Package-path bootstrap ────────────────────────────────────────────────────
# Allow running the file as a script (``python3 pluto_agent_friend.py``)
# AND as a package member (``from agent_friend.pluto_agent_friend import …``).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_PY = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)


def _read_version() -> str:
    """Read the version string from VERSION.md two levels above this file."""
    for candidate in (
        os.path.join(_THIS_DIR, "..", "..", "VERSION.md"),
        os.path.join(_THIS_DIR, "..", "VERSION.md"),
    ):
        try:
            with open(os.path.normpath(candidate)) as _f:
                v = _f.read().strip()
                if v:
                    return v
        except OSError:
            pass
    return "unknown"


__version__: str = _read_version()


# ── Re-export public API (backward compatibility) ────────────────────────────
# Tests and external code import many symbols directly from this module;
# keep the surface unchanged after the v0.2.44 split.
from agent_friend.constants import (  # noqa: E402,F401
    AGENT_STATE_ASKING_USER,
    AGENT_STATE_BUSY,
    AGENT_STATE_READY,
    BOLD,
    CYAN,
    DIM,
    FOCUS_IN_EVENT,
    FOCUS_OUT_EVENT,
    FOCUS_TRACKING_DISABLE,
    FOCUS_TRACKING_ENABLE,
    GREEN,
    GUIDE_MAX_WAIT_S,
    GUIDE_READY_GRACE_S,
    GUIDE_RETRIES,
    GUIDE_RETRY_DELAY_S,
    GUIDE_STARTUP_DELAY_S,
    INJECT_SUBMIT_DELAY_S,
    NC,
    SILENCE_TIMEOUT_S,
    YELLOW,
    strip_ansi,
)
from agent_friend.frameworks import (  # noqa: E402,F401
    KNOWN_FRAMEWORKS,
    check_pluto_status,
    detect_available_frameworks,
    get_framework_cmd,
    get_framework_model_flag,
    get_framework_ready_pattern,
    list_roles,
    load_pluto_config,
    load_role,
)
from agent_friend.message_formatter import MessageFormatter  # noqa: E402
from agent_friend.pluto_connection import PlutoConnection  # noqa: E402
from agent_friend.state_detector import AgentStateDetector  # noqa: E402
from agent_friend.terminal_proxy import TerminalProxy  # noqa: E402

logger = logging.getLogger("pluto_agent_friend")


# ═══════════════════════════════════════════════════════════════════════════════
#  PlutoAgentFriend — Top-level orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class PlutoAgentFriend(TerminalProxy):
    """
    Launch an agent CLI in a PTY with Pluto coordination.

    Inherits from :class:`TerminalProxy` for the core I/O loop and composes
    :class:`AgentStateDetector`, :class:`PlutoConnection`, and
    :class:`MessageFormatter` for the higher-level behaviour.
    """

    def __init__(
        self,
        cmd: list[str],
        agent_id: str,
        pluto_host: str = "localhost",
        pluto_http_port: int = 9001,
        ready_pattern: str | None = None,
        ask_patterns: list[str] | None = None,
        mode: str = "auto",
        poll_timeout: int = 15,
        silence_timeout: float = SILENCE_TIMEOUT_S,
        guide_file: str | None = None,
        guide_startup_delay: float = GUIDE_STARTUP_DELAY_S,
        guide_ready_grace: float = GUIDE_READY_GRACE_S,
        guide_max_wait: float = GUIDE_MAX_WAIT_S,
        guide_retries: int = GUIDE_RETRIES,
        guide_retry_delay: float = GUIDE_RETRY_DELAY_S,
        inject_submit_delay: float = INJECT_SUBMIT_DELAY_S,
        role_file: str | None = None,
        verbose: bool = False,
    ):
        super().__init__(cmd)

        self.agent_id = agent_id
        self.mode = mode
        self.verbose = verbose
        self.guide_file = guide_file
        self.role_file = role_file
        self.guide_startup_delay = guide_startup_delay
        self.guide_ready_grace = guide_ready_grace
        self.guide_max_wait = guide_max_wait
        self.guide_retries = max(0, guide_retries)
        self.guide_retry_delay = guide_retry_delay
        self.inject_submit_delay = inject_submit_delay

        self.detector = AgentStateDetector(
            ready_pattern=ready_pattern,
            ask_patterns=ask_patterns,
            silence_timeout=silence_timeout,
            verbose=verbose,
        )
        self.pluto = PlutoConnection(
            agent_id=agent_id,
            host=pluto_host,
            http_port=pluto_http_port,
            poll_timeout=poll_timeout,
            verbose=verbose,
        )
        self.formatter = MessageFormatter()

        self._injection_thread: threading.Thread | None = None
        self._guide_injected = False
        self._role_injected = False

    # ── Public entry point ────────────────────────────────────────────────

    def run(self) -> int:
        """Run the full PlutoAgentFriend lifecycle.  Returns the child exit code."""
        self._print_banner()

        if self.pluto.connect():
            self._info(
                f"Connected to Pluto at {self.pluto.host}:{self.pluto.http_port} "
                f"(token: {self.pluto.token}...)"
            )
            self._pluto_env = {
                "PLUTO_AGENT_ID": self.pluto.agent_id,
                "PLUTO_TOKEN": self.pluto.full_token,
                "PLUTO_HOST": str(self.pluto.host),
                "PLUTO_HTTP_PORT": str(self.pluto.http_port),
                "PLUTO_WRAPPER": "PlutoAgentFriend",
            }
        else:
            self._info("Starting without Pluto (messages won't be injected)")

        self.spawn()

        self.sync_window_size()
        signal.signal(signal.SIGWINCH, self._handle_sigwinch)
        # Forward termination signals to the child and tear down cleanly.
        # SIGINT (Ctrl-C) is normally delivered to the child via the PTY when
        # the terminal is in raw mode; these handlers cover the cases where
        # the wrapper process itself receives the signal (e.g. shell exits,
        # parent sends SIGTERM, or Ctrl-C before raw-mode was entered).
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
        signal.signal(signal.SIGHUP, self._handle_shutdown_signal)
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        self.enter_raw_mode()

        self._running = True

        if self.pluto.connected:
            self.pluto.start_polling()
            self._injection_thread = threading.Thread(
                target=self._injection_loop, daemon=True, name="inject"
            )
            self._injection_thread.start()

        if self.guide_file:
            threading.Thread(
                target=self._guide_injection_loop, daemon=True,
                name="guide-inject",
            ).start()

        if self.role_file:
            threading.Thread(
                target=self._role_injection_loop, daemon=True,
                name="role-inject",
            ).start()

        exit_code = 1
        try:
            self.copy_loop(timeout=0.5)
            # Child pipe closed — reap with a bounded wait so a wedged
            # child can never hang the wrapper.
            exit_code = self.wait_child(timeout=5.0, escalate_after=2.0)
        except Exception as exc:
            logger.error("I/O loop error: %s", exc)
        finally:
            self.stop()
            self.restore_terminal()
            self.pluto.disconnect()
            os.close(self._master_fd)
            if self.pluto.connected:
                self._info("Disconnected from Pluto")

        return exit_code

    # ── TerminalProxy hooks ───────────────────────────────────────────────

    def on_agent_output(self, data: bytes) -> None:
        """Feed agent output to the state detector."""
        self.detector.analyse_output(data)

    def on_user_input(self, data: bytes) -> None:
        """Record that the user is actively typing."""
        self.detector.record_user_input()

    # ── Guide injection on startup ─────────────────────────────────────────

    def _guide_injection_loop(self) -> None:
        """Background: wait for idle, then inject the skill-guide prompt."""
        time.sleep(self.guide_startup_delay)

        deadline = time.monotonic() + self.guide_max_wait
        while self._running and time.monotonic() < deadline:
            if self.detector.ever_ready or \
                    self.detector.is_ready_for_injection():
                break
            time.sleep(0.2)
        else:
            if self._running:
                logger.debug("Guide injection: agent never became idle")
            return

        self._wait_for_silence(self.guide_ready_grace, deadline)

        if self._guide_injected or not self._running:
            return
        self._guide_injected = True

        guide_basename = os.path.basename(self.guide_file)
        actual_id = self.pluto.agent_id if self.pluto.connected else self.agent_id
        token = self.pluto.full_token if self.pluto.connected else ""
        token_part = (
            f" Your session token for sending messages via curl is: {token}"
        ) if token else ""

        # Load guide content and inline it directly so the agent never has
        # to locate the file on disk (which fails when CWD ≠ Pluto install).
        try:
            with open(self.guide_file, encoding="utf-8") as _gf:
                guide_content = _gf.read().strip()
        except OSError:
            guide_content = ""

        if guide_content:
            prompt = (
                f"This is your skill guide ({guide_basename}) for working with "
                f"PlutoAgentFriend — the coordination wrapper you are currently "
                f"running inside. Read and internalize it now:\n\n"
                f"{guide_content}\n\n"
                f"---\n"
                f"Your agent ID is \"{actual_id}\". "
                f"Use this as your identity when interacting with the Pluto server."
                f"{token_part} "
                f"Do NOT register again — the wrapper already did it for you. "
                f"Incoming messages will be injected into your input automatically. "
                f"Confirm briefly when done."
            )
        else:
            prompt = (
                f"Your skill guide ({guide_basename}) could not be loaded. "
                f"Your agent ID is \"{actual_id}\"."
                f"{token_part} "
                f"Incoming messages will be injected automatically. "
                f"Confirm briefly when ready."
            )
        attempts = 1 + self.guide_retries
        for attempt in range(1, attempts + 1):
            if attempt == 1:
                self._info(f"Injecting startup guide: {guide_basename}")
            else:
                self._info(
                    f"Guide injection retry {attempt - 1}/"
                    f"{self.guide_retries}: {guide_basename}"
                )
            is_last = (attempt == attempts)
            echoed = self.inject_and_submit_when_echoed(
                prompt,
                hard_deadline=max(self.guide_retry_delay, 5.0),
                submit_on_timeout=is_last,
            )
            self.detector.state = AGENT_STATE_BUSY

            if echoed:
                if self.verbose:
                    logger.debug("Guide injection: echo confirmed")
                break

            if is_last:
                logger.debug(
                    "Guide injection: echo never confirmed; "
                    "sent Enter as best-effort fallback"
                )
                break

            logger.debug(
                "Guide injection: no echo within %.1fs, waiting for "
                "silence then retrying",
                self.guide_retry_delay,
            )
            self._wait_for_silence(
                self.guide_ready_grace,
                time.monotonic() + self.guide_retry_delay * 2,
            )

    def _wait_for_silence(self, quiet_for: float, deadline: float) -> bool:
        """Block until agent output has been quiet for *quiet_for* seconds."""
        while self._running and time.monotonic() < deadline:
            quiet = time.monotonic() - self.detector._last_output_time
            if quiet >= quiet_for:
                return True
            time.sleep(min(0.2, max(0.05, quiet_for - quiet)))
        return False

    # ── Role injection on startup ──────────────────────────────────────────

    def _role_injection_loop(self) -> None:
        """Background: after guide injection, inject the role prompt.

        Waits for the guide injection to complete (or for the agent to be
        idle again if no guide was configured), then injects the role file
        content as a single prompt so the agent can internalize its
        behavioral role before processing any coordination messages.
        """
        # Wait until the guide injection has fired (or give up after max_wait)
        # to avoid racing the guide and overwhelming the agent at startup.
        deadline = time.monotonic() + self.guide_max_wait + 30.0
        if self.guide_file:
            # Spin until guide is marked done or deadline passes.
            while self._running and not self._guide_injected \
                    and time.monotonic() < deadline:
                time.sleep(0.5)
        else:
            # No guide — wait for agent to be idle first.
            time.sleep(self.guide_startup_delay)
            while self._running and time.monotonic() < deadline:
                if self.detector.ever_ready or \
                        self.detector.is_ready_for_injection():
                    break
                time.sleep(0.2)

        if not self._running:
            return

        # Allow the agent to finish processing the guide before we pile in.
        self._wait_for_silence(self.guide_ready_grace, deadline)

        if self._role_injected or not self._running:
            return
        self._role_injected = True

        try:
            role_content = load_role(self.role_file)
        except OSError as exc:
            logger.warning("Role file not readable: %s", exc)
            return

        role_basename = os.path.basename(self.role_file)

        # Resolve protocol.md to its absolute path at runtime so the agent
        # can locate it regardless of the working directory it was launched from.
        # Primary: one level up from the role file's directory (works when role
        # lives in library/roles/ regardless of CWD).
        # Fallback: library/protocol.md inside the Pluto project root (needed
        # when the role is given as a full path from an unrelated directory).
        _role_dir = os.path.dirname(os.path.abspath(self.role_file))
        _project_root_local = os.path.normpath(
            os.path.join(_THIS_DIR, "..", "..")
        )
        protocol_path = None
        for _candidate in (
            os.path.normpath(os.path.join(_role_dir, "..", "protocol.md")),
            os.path.join(_project_root_local, "library", "protocol.md"),
        ):
            if os.path.isfile(_candidate):
                protocol_path = _candidate
                break
        protocol_note = (
            f"\n\n(Note: all references to `protocol.md` in your role refer to "
            f"the file at the absolute path: {protocol_path})"
            if protocol_path is not None and "protocol.md" in role_content
            else ""
        )

        prompt = (
            f"You have been assigned a specific role for this session. "
            f"Read and internalize the following role description from "
            f"{role_basename}, then confirm briefly that you understand "
            f"your role and are ready to begin:\n\n{role_content}{protocol_note}"
        )

        attempts = 1 + self.guide_retries
        for attempt in range(1, attempts + 1):
            if attempt == 1:
                self._info(f"Injecting role: {role_basename}")
            else:
                self._info(
                    f"Role injection retry {attempt - 1}/"
                    f"{self.guide_retries}: {role_basename}"
                )
            is_last = (attempt == attempts)
            echoed = self.inject_and_submit_when_echoed(
                prompt,
                hard_deadline=max(self.guide_retry_delay, 5.0),
                submit_on_timeout=is_last,
            )
            self.detector.state = AGENT_STATE_BUSY

            if echoed:
                if self.verbose:
                    logger.debug("Role injection: echo confirmed")
                break

            if is_last:
                logger.debug(
                    "Role injection: echo never confirmed; "
                    "sent Enter as best-effort fallback"
                )
                break

            self._wait_for_silence(
                self.guide_ready_grace,
                time.monotonic() + self.guide_retry_delay * 2,
            )

    # ── Injection logic ───────────────────────────────────────────────────

    def _injection_loop(self) -> None:
        """Background: inject pending Pluto messages when the agent is ready."""
        if self.verbose:
            logger.debug("Injection loop started")
        while self._running:
            time.sleep(0.5)

            if not self.pluto.has_messages():
                continue

            if not self.detector.is_ready_for_injection():
                if self.verbose:
                    logger.debug(
                        "Messages waiting but agent not ready "
                        "(state=%s, silence=%.1fs)",
                        self.detector.state,
                        time.monotonic() - self.detector._last_output_time,
                    )
                continue

            messages = self.pluto.drain_messages()
            if not messages:
                continue

            # Cap batch size (oldest first — ack-by-max-seq stays safe).
            MAX_INJECT = 10
            if len(messages) > MAX_INJECT:
                if self.verbose:
                    logger.debug(
                        "Deferring %d excess message(s) to next cycle",
                        len(messages) - MAX_INJECT,
                    )
                messages = messages[:MAX_INJECT]

            if self.mode == "auto":
                self._do_inject(messages)
            elif self.mode == "confirm":
                self._notify_pending(messages)
                self._wait_confirm_then_inject(messages)
            elif self.mode == "manual":
                # Notification-only: at-most-once by design.
                self._notify_pending(messages)
                self.pluto.confirm_delivered(messages)

    def _do_inject(self, messages: list[dict]) -> None:
        """Format and inject messages; ack on success, abort on failure."""
        prompt = self.formatter.format(messages)
        self._info(f"Injecting {len(messages)} message(s) from Pluto")
        ok = self.inject_and_submit_when_echoed(
            prompt, submit_on_timeout=False,
        )
        if ok:
            self.pluto.confirm_delivered(messages)
            self.detector.state = AGENT_STATE_BUSY
        else:
            self.pluto.abort_delivery(messages)
            logger.warning(
                "Injection of %d message(s) not confirmed by echo; "
                "will retry on next peek cycle",
                len(messages),
            )

    def _notify_pending(self, messages: list[dict]) -> None:
        """Show the user a preview of pending messages (on stderr)."""
        for msg in messages:
            event = msg.get("event", "message")
            sender = msg.get("from", "unknown")
            payload = msg.get("payload", {})
            preview = json.dumps(payload)[:80]
            self._notify(f"Pending [{event}] from {sender}: {preview}")

    def _wait_confirm_then_inject(self, messages: list[dict]) -> None:
        """Confirm mode: show notification, auto-inject after 10 s idle."""
        self._notify(
            "Press Enter in the agent to accept injection (auto in 10s)..."
        )
        deadline = time.monotonic() + 10.0
        while self._running and time.monotonic() < deadline:
            time.sleep(0.5)
            if not self.detector.is_ready_for_injection():
                return
        if self.detector.is_ready_for_injection():
            self._do_inject(messages)

    # ── Window resize ─────────────────────────────────────────────────────

    def _handle_sigwinch(self, _signum, _frame) -> None:
        """Propagate terminal resize to the child PTY."""
        self.sync_window_size()
        try:
            os.kill(self._child_pid, signal.SIGWINCH)
        except OSError:
            pass

    def _handle_shutdown_signal(self, signum, _frame) -> None:
        """Forward a shutdown signal to the child and break the copy loop.

        Installed for SIGINT / SIGTERM / SIGHUP.  In raw-mode the user's
        Ctrl-C is delivered to the child via the PTY, so this handler
        only fires for signals sent directly to the wrapper process
        (e.g. ``kill <pid>``, parent shell exit, Ctrl-C before raw mode).
        """
        name = {
            signal.SIGINT: "SIGINT",
            signal.SIGTERM: "SIGTERM",
            signal.SIGHUP: "SIGHUP",
        }.get(signum, str(signum))
        logger.debug("Received %s \u2014 shutting down", name)
        # Relay to the child so it gets a chance to exit cleanly.
        sig_to_child = signal.SIGHUP if signum == signal.SIGHUP \
            else signal.SIGTERM if signum == signal.SIGTERM \
            else signal.SIGINT
        self._signal_child(sig_to_child)
        self._running = False

    # ── User-facing output helpers ────────────────────────────────────────

    def _print_banner(self) -> None:
        """Print startup information to stderr."""
        guide_str = os.path.basename(self.guide_file) if self.guide_file else "none"
        sys.stderr.write(
            f"\n{CYAN}[pluto-friend]{NC} Agent wrapper starting\n"
            f"{CYAN}[pluto-friend]{NC} Agent ID  : {BOLD}{self.agent_id}{NC}\n"
            f"{CYAN}[pluto-friend]{NC} Command   : {' '.join(self.cmd)}\n"
            f"{CYAN}[pluto-friend]{NC} Mode      : {self.mode}\n"
            f"{CYAN}[pluto-friend]{NC} Guide     : {guide_str}\n"
            f"{CYAN}[pluto-friend]{NC} Pluto     : "
            f"{self.pluto.host}:{self.pluto.http_port}\n"
        )
        sys.stderr.flush()

    @staticmethod
    def _info(msg: str) -> None:
        """Print an informational line to stderr."""
        sys.stderr.write(f"\r\n{CYAN}[pluto-friend]{NC} {msg}\r\n")
        sys.stderr.flush()

    @staticmethod
    def _notify(msg: str) -> None:
        """Print a notification line to stderr."""
        sys.stderr.write(f"\r\n{YELLOW}[pluto-friend]{NC} {msg}\r\n")
        sys.stderr.flush()


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pluto_agent_friend",
        description=(
            "PlutoAgentFriend — PTY wrapper that injects Pluto coordination "
            "messages into an AI agent's CLI when the agent is idle."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --agent-id coder-1 -- claude\n"
            "  %(prog)s --agent-id coder-1 --framework copilot --mode confirm\n"
            "  %(prog)s --agent-id coder-1 --ready-pattern '^> $' -- aider\n"
        ),
    )
    parser.add_argument(
        "--agent-id", required=True,
        help="Agent ID for Pluto registration (e.g. coder-1)",
    )
    parser.add_argument(
        "--framework", choices=list(KNOWN_FRAMEWORKS.keys()),
        help="Agent framework: claude, copilot, aider, cursor. "
             "Auto-detected if omitted.",
    )
    parser.add_argument(
        "--model", default=None, metavar="NAME",
        help="Model to pass to the underlying agent CLI "
             "(e.g. 'gpt-5.2', 'claude-sonnet-4.5', 'claude-haiku-4.5'). "
             "Forwarded as the framework's model flag (e.g. copilot/claude/aider "
             "all use --model). Ignored when an explicit command is given "
             "after `--`.",
    )
    parser.add_argument(
        "--host", default=None,
        help="Pluto server host (default: from config or localhost)",
    )
    parser.add_argument(
        "--http-port", type=int, default=None,
        help="Pluto HTTP port (default: from config or 9001)",
    )
    parser.add_argument(
        "--ready-pattern",
        help="Regex matching the agent's 'ready for input' prompt",
    )
    parser.add_argument(
        "--mode", choices=["auto", "confirm", "manual"], default="auto",
        help="Injection mode (default: auto)",
    )
    parser.add_argument(
        "--silence-timeout", type=float, default=SILENCE_TIMEOUT_S,
        help=f"Seconds of silence = agent idle (default: {SILENCE_TIMEOUT_S})",
    )
    parser.add_argument(
        "--poll-timeout", type=int, default=15,
        help="Pluto long-poll timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--guide", default=None,
        help="Path to a skill-guide file to inject on startup. "
             "Defaults to agent_friend_guide.md if it exists.",
    )
    parser.add_argument(
        "--no-guide", action="store_true",
        help="Disable automatic guide injection even if the file exists.",
    )
    parser.add_argument(
        "--role", default=None, metavar="PATH",
        help="Path to a role file (.md) to inject after the guide. "
             "Default: no role injection.",
    )
    parser.add_argument(
        "--roles-dir", default=None, metavar="DIR",
        help="Directory to scan for role files (default: library/roles/ "
             "under the project root). Used when listing available roles.",
    )
    parser.add_argument(
        "--guide-startup-delay", type=float, default=GUIDE_STARTUP_DELAY_S,
        help=f"Seconds to wait after spawn before checking agent readiness "
             f"(default: {GUIDE_STARTUP_DELAY_S}, env: PLUTO_GUIDE_STARTUP_DELAY).",
    )
    parser.add_argument(
        "--guide-ready-grace", type=float, default=GUIDE_READY_GRACE_S,
        help=f"Seconds to wait after the ready pattern matches before "
             f"injecting (default: {GUIDE_READY_GRACE_S}, "
             f"env: PLUTO_GUIDE_READY_GRACE).",
    )
    parser.add_argument(
        "--guide-max-wait", type=float, default=GUIDE_MAX_WAIT_S,
        help=f"Maximum seconds to wait for agent to become ready "
             f"(default: {GUIDE_MAX_WAIT_S}, env: PLUTO_GUIDE_MAX_WAIT).",
    )
    parser.add_argument(
        "--guide-retries", type=int, default=GUIDE_RETRIES,
        help=f"Re-inject the guide prompt this many times if the agent "
             f"does not react (default: {GUIDE_RETRIES}, "
             f"env: PLUTO_GUIDE_RETRIES).",
    )
    parser.add_argument(
        "--guide-retry-delay", type=float, default=GUIDE_RETRY_DELAY_S,
        help=f"Seconds to wait for agent reaction before retrying "
             f"(default: {GUIDE_RETRY_DELAY_S}, "
             f"env: PLUTO_GUIDE_RETRY_DELAY).",
    )
    parser.add_argument(
        "--inject-submit-delay", type=float, default=INJECT_SUBMIT_DELAY_S,
        help=f"Seconds between writing injected text and the Enter key "
             f"(default: {INJECT_SUBMIT_DELAY_S}, "
             f"env: PLUTO_INJECT_SUBMIT_DELAY).",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable verbose debug logging",
    )
    parser.add_argument(
        "cmd", nargs="*",
        help="Agent command (after --).  Overrides --framework.",
    )

    args = parser.parse_args()

    # ── Show banner ───────────────────────────────────────────────────────
    DKGRAY = "\033[1;30m"
    print(
        f"\n"
        f"                    {GREEN}.am######mp.{NC}\n"
        f"                {GREEN}.a################a.{NC}\n"
        f"             {GREEN}.a######################a.{NC}\n"
        f"            {GREEN}a##########################a{NC}\n"
        f"           {GREEN}####                      ####{NC}\n"
        f"          {GREEN}###  {DKGRAY}########{GREEN}    {DKGRAY}########{GREEN}  ###{NC}\n"
        f"          {GREEN}##  {DKGRAY}##########{GREEN}  {DKGRAY}##########{GREEN}  ##{NC}\n"
        f"          {GREEN}###  {DKGRAY}########{GREEN}    {DKGRAY}########{GREEN}  ###{NC}\n"
        f"           {GREEN}####                      ####{NC}\n"
        f"            {GREEN}######    {DKGRAY}._____{GREEN}.   ######{NC}\n"
        f"             {GREEN}.a######################a.{NC}\n"
        f"                {GREEN}a################a{NC}\n"
        f"                   {GREEN}7##########7{NC}\n"
        f"                      {GREEN}'####'{NC}\n"
        f"                        {GREEN}''{NC}\n"
        f"\n"
        f"    {CYAN}╔═══════════════════════════════════════════════╗{NC}\n"
        f"    {CYAN}║{NC}                                               {CYAN}║{NC}\n"
        f"    {CYAN}║{NC}   {GREEN}★{NC}  {BOLD}PlutoAgentFriend{NC}  {DIM}{__version__}{NC}              {CYAN}║{NC}\n"
        f"    {CYAN}║{NC}      AI Agent + Pluto Coordination Wrapper    {CYAN}║{NC}\n"
        f"    {CYAN}║{NC}                                               {CYAN}║{NC}\n"
        f"    {CYAN}╚═══════════════════════════════════════════════╝{NC}\n",
        file=sys.stderr,
    )

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")

    # --- Load config defaults ---
    config = load_pluto_config()
    server_cfg = config.get("pluto_server", {})
    host = args.host or server_cfg.get("host_ip", "localhost")
    http_port = args.http_port or server_cfg.get("host_http_port", 9001)

    # --- Determine agent command ---
    cmd = args.cmd
    ready_pattern = args.ready_pattern

    if not cmd:
        if args.framework:
            cmd = get_framework_cmd(args.framework, model=args.model)
            if not ready_pattern:
                ready_pattern = get_framework_ready_pattern(args.framework)
        else:
            available = detect_available_frameworks()
            if not available:
                print(
                    "No known agent frameworks found.  "
                    "Specify a command after -- or use --framework.",
                    file=sys.stderr,
                )
                sys.exit(1)
            elif len(available) == 1:
                fw = available[0]
                print(
                    f"Auto-detected: {fw['display']} ({fw['path']})",
                    file=sys.stderr,
                )
                cmd = get_framework_cmd(fw["key"], model=args.model)
                if not ready_pattern:
                    ready_pattern = get_framework_ready_pattern(fw["key"])
            else:
                print("Multiple agent frameworks detected:", file=sys.stderr)
                for i, fw in enumerate(available, 1):
                    print(f"  {i}. {fw['display']} ({fw['path']})",
                          file=sys.stderr)
                print(file=sys.stderr)
                try:
                    choice = input("Select framework (number): ").strip()
                    idx = int(choice) - 1
                    if 0 <= idx < len(available):
                        fw = available[idx]
                        cmd = get_framework_cmd(fw["key"], model=args.model)
                        if not ready_pattern:
                            ready_pattern = get_framework_ready_pattern(
                                fw["key"])
                    else:
                        print("Invalid choice.", file=sys.stderr)
                        sys.exit(1)
                except (ValueError, EOFError):
                    print("Invalid choice.", file=sys.stderr)
                    sys.exit(1)
    elif args.model:
        # Explicit command after `--` was provided; warn that --model is ignored
        # so the user is not surprised when the underlying CLI starts with its
        # default model.
        print(
            f"{YELLOW}[pluto-friend]{NC} --model={args.model} ignored: an "
            f"explicit command was supplied after `--`. Add the model flag "
            f"to the command yourself if needed.",
            file=sys.stderr,
        )

    # --- Show Pluto server status ---
    print(
        f"\n{CYAN}[pluto-friend]{NC} Checking Pluto server at "
        f"{host}:{http_port}...",
        file=sys.stderr,
    )
    status = check_pluto_status(host, http_port)
    if status:
        version = status.get("version", "?")
        print(
            f"{GREEN}[pluto-friend]{NC} Pluto server is {GREEN}ONLINE{NC} "
            f"(v{version}) at {host}:{http_port}",
            file=sys.stderr,
        )
        # Strict client/server version check. The HTTP API surface
        # (peek/ack, roles, etc.) must match exactly; a stale server
        # silently returns 404 on missing endpoints which is very
        # confusing for users.
        def _normalize(v: str) -> str:
            v = (v or "").strip()
            if v.startswith("v") or v.startswith("V"):
                v = v[1:]
            return v

        client_v = _normalize(__version__)
        server_v = _normalize(version)
        if client_v and server_v and client_v != server_v:
            print(
                f"\n{YELLOW}[pluto-friend]{NC} {BOLD}Version mismatch:{NC} "
                f"client is v{client_v}, server is v{server_v}.\n"
                f"{YELLOW}[pluto-friend]{NC} Rebuild and restart the server "
                f"to match:\n"
                f"    ./PlutoServer.sh --kill && ./PlutoServer.sh --daemon\n"
                f"{YELLOW}[pluto-friend]{NC} Refusing to start — endpoints "
                f"like /agents/peek may be missing on the running server.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        print(
            f"{YELLOW}[pluto-friend]{NC} Pluto server is {YELLOW}OFFLINE{NC} "
            f"at {host}:{http_port}",
            file=sys.stderr,
        )

    # --- Resolve guide file ---
    # Search order: explicit --guide path → project root → CWD.
    # Project root is preferred over CWD so the wrapper works correctly
    # when invoked from a directory other than the Pluto installation.
    _project_root = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))
    guide_file = None
    if not args.no_guide:
        if args.guide:
            guide_file = os.path.abspath(args.guide)
        else:
            for candidate in [
                os.path.join(_project_root, "agent_friend_guide.md"),
                os.path.join(os.getcwd(), "agent_friend_guide.md"),
            ]:
                if os.path.isfile(candidate):
                    guide_file = candidate
                    break
        if guide_file and not os.path.isfile(guide_file):
            print(
                f"{YELLOW}[pluto-friend]{NC} Guide file not found: "
                f"{guide_file}",
                file=sys.stderr,
            )
            guide_file = None
        if guide_file:
            print(
                f"{CYAN}[pluto-friend]{NC} Guide: {guide_file}",
                file=sys.stderr,
            )

    # --- Resolve role file ---
    # Accept: absolute/relative path, OR a bare name resolved against the
    # project's library/roles/ directory (works regardless of CWD).
    role_file = None
    if args.role:
        raw = args.role
        # Bare name: no path separator and no .md suffix → look in library/roles/
        if os.sep not in raw and "/" not in raw and not raw.endswith(".md"):
            roles_dir = (
                args.roles_dir
                if args.roles_dir
                else os.path.join(_project_root, "library", "roles")
            )
            candidate = os.path.join(roles_dir, raw + ".md")
            if os.path.isfile(candidate):
                role_file = candidate
            else:
                # Fall back: treat as a CWD-relative or absolute path
                role_file = os.path.abspath(raw)
        else:
            role_file = os.path.abspath(raw)
        if not os.path.isfile(role_file):
            print(
                f"{YELLOW}[pluto-friend]{NC} Role file not found: {role_file}",
                file=sys.stderr,
            )
            role_file = None
        else:
            role_name = os.path.splitext(os.path.basename(role_file))[0]
            print(
                f"{CYAN}[pluto-friend]{NC} Role: {role_name} ({role_file})",
                file=sys.stderr,
            )

    # --- Launch ---
    friend = PlutoAgentFriend(
        cmd=cmd,
        agent_id=args.agent_id,
        pluto_host=host,
        pluto_http_port=http_port,
        ready_pattern=ready_pattern,
        mode=args.mode,
        poll_timeout=args.poll_timeout,
        silence_timeout=args.silence_timeout,
        guide_file=guide_file,
        guide_startup_delay=args.guide_startup_delay,
        guide_ready_grace=args.guide_ready_grace,
        guide_max_wait=args.guide_max_wait,
        guide_retries=args.guide_retries,
        guide_retry_delay=args.guide_retry_delay,
        inject_submit_delay=args.inject_submit_delay,
        role_file=role_file,
        verbose=args.verbose,
    )
    sys.exit(friend.run())


if __name__ == "__main__":
    main()
