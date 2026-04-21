"""TerminalProxy — low-level PTY management for PlutoAgentFriend.

Spawns a child process inside a PTY and proxies I/O to/from the real
terminal.  Handles non-blocking reads/writes, focus-event interception
(for Ink-based TUIs), and deterministic inject-with-echo-confirmation.
"""

import errno
import fcntl
import logging
import os
import pty
import re
import select
import signal
import termios
import time
import tty

from agent_friend.constants import (
    BRACKETED_PASTE_DISABLE,
    BRACKETED_PASTE_ENABLE,
    FOCUS_IN_EVENT,
    FOCUS_OUT_EVENT,
    FOCUS_TRACKING_DISABLE,
    FOCUS_TRACKING_ENABLE,
    INJECT_SUBMIT_DELAY_S,
    PASTE_END,
    PASTE_START,
    STDIN_FILENO,
    STDOUT_FILENO,
    strip_ansi,
)

logger = logging.getLogger("pluto_agent_friend")


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

    _HIGH_WATER = 4096

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self._child_pid: int = 0
        self._master_fd: int = -1
        self._old_tty_attrs = None
        self._running = False

        self._ibuf = b""
        self._obuf = b""

        self._echo_buf: bytes = b""
        self._echo_total: int = 0
        self._echo_buf_max: int = 65536

        self._focus_tracking_active = False
        self._bracketed_paste_active = False

    # ── Child lifecycle ───────────────────────────────────────────────────

    def spawn(self) -> None:
        """Fork a PTY and exec *self.cmd* in the child process."""
        parent_attrs = None
        try:
            parent_attrs = termios.tcgetattr(STDIN_FILENO)
        except termios.error:
            pass

        self._child_pid, self._master_fd = pty.fork()
        if self._child_pid == 0:
            if hasattr(self, '_pluto_env'):
                for k, v in self._pluto_env.items():
                    os.environ[k] = v
            os.execvp(self.cmd[0], self.cmd)
            os._exit(127)

        if parent_attrs is not None:
            try:
                termios.tcsetattr(self._master_fd, termios.TCSANOW,
                                  parent_attrs)
            except termios.error:
                pass

    def wait_child(self, timeout: float | None = None,
                   escalate_after: float = 2.0) -> int:
        """Wait for the child to exit and return its exit code.

        If *timeout* is given (seconds), the wait is bounded.  After
        *escalate_after* seconds with no exit, SIGTERM is sent; if the
        child still hasn't exited by *timeout*, SIGKILL is sent.
        """
        if timeout is None:
            try:
                _, status = os.waitpid(self._child_pid, 0)
            except (ChildProcessError, OSError):
                return 1
            if os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            if os.WIFSIGNALED(status):
                return 128 + os.WTERMSIG(status)
            return 1

        deadline = time.monotonic() + timeout
        sent_term = False
        sent_kill = False
        while True:
            try:
                pid, status = os.waitpid(self._child_pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                return 1
            if pid != 0:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                if os.WIFSIGNALED(status):
                    return 128 + os.WTERMSIG(status)
                return 1
            now = time.monotonic()
            if not sent_term and now >= deadline - (timeout - escalate_after):
                self._signal_child(signal.SIGTERM)
                sent_term = True
            if not sent_kill and now >= deadline:
                self._signal_child(signal.SIGKILL)
                sent_kill = True
                deadline = now + 1.0  # brief reap window after SIGKILL
            time.sleep(0.05)

    def _signal_child(self, sig: int) -> None:
        """Send *sig* to the child process, ignoring failures."""
        if self._child_pid <= 0:
            return
        try:
            os.kill(self._child_pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # ── Terminal setup / teardown ─────────────────────────────────────────

    def enter_raw_mode(self) -> None:
        """Save the current terminal attributes and switch stdin to raw mode."""
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
        """Intercept focus-tracking / bracketed-paste control sequences
        in agent output before forwarding to the real terminal."""
        if FOCUS_TRACKING_ENABLE in data:
            data = data.replace(FOCUS_TRACKING_ENABLE, b"")
            if not self._focus_tracking_active:
                self._focus_tracking_active = True
                self._ibuf += FOCUS_IN_EVENT
                logger.debug("Intercepted focus-tracking enable; "
                             "sent focus-in to child")
        if FOCUS_TRACKING_DISABLE in data:
            data = data.replace(FOCUS_TRACKING_DISABLE, b"")
            self._focus_tracking_active = False
        if BRACKETED_PASTE_ENABLE in data:
            self._bracketed_paste_active = True
            logger.debug("Bracketed paste mode enabled by agent")
        if BRACKETED_PASTE_DISABLE in data:
            self._bracketed_paste_active = False
            logger.debug("Bracketed paste mode disabled by agent")
        return data

    def _filter_stdin(self, data: bytes) -> bytes:
        """Drop outer-terminal focus-out events before forwarding to the child."""
        if FOCUS_OUT_EVENT in data:
            data = data.replace(FOCUS_OUT_EVENT, b"")
            logger.debug("Filtered focus-out event from stdin")
        return data

    def copy_loop(self, timeout: float = 0.5) -> None:
        """Non-blocking copy loop (mirrors :func:`pty._copy`)."""
        master_fd = self._master_fd
        self._running = True

        os.set_blocking(master_fd, False)

        stdin_open = master_fd != STDIN_FILENO
        stdout_open = master_fd != STDOUT_FILENO

        try:
            while self._running:
                rfds: list[int] = []
                wfds: list[int] = []

                if stdin_open and len(self._ibuf) < self._HIGH_WATER:
                    rfds.append(STDIN_FILENO)

                if stdout_open and len(self._obuf) < self._HIGH_WATER:
                    rfds.append(master_fd)

                if stdout_open and self._obuf:
                    wfds.append(STDOUT_FILENO)

                if self._ibuf:
                    wfds.append(master_fd)

                try:
                    rfds, wfds, _ = select.select(rfds, wfds, [], timeout)
                except (OSError, ValueError):
                    break

                if STDOUT_FILENO in wfds:
                    try:
                        n = os.write(STDOUT_FILENO, self._obuf)
                        self._obuf = self._obuf[n:]
                    except OSError:
                        stdout_open = False

                if master_fd in rfds:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    data = self._filter_agent_output(data)
                    self._obuf += data
                    self._record_echo(data)
                    self.on_agent_output(data)

                if master_fd in wfds:
                    try:
                        n = os.write(master_fd, self._ibuf)
                        self._ibuf = self._ibuf[n:]
                    except OSError:
                        pass

                if stdin_open and STDIN_FILENO in rfds:
                    try:
                        data = os.read(STDIN_FILENO, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        stdin_open = False
                    else:
                        data = self._filter_stdin(data)
                        if data:
                            self._ibuf += data
                            self.on_user_input(data)

                if not rfds and not wfds:
                    self.on_idle()

        finally:
            os.set_blocking(master_fd, True)

    def stop(self) -> None:
        """Signal the copy loop to exit on its next iteration."""
        self._running = False

    def inject_input(self, text: str) -> None:
        """Enqueue *text* to be written to the agent's stdin."""
        self._ibuf += text.encode("utf-8")

    # ── Echo-detection helpers ─────────────────────────────────────────────

    def _record_echo(self, data: bytes) -> None:
        """Append agent output to the rolling echo buffer."""
        self._echo_total += len(data)
        if len(data) >= self._echo_buf_max:
            self._echo_buf = data[-self._echo_buf_max:]
        else:
            self._echo_buf = (self._echo_buf + data)[-self._echo_buf_max:]

    def _echo_since(self, marker: int) -> bytes:
        """Return agent output bytes received since ``marker``."""
        available_start = self._echo_total - len(self._echo_buf)
        if marker <= available_start:
            return self._echo_buf
        offset = marker - available_start
        return self._echo_buf[offset:]

    @staticmethod
    def _normalize_for_echo(data: bytes) -> bytes:
        """Strip ANSI escapes and collapse whitespace for echo matching."""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return b""
        text = strip_ansi(text)
        return "".join(text.split()).encode("utf-8")

    def inject_and_submit_when_echoed(
        self,
        text: str,
        sentinel: str | None = None,
        hard_deadline: float = 15.0,
        poll_interval: float = 0.05,
        submit_on_timeout: bool = True,
    ) -> bool:
        """Inject text and send Enter only after echo confirmation.

        See :mod:`pluto_agent_friend` module docstring for the rationale
        (avoids the race where Enter arrives before an Ink TUI's React
        reconciler has committed pasted text to its input state).
        """
        flat_text = text.replace("\r\n", " ").replace("\n", " ")
        flat_text = flat_text.replace("\r", " ").replace("\t", " ")
        flat_text = re.sub(r" {2,}", " ", flat_text).strip()

        if sentinel is None:
            printable = "".join(c for c in flat_text if 0x20 <= ord(c) < 0x7f)
            sentinel = printable[-32:] if len(printable) >= 8 else printable
        sentinel_norm = "".join(sentinel.split()).encode("utf-8")

        marker = self._echo_total

        payload = flat_text.encode("utf-8")
        if self._bracketed_paste_active:
            self._ibuf += PASTE_START + payload + PASTE_END
        else:
            self._ibuf += payload

        deadline = time.monotonic() + hard_deadline

        while self._ibuf and time.monotonic() < deadline:
            time.sleep(poll_interval)

        detected = False
        if sentinel_norm:
            while time.monotonic() < deadline:
                new_bytes = self._echo_since(marker)
                if sentinel_norm in self._normalize_for_echo(new_bytes):
                    detected = True
                    break
                time.sleep(poll_interval)

        if detected or submit_on_timeout:
            try:
                os.write(self._master_fd, b"\r")
            except OSError:
                self._ibuf += b"\r"
        return detected

    def inject_and_submit(self, text: str, delay: float | None = None) -> None:
        """Inject *text* followed by Enter (``\\r``), with a delay between them."""
        if delay is None:
            delay = INJECT_SUBMIT_DELAY_S

        self._ibuf += text.encode("utf-8")

        flush_deadline = time.monotonic() + 10.0
        while self._ibuf and time.monotonic() < flush_deadline:
            time.sleep(0.05)

        time.sleep(delay)

        try:
            os.write(self._master_fd, b"\r")
        except OSError:
            self._ibuf += b"\r"

    # ── Hooks for subclasses ──────────────────────────────────────────────

    def on_agent_output(self, data: bytes) -> None:
        """Called with every chunk the agent writes.  Override to inspect."""

    def on_user_input(self, data: bytes) -> None:
        """Called with every chunk the user types.  Override to track."""

    def on_idle(self) -> None:
        """Called when ``select()`` times out.  Override for periodic work."""
