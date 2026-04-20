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
                    PlutoConnection      (long-polls Pluto server for messages)
                            │
                    MessageFormatter     (formats messages as natural-language)
                            │
                    InjectionGate        (decides when/how to inject)

Class Hierarchy
===============

    TerminalProxy        Low-level PTY management, raw mode, non-blocking I/O.
                         Modelled after Python's pty.spawn() with buffered
                         reads/writes and proper non-blocking master_fd.

    AgentStateDetector   Watches agent output and classifies the agent's current
                         state: BUSY (producing output), ASKING_USER (waiting
                         for human answer), or READY (idle, safe to inject).

    MessageFormatter     Converts Pluto protocol messages (JSON dicts) into
                         natural-language prompts that any LLM agent can process.

    PlutoConnection      Manages the HTTP session with the Pluto server:
                         register, long-poll for messages, unregister on exit.

    PlutoAgentFriend     Top-level orchestrator that wires everything together.
                         Owns the main run() loop and coordinates injection.

Injection Modes
===============

    auto    — Inject as soon as the agent is READY and no user input pending.
    confirm — Show notification; auto-inject after 10 s if still ready.
    manual  — Show notification only; the user copy-pastes or types the message.

Safety Rules
============

    1. User input always has priority over injected messages.
    2. Never inject when the agent is asking the user a question.
    3. Injections are displayed to the user (stderr) for transparency.

Usage
=====

    python3 pluto_agent_friend.py --agent-id coder-1 -- claude
    python3 pluto_agent_friend.py --agent-id coder-1 --framework copilot --mode confirm
    python3 pluto_agent_friend.py --agent-id coder-1 --ready-pattern '^> $' -- aider
"""

# ── Standard library imports ──────────────────────────────────────────────────
import argparse
import fcntl
import json
import logging
import os
import pty
import re
import select
import signal
import sys
import termios
import threading
import time
import tty

# ── Project imports ───────────────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_PY = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from pluto_client import PlutoHttpClient, PlutoError  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("pluto_agent_friend")

# ── File descriptors (match pty module constants) ─────────────────────────────
STDIN_FILENO = pty.STDIN_FILENO   # 0
STDOUT_FILENO = pty.STDOUT_FILENO  # 1

# ── ANSI colour helpers (for stderr messages) ─────────────────────────────────
CYAN = "\033[0;36m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
DIM = "\033[2m"
BOLD = "\033[1m"

# ── Terminal escape sequences for focus event handling ────────────────────────
# When an Ink-based TUI (e.g. Copilot CLI) enables focus event tracking, the
# outer terminal (VS Code) sends \x1b[O (focus-out) whenever the terminal pane
# loses focus.  In a PTY proxy context these events are forwarded to the inner
# process, causing it to believe the terminal is inactive and stop accepting
# keyboard input.
#
# Solution: intercept focus-tracking enable/disable in the agent's output so the
# outer terminal never enables focus events, and always tell the inner process
# that the terminal is focused.
FOCUS_TRACKING_ENABLE  = b"\x1b[?1004h"   # DEC private mode 1004 – enable
FOCUS_TRACKING_DISABLE = b"\x1b[?1004l"   # DEC private mode 1004 – disable
FOCUS_IN_EVENT  = b"\x1b[I"               # CSI I – terminal gained focus
FOCUS_OUT_EVENT = b"\x1b[O"               # CSI O – terminal lost focus
NC = "\033[0m"

# ── Agent state constants ─────────────────────────────────────────────────────
AGENT_STATE_BUSY = "BUSY"
AGENT_STATE_ASKING_USER = "ASKING_USER"
AGENT_STATE_READY = "READY"

# ── ANSI escape stripper ─────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text* and return the clean string."""
    return _ANSI_RE.sub("", text)


# ═══════════════════════════════════════════════════════════════════════════════
#  TerminalProxy — Low-level PTY management
# ═══════════════════════════════════════════════════════════════════════════════

class TerminalProxy:
    """
    Spawn a child process inside a PTY and proxy I/O to/from the real terminal.

    This class mirrors the logic of Python's :func:`pty.spawn` / :func:`pty._copy`
    with two critical improvements over a naive approach:

    * The PTY master fd is set to **non-blocking** so writes never stall the
      event loop (which would freeze keyboard input).
    * Read and write operations are **buffered**: data is accumulated in
      ``_ibuf`` (stdin → master) and ``_obuf`` (master → stdout), and
      ``select()`` is used for *both* read-readiness and write-readiness.

    Subclasses can override :meth:`on_agent_output` to inspect every chunk
    the agent writes to its stdout/stderr.
    """

    # High-water mark: stop reading when buffer exceeds this size.
    _HIGH_WATER = 4096

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self._child_pid: int = 0
        self._master_fd: int = -1
        self._old_tty_attrs = None
        self._running = False

        # Internal I/O buffers (stdin→master and master→stdout).
        self._ibuf = b""   # input  buffer: user keystrokes → agent
        self._obuf = b""   # output buffer: agent output   → terminal

        # Focus event handling: when the child enables focus tracking
        # (\x1b[?1004h), we intercept it so the outer terminal never sends
        # focus events.  Instead we immediately tell the child it is focused.
        self._focus_tracking_active = False

    # ── Child lifecycle ───────────────────────────────────────────────────

    def spawn(self) -> None:
        """Fork a PTY and exec *self.cmd* in the child process."""
        # Capture parent terminal attributes BEFORE forking so we can
        # apply them to the PTY slave.  This ensures the child inherits
        # realistic terminal settings (baud rate, special characters, flags)
        # rather than the system defaults that openpty() provides.
        parent_attrs = None
        try:
            parent_attrs = termios.tcgetattr(STDIN_FILENO)
        except termios.error:
            pass

        self._child_pid, self._master_fd = pty.fork()
        if self._child_pid == 0:
            # --- child process ---
            os.execvp(self.cmd[0], self.cmd)
            os._exit(127)  # only reached if execvp fails

        # --- parent process: copy terminal attrs to PTY slave (via master) ---
        if parent_attrs is not None:
            try:
                termios.tcsetattr(self._master_fd, termios.TCSANOW,
                                  parent_attrs)
            except termios.error:
                pass

    def wait_child(self) -> int:
        """Wait for the child to exit and return its exit code."""
        _, status = os.waitpid(self._child_pid, 0)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        return 1

    # ── Terminal setup / teardown ─────────────────────────────────────────

    def enter_raw_mode(self) -> None:
        """
        Save the current terminal attributes and switch stdin to raw mode.

        Raw mode delivers every keystroke immediately (no line buffering,
        no echo, no signal generation) so the proxy can forward them to the PTY
        without delay.
        """
        try:
            self._old_tty_attrs = termios.tcgetattr(STDIN_FILENO)
            tty.setraw(STDIN_FILENO)
        except termios.error:
            self._old_tty_attrs = None

    def restore_terminal(self) -> None:
        """Restore the terminal to its state before :meth:`enter_raw_mode`."""
        if self._old_tty_attrs is not None:
            try:
                termios.tcsetattr(STDIN_FILENO, termios.TCSAFLUSH,
                                  self._old_tty_attrs)
            except termios.error:
                pass

    def sync_window_size(self) -> None:
        """Copy the real terminal's window size to the PTY."""
        try:
            ws = fcntl.ioctl(STDOUT_FILENO, termios.TIOCGWINSZ, b"\x00" * 8)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, ws)
        except (OSError, termios.error):
            pass

    # ── Main I/O loop ─────────────────────────────────────────────────────

    def _filter_agent_output(self, data: bytes) -> bytes:
        """
        Filter escape sequences in agent output before forwarding to the
        real terminal.

        Currently handles:
        - **Focus tracking** (``\\x1b[?1004h`` / ``\\x1b[?1004l``):
          Intercepted so the outer terminal never enables focus event
          reporting.  Instead, a fake "focus-in" event is sent back to
          the agent so it always believes the terminal is focused.
          This is critical for Ink-based TUI applications (e.g. Copilot
          CLI) that disable input on focus-out.
        """
        if FOCUS_TRACKING_ENABLE in data:
            data = data.replace(FOCUS_TRACKING_ENABLE, b"")
            if not self._focus_tracking_active:
                self._focus_tracking_active = True
                # Tell the child it is focused.
                self._ibuf += FOCUS_IN_EVENT
                logger.debug("Intercepted focus-tracking enable; "
                             "sent focus-in to child")
        if FOCUS_TRACKING_DISABLE in data:
            data = data.replace(FOCUS_TRACKING_DISABLE, b"")
            self._focus_tracking_active = False
        return data

    def _filter_stdin(self, data: bytes) -> bytes:
        """
        Filter escape sequences from the outer terminal before forwarding
        to the child PTY.

        Currently handles:
        - **Focus-out events** (``\\x1b[O``): Dropped so the child never
          sees a "terminal lost focus" event.  In a PTY proxy the child's
          terminal is always logically connected and focused.
        """
        if FOCUS_OUT_EVENT in data:
            data = data.replace(FOCUS_OUT_EVENT, b"")
            logger.debug("Filtered focus-out event from stdin")
        return data

    def copy_loop(self, timeout: float = 0.5) -> None:
        """
        Non-blocking copy loop (mirrors :func:`pty._copy`).

        Reads from STDIN and the PTY master, buffers the data, and writes
        it out when the target fd is ready.  A *timeout* on ``select()``
        allows callers (subclasses) to do periodic work between iterations
        by overriding :meth:`on_idle`.

        The master fd is set to non-blocking so a large burst of output
        from the agent (or a sluggish terminal) cannot block the loop and
        starve keyboard input.

        Agent output is filtered through :meth:`_filter_agent_output` and
        stdin data through :meth:`_filter_stdin` to handle escape
        sequences that would otherwise break double-PTY proxying (most
        notably terminal focus-out events).
        """
        master_fd = self._master_fd
        self._running = True

        # --- set master_fd to non-blocking (critical for responsiveness) ---
        os.set_blocking(master_fd, False)

        stdin_open = master_fd != STDIN_FILENO
        stdout_open = master_fd != STDOUT_FILENO

        try:
            while self._running:
                rfds: list[int] = []
                wfds: list[int] = []

                # Read from stdin if buffer has room
                if stdin_open and len(self._ibuf) < self._HIGH_WATER:
                    rfds.append(STDIN_FILENO)

                # Read from master if buffer has room
                if stdout_open and len(self._obuf) < self._HIGH_WATER:
                    rfds.append(master_fd)

                # Write to stdout if there is buffered output
                if stdout_open and self._obuf:
                    wfds.append(STDOUT_FILENO)

                # Write to master if there is buffered input
                if self._ibuf:
                    wfds.append(master_fd)

                try:
                    rfds, wfds, _ = select.select(rfds, wfds, [], timeout)
                except (OSError, ValueError):
                    break

                # --- Write agent output to the real terminal ---
                if STDOUT_FILENO in wfds:
                    try:
                        n = os.write(STDOUT_FILENO, self._obuf)
                        self._obuf = self._obuf[n:]
                    except OSError:
                        stdout_open = False

                # --- Read agent output from the PTY ---
                if master_fd in rfds:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break  # child closed its side → exit loop
                    data = self._filter_agent_output(data)
                    self._obuf += data
                    self.on_agent_output(data)

                # --- Write user keystrokes to the agent PTY ---
                if master_fd in wfds:
                    try:
                        n = os.write(master_fd, self._ibuf)
                        self._ibuf = self._ibuf[n:]
                    except OSError:
                        pass  # agent not ready to read yet

                # --- Read user keystrokes ---
                if stdin_open and STDIN_FILENO in rfds:
                    try:
                        data = os.read(STDIN_FILENO, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        stdin_open = False
                    else:
                        data = self._filter_stdin(data)
                        if data:  # may be empty after filtering
                            self._ibuf += data
                            self.on_user_input(data)

                # No events → let subclass do periodic work
                if not rfds and not wfds:
                    self.on_idle()

        finally:
            os.set_blocking(master_fd, True)  # restore for waitpid

    def stop(self) -> None:
        """Signal the copy loop to exit on its next iteration."""
        self._running = False

    def inject_input(self, text: str) -> None:
        """
        Enqueue *text* to be written to the agent's stdin.

        The text is appended to ``_ibuf`` and will be flushed on the next
        ``select()`` iteration when the master fd is write-ready.
        Thread-safe (GIL protects the bytestring append).
        """
        self._ibuf += text.encode("utf-8")

    def inject_and_submit(self, text: str, delay: float = 0.3) -> None:
        """
        Inject *text* followed by Enter (``\\r``), with a delay between them.

        TUI frameworks like Ink (used by Copilot CLI) process stdin in
        ``data`` events.  If the text and Enter arrive in the same chunk,
        the Enter is consumed as text content rather than triggering a
        submit action.  This method injects the text first, waits for
        *delay* seconds (allowing the I/O loop to flush the text and the
        TUI to process it), then writes ``\\r`` directly to the PTY
        master fd — guaranteeing it arrives as a separate write/data event.

        Runs synchronously — call from a background thread.
        """
        self._ibuf += text.encode("utf-8")
        time.sleep(delay)
        # Write \r directly to the PTY master fd so it is guaranteed to
        # be a separate os.write() call, never bundled with the text.
        try:
            os.write(self._master_fd, b"\r")
        except OSError:
            # Fallback: queue through the buffer.
            self._ibuf += b"\r"

    # ── Hooks for subclasses ──────────────────────────────────────────────

    def on_agent_output(self, data: bytes) -> None:
        """Called with every chunk the agent writes.  Override to inspect."""

    def on_user_input(self, data: bytes) -> None:
        """Called with every chunk the user types.  Override to track."""

    def on_idle(self) -> None:
        """Called when ``select()`` times out.  Override for periodic work."""


# ═══════════════════════════════════════════════════════════════════════════════
#  AgentStateDetector — Output analysis for state classification
# ═══════════════════════════════════════════════════════════════════════════════

# Default patterns that indicate the agent is asking the user a question.
DEFAULT_ASK_PATTERNS = [
    r"\?\s*$",       # line ending with ?
    r"\[y/n\]",      # [y/n] prompt
    r"\[Y/n\]",      # [Y/n] prompt
    r"\[yes/no\]",   # [yes/no] prompt
    r"Enter choice",
    r"Select.*:",
    r"Confirm\?",
    r"Continue\?",
    r"Proceed\?",
    r"Press Enter",
    r"Type .* to continue",
]

# How long after last output before considering the agent "idle" (fallback).
SILENCE_TIMEOUT_S = 3.0


class AgentStateDetector:
    """
    Classify an agent's current state by analysing its terminal output.

    States
    ------
    BUSY          The agent is actively producing output.
    ASKING_USER   The agent printed a question and is waiting for the user.
    READY         The agent is idle (explicit prompt matched or silence timeout).

    The detector exposes :attr:`state` and the convenience method
    :meth:`is_ready_for_injection` which also considers user-typing recency.
    """

    def __init__(
        self,
        ready_pattern: str | None = None,
        ask_patterns: list[str] | None = None,
        silence_timeout: float = SILENCE_TIMEOUT_S,
        verbose: bool = False,
    ):
        # Compile the optional "ready" regex (matches the agent's prompt).
        self.ready_re = re.compile(ready_pattern) if ready_pattern else None

        # Compile "asking" patterns (questions directed at the user).
        self.ask_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (ask_patterns or DEFAULT_ASK_PATTERNS)
        ]

        self.silence_timeout = silence_timeout
        self.verbose = verbose

        # Current state and timing bookkeeping.
        self.state: str = AGENT_STATE_BUSY
        self._last_output_time: float = time.monotonic()
        self._last_output_line: str = ""
        self._user_typing_time: float = 0.0
        # Latched True once the ready_pattern has matched at least once.
        # Used by the startup guide injector — Ink-based TUIs keep redrawing
        # the cursor/footer so silence_timeout never fires, but the banner
        # text appears once and means the agent is initialised.
        self.ever_ready: bool = False

    # ── Public interface ──────────────────────────────────────────────────

    def analyse_output(self, data: bytes) -> None:
        """
        Feed agent output and update :attr:`state` accordingly.

        Call this with every chunk read from the PTY master fd.
        """
        self._last_output_time = time.monotonic()

        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return

        clean = strip_ansi(text)

        # --- Check each line for "asking user" patterns ---
        for line in clean.split("\n"):
            stripped = line.rstrip()
            if not stripped:
                continue
            self._last_output_line = stripped
            for pat in self.ask_patterns:
                if pat.search(stripped):
                    self.state = AGENT_STATE_ASKING_USER
                    if self.verbose:
                        logger.debug("State → ASKING_USER (matched: %s)", stripped)
                    return

        # --- Check for explicit "ready" prompt pattern ---
        if self.ready_re:
            last_segment = strip_ansi(text.split("\n")[-1])
            # Match either on the last line (classic prompt patterns like
            # "^> $") or anywhere in the cleaned chunk (banner-style
            # patterns used by Ink TUIs that emit text mixed with cursor
            # positioning).
            if self.ready_re.search(last_segment) or self.ready_re.search(clean):
                self.state = AGENT_STATE_READY
                self.ever_ready = True
                if self.verbose:
                    logger.debug("State → READY (pattern match)")
                return

        # --- Default: agent is producing output → BUSY ---
        self.state = AGENT_STATE_BUSY

    def record_user_input(self) -> None:
        """Mark that the user just typed something (updates recency clock)."""
        self._user_typing_time = time.monotonic()

    def is_ready_for_injection(self) -> bool:
        """
        Return ``True`` if it is safe to inject a Pluto message right now.

        Injection is blocked when:
        - The agent is asking the user a question.
        - The user typed something in the last 5 seconds.
        """
        now = time.monotonic()

        # Never inject while the agent is prompting the user.
        if self.state == AGENT_STATE_ASKING_USER:
            return False

        # Never inject while the user is actively typing.
        if now - self._user_typing_time < 5.0:
            return False

        # Ready if explicit prompt pattern matched.
        if self.state == AGENT_STATE_READY:
            return True

        # Fallback: ready if agent has been silent for silence_timeout.
        if now - self._last_output_time >= self.silence_timeout:
            return True

        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  MessageFormatter — Convert Pluto messages to natural-language prompts
# ═══════════════════════════════════════════════════════════════════════════════

class MessageFormatter:
    """
    Turn Pluto protocol messages (JSON dicts) into natural-language text
    that any LLM-based agent can understand and act on.

    Each Pluto event type gets its own formatting template so the agent
    receives clear, actionable instructions.
    """

    @staticmethod
    def format(messages: list[dict]) -> str:
        """
        Format one or more Pluto messages into a single injection string.

        Parameters
        ----------
        messages : list[dict]
            Raw message dicts from the Pluto long-poll response.

        Returns
        -------
        str
            A multi-line text block ready to be injected into the agent's stdin.
        """
        parts: list[str] = []

        for msg in messages:
            event = msg.get("event", "message")
            sender = msg.get("from", "unknown")
            payload = msg.get("payload", {})

            if event == "message":
                parts.append(
                    f"[Pluto Message from {sender}]\n"
                    f"{json.dumps(payload, indent=2)}"
                )
            elif event == "broadcast":
                parts.append(
                    f"[Pluto Broadcast from {sender}]\n"
                    f"{json.dumps(payload, indent=2)}"
                )
            elif event == "task_assigned":
                task_id = msg.get("task_id", "?")
                desc = msg.get("description", "")
                parts.append(
                    f"[Pluto Task Assignment - {task_id}]\n"
                    f"From: {sender}\n"
                    f"Description: {desc}\n"
                    f"Payload: {json.dumps(payload, indent=2)}\n"
                    f"\nWork on this task. When done, update it with "
                    f'pluto_task_update("{task_id}", "completed", '
                    f'{{"result": ...}}).'
                )
            elif event == "topic_message":
                topic = msg.get("topic", "?")
                parts.append(
                    f"[Pluto Topic '{topic}' from {sender}]\n"
                    f"{json.dumps(payload, indent=2)}"
                )
            else:
                # Unknown event type — show the raw JSON.
                parts.append(
                    f"[Pluto Event: {event}]\n"
                    f"{json.dumps(msg, indent=2)}"
                )

        header = (
            "You have received the following Pluto coordination messages. "
            "Process them and take appropriate action.\n\n"
        )
        return header + "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
#  PlutoConnection — Pluto server session management
# ═══════════════════════════════════════════════════════════════════════════════

class PlutoConnection:
    """
    Manage a persistent HTTP session with the Pluto coordination server.

    Handles:
    - Registration (with configurable TTL).
    - Background long-polling for incoming messages.
    - Graceful unregistration on shutdown.

    Call :meth:`start_polling` to launch the background thread and
    :meth:`stop` to tear down.  Received messages accumulate in
    :meth:`drain_messages`.
    """

    def __init__(
        self,
        agent_id: str,
        host: str = "localhost",
        http_port: int = 9001,
        poll_timeout: int = 15,
        ttl_ms: int = 600_000,
        verbose: bool = False,
    ):
        self.agent_id = agent_id
        self.host = host
        self.http_port = http_port
        self.poll_timeout = poll_timeout
        self.ttl_ms = ttl_ms
        self.verbose = verbose

        self._client: PlutoHttpClient | None = None
        self._poll_thread: threading.Thread | None = None
        self._running = False
        self._messages: list[dict] = []
        self._lock = threading.Lock()

    # ── Connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Register with the Pluto server.  Returns ``True`` on success.

        If the server is unreachable, logs a warning and returns ``False``
        so the caller can continue in standalone mode.
        """
        try:
            self._client = PlutoHttpClient(
                host=self.host,
                http_port=self.http_port,
                agent_id=self.agent_id,
                mode="http",
                ttl_ms=self.ttl_ms,
            )
            resp = self._client.register()
            if resp.get("status") != "ok":
                logger.warning("Pluto registration failed: %s", resp)
                self._client = None
                return False

            # The server may assign a different agent ID.
            actual = resp.get("agent_id", self.agent_id)
            if actual != self.agent_id:
                self.agent_id = actual

            return True

        except Exception as exc:
            logger.warning("Cannot connect to Pluto: %s", exc)
            self._client = None
            return False

    def disconnect(self) -> None:
        """Unregister from the Pluto server and stop polling."""
        self._running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        if self._client:
            try:
                self._client.unregister()
            except Exception:
                pass
            self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def token(self) -> str:
        """Return the first 12 chars of the session token (for display)."""
        if self._client and self._client.token:
            return self._client.token[:12]
        return "?"

    # ── Polling ───────────────────────────────────────────────────────────

    def start_polling(self) -> None:
        """Launch the background long-poll thread."""
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="pluto-poll"
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        """Background: repeatedly long-poll the server for new messages."""
        while self._running and self._client:
            try:
                msgs = self._client.long_poll(
                    timeout=self.poll_timeout, ack=True
                )
                if msgs:
                    with self._lock:
                        self._messages.extend(msgs)
                    if self.verbose:
                        logger.debug("Received %d Pluto message(s)", len(msgs))
            except (PlutoError, Exception) as exc:
                logger.warning("Pluto poll error: %s", exc)
                time.sleep(5)

    def drain_messages(self) -> list[dict]:
        """Return and clear all pending messages (thread-safe)."""
        with self._lock:
            msgs = self._messages
            self._messages = []
        return msgs

    def has_messages(self) -> bool:
        """Check if there are pending messages without draining them."""
        with self._lock:
            return bool(self._messages)


# ═══════════════════════════════════════════════════════════════════════════════
#  PlutoAgentFriend — Top-level orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class PlutoAgentFriend(TerminalProxy):
    """
    Launch an agent CLI in a PTY with Pluto coordination.

    Inherits from :class:`TerminalProxy` for the core I/O loop and composes
    :class:`AgentStateDetector`, :class:`PlutoConnection`, and
    :class:`MessageFormatter` for the higher-level behaviour.

    Parameters
    ----------
    cmd : list[str]
        The agent command to execute (e.g. ``["claude"]``).
    agent_id : str
        Unique identifier for this agent in the Pluto network.
    pluto_host, pluto_http_port : str, int
        Pluto server coordinates.
    ready_pattern : str | None
        Optional regex matching the agent's "ready for input" prompt.
    ask_patterns : list[str] | None
        Regexes that indicate the agent is asking the user a question.
    mode : str
        Injection mode: ``"auto"``, ``"confirm"``, or ``"manual"``.
    poll_timeout : int
        Seconds for each Pluto long-poll request.
    silence_timeout : float
        Seconds of silence before the agent is considered idle.
    verbose : bool
        Enable debug logging.
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
        verbose: bool = False,
    ):
        super().__init__(cmd)

        self.agent_id = agent_id
        self.mode = mode
        self.verbose = verbose
        self.guide_file = guide_file

        # --- Composed components ---
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

        # Injection thread handle.
        self._injection_thread: threading.Thread | None = None
        self._guide_injected = False

    # ── Public entry point ────────────────────────────────────────────────

    def run(self) -> int:
        """
        Run the full PlutoAgentFriend lifecycle.  Returns the child exit code.

        1. Print banner.
        2. Connect to Pluto (or continue standalone).
        3. Spawn the agent in a PTY.
        4. Set the real terminal to raw mode.
        5. Start Pluto polling + injection threads.
        6. Enter the non-blocking I/O copy loop.
        7. Restore terminal and disconnect on exit.
        """
        self._print_banner()

        # --- Pluto connection (optional — agent works without it) ---
        if self.pluto.connect():
            self._info(
                f"Connected to Pluto at {self.pluto.host}:{self.pluto.http_port} "
                f"(token: {self.pluto.token}...)"
            )
        else:
            self._info("Starting without Pluto (messages won't be injected)")

        # --- Spawn the agent process ---
        self.spawn()

        # --- Terminal setup ---
        self.sync_window_size()
        signal.signal(signal.SIGWINCH, self._handle_sigwinch)
        self.enter_raw_mode()

        # --- Background threads ---
        if self.pluto.connected:
            self.pluto.start_polling()
            self._injection_thread = threading.Thread(
                target=self._injection_loop, daemon=True, name="inject"
            )
            self._injection_thread.start()

        # --- Startup guide injection thread ---
        if self.guide_file:
            threading.Thread(
                target=self._guide_injection_loop, daemon=True,
                name="guide-inject",
            ).start()

        # --- Main I/O loop ---
        exit_code = 1
        try:
            self.copy_loop(timeout=0.5)
            exit_code = self.wait_child()
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
        """
        Background thread: wait for the agent to become idle for the first
        time after startup, then inject a prompt telling it to read the
        skill guide file.
        """
        # Give the agent a moment to start its TUI.
        time.sleep(2)

        # Wait until the agent has signalled readiness at least once
        # (ready_pattern matched) OR has been idle long enough.  Ink-based
        # TUIs (Copilot) constantly redraw, so silence never fires — the
        # ever_ready latch handles that case.
        deadline = time.monotonic() + 60.0
        while self._running and time.monotonic() < deadline:
            if self.detector.ever_ready:
                # Pattern matched — small grace period for TUI to settle.
                time.sleep(0.5)
                break
            if self.detector.is_ready_for_injection():
                break
            time.sleep(0.2)
        else:
            if self._running:
                logger.debug("Guide injection: agent never became idle")
            return

        if self._guide_injected or not self._running:
            return
        self._guide_injected = True

        guide_basename = os.path.basename(self.guide_file)
        prompt = (
            f"Read the file {guide_basename} in this project root. "
            f"This is your skill guide for working with PlutoAgentFriend "
            f"— the coordination wrapper you are currently running inside. "
            f"Internalize the instructions so you know how to receive Pluto "
            f"messages, lock resources, send messages to other agents, and "
            f"update tasks. Confirm briefly when done."
        )
        self._info(f"Injecting startup guide: {guide_basename}")
        self.inject_and_submit(prompt)
        self.detector.state = AGENT_STATE_BUSY

    # ── Injection logic ───────────────────────────────────────────────────

    def _injection_loop(self) -> None:
        """
        Background thread: periodically check for pending Pluto messages
        and inject them when the agent is ready.
        """
        while self._running:
            time.sleep(0.5)

            if not self.pluto.has_messages():
                continue

            if not self.detector.is_ready_for_injection():
                continue

            messages = self.pluto.drain_messages()
            if not messages:
                continue

            if self.mode == "auto":
                self._do_inject(messages)
            elif self.mode == "confirm":
                self._notify_pending(messages)
                self._wait_confirm_then_inject(messages)
            elif self.mode == "manual":
                self._notify_pending(messages)
                # Manual = notification only; messages are already drained.

    def _do_inject(self, messages: list[dict]) -> None:
        """Format and inject messages into the agent's stdin buffer."""
        prompt = self.formatter.format(messages)
        self._info(f"Injecting {len(messages)} message(s) from Pluto")
        self.inject_and_submit(prompt)
        self.detector.state = AGENT_STATE_BUSY

    def _notify_pending(self, messages: list[dict]) -> None:
        """Show the user a preview of pending messages (on stderr)."""
        for msg in messages:
            event = msg.get("event", "message")
            sender = msg.get("from", "unknown")
            payload = msg.get("payload", {})
            preview = json.dumps(payload)[:80]
            self._notify(f"Pending [{event}] from {sender}: {preview}")

    def _wait_confirm_then_inject(self, messages: list[dict]) -> None:
        """
        Confirm mode: show notification, then auto-inject after 10 s
        if the agent is still idle.
        """
        self._notify(
            "Press Enter in the agent to accept injection (auto in 10s)..."
        )
        deadline = time.monotonic() + 10.0
        while self._running and time.monotonic() < deadline:
            time.sleep(0.5)
            if not self.detector.is_ready_for_injection():
                return  # user started interacting → cancel
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
#  Agent Framework Detection (module-level helpers)
# ═══════════════════════════════════════════════════════════════════════════════

KNOWN_FRAMEWORKS = {
    "claude": {
        "cmd_names": ["claude"],
        "display": "Claude Code",
        "default_args": [],
        "ready_pattern": None,  # Claude uses silence timeout
    },
    "copilot": {
        "cmd_names": ["copilot"],
        "display": "GitHub Copilot CLI",
        "default_args": [],
        "ready_pattern": r"Describe a task to get started",
    },
    "aider": {
        "cmd_names": ["aider"],
        "display": "Aider",
        "default_args": [],
        "ready_pattern": r"^[>›] $",
    },
    "cursor": {
        "cmd_names": ["cursor"],
        "display": "Cursor",
        "default_args": [],
        "ready_pattern": None,
    },
}


def detect_available_frameworks() -> list[dict]:
    """
    Scan ``$PATH`` for known agent framework executables.

    Returns a list of dicts with keys: ``key``, ``display``, ``cmd``, ``path``.
    """
    import shutil

    available = []
    for key, info in KNOWN_FRAMEWORKS.items():
        for cmd_name in info["cmd_names"]:
            path = shutil.which(cmd_name)
            if path:
                available.append({
                    "key": key,
                    "display": info["display"],
                    "cmd": cmd_name,
                    "path": path,
                })
                break
    return available


def get_framework_cmd(framework: str) -> list[str]:
    """Return the command list for a known framework, or ``[framework]``."""
    info = KNOWN_FRAMEWORKS.get(framework)
    if not info:
        return [framework]
    return info["cmd_names"][:1] + info["default_args"]


def get_framework_ready_pattern(framework: str) -> str | None:
    """Return the prompt-ready regex for a framework, or ``None``."""
    info = KNOWN_FRAMEWORKS.get(framework)
    return info["ready_pattern"] if info else None


# ═══════════════════════════════════════════════════════════════════════════════
#  Pluto Server Status & Config Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def check_pluto_status(host: str, http_port: int) -> dict | None:
    """
    Hit the Pluto ``/health`` endpoint and return the response dict,
    or ``None`` if the server is unreachable.
    """
    import urllib.request

    try:
        url = f"http://{host}:{http_port}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def load_pluto_config() -> dict:
    """
    Load ``config/pluto_config.json`` from the project root.

    Searches in two locations:
    1. Relative to this file (``../../config/pluto_config.json``).
    2. Relative to the current working directory.

    Returns an empty dict if no config file is found.
    """
    candidates = [
        os.path.normpath(
            os.path.join(_THIS_DIR, "..", "..", "config", "pluto_config.json")
        ),
        os.path.normpath(
            os.path.join(os.getcwd(), "config", "pluto_config.json")
        ),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    return {}


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
             "The agent will be prompted to read it when it first becomes "
             "idle.  Defaults to agent_friend_guide.md if it exists.",
    )
    parser.add_argument(
        "--no-guide", action="store_true",
        help="Disable automatic guide injection even if the file exists.",
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
            cmd = get_framework_cmd(args.framework)
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
                cmd = [fw["cmd"]]
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
                        cmd = [fw["cmd"]]
                        if not ready_pattern:
                            ready_pattern = get_framework_ready_pattern(
                                fw["key"])
                    else:
                        print("Invalid choice.", file=sys.stderr)
                        sys.exit(1)
                except (ValueError, EOFError):
                    print("Invalid choice.", file=sys.stderr)
                    sys.exit(1)

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
    else:
        print(
            f"{YELLOW}[pluto-friend]{NC} Pluto server is {YELLOW}OFFLINE{NC} "
            f"at {host}:{http_port}",
            file=sys.stderr,
        )

    # --- Resolve guide file ---
    guide_file = None
    if not args.no_guide:
        if args.guide:
            guide_file = os.path.abspath(args.guide)
        else:
            # Auto-discover agent_friend_guide.md in project root
            for candidate in [
                os.path.join(os.getcwd(), "agent_friend_guide.md"),
                os.path.normpath(
                    os.path.join(_THIS_DIR, "..", "..",
                                 "agent_friend_guide.md")
                ),
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
        verbose=args.verbose,
    )
    sys.exit(friend.run())


if __name__ == "__main__":
    main()
