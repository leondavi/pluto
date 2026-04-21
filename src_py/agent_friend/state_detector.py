"""AgentStateDetector — classify the agent's current state from its output."""

import logging
import re
import time

from agent_friend.constants import (
    AGENT_STATE_ASKING_USER,
    AGENT_STATE_BUSY,
    AGENT_STATE_READY,
    DEFAULT_ASK_PATTERNS,
    SILENCE_TIMEOUT_S,
    strip_ansi,
)

logger = logging.getLogger("pluto_agent_friend")


class AgentStateDetector:
    """
    Classify an agent's current state by analysing its terminal output.

    States
    ------
    BUSY          The agent is actively producing output.
    ASKING_USER   The agent printed a question and is waiting for the user.
    READY         The agent is idle (explicit prompt matched or silence timeout).
    """

    def __init__(
        self,
        ready_pattern: str | None = None,
        ask_patterns: list[str] | None = None,
        silence_timeout: float = SILENCE_TIMEOUT_S,
        verbose: bool = False,
    ):
        self.ready_re = re.compile(ready_pattern) if ready_pattern else None

        self.ask_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (ask_patterns or DEFAULT_ASK_PATTERNS)
        ]

        self.silence_timeout = silence_timeout
        self.verbose = verbose

        self.state: str = AGENT_STATE_BUSY
        self._last_output_time: float = time.monotonic()
        self._last_output_line: str = ""
        self._user_typing_time: float = 0.0
        self._prev_content: str = ""
        # Latched True once the ready_pattern has matched at least once.
        self.ever_ready: bool = False

    def analyse_output(self, data: bytes) -> None:
        """Feed agent output and update :attr:`state` accordingly."""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return

        clean = strip_ansi(text)
        content = clean.strip()

        if content and content != self._prev_content:
            self._last_output_time = time.monotonic()
            self._prev_content = content
        elif not content:
            return

        for line in clean.split("\n"):
            stripped = line.rstrip()
            if not stripped:
                continue
            self._last_output_line = stripped
            for pat in self.ask_patterns:
                if pat.search(stripped):
                    self.state = AGENT_STATE_ASKING_USER
                    if self.verbose:
                        logger.debug("State → ASKING_USER (matched: %s)",
                                     stripped)
                    return

        if self.ready_re:
            last_segment = strip_ansi(text.split("\n")[-1])
            if self.ready_re.search(last_segment) or self.ready_re.search(clean):
                self.state = AGENT_STATE_READY
                self.ever_ready = True
                if self.verbose:
                    logger.debug("State → READY (pattern match)")
                return

        self.state = AGENT_STATE_BUSY

    def record_user_input(self) -> None:
        """Mark that the user just typed something (updates recency clock)."""
        self._user_typing_time = time.monotonic()

    def is_ready_for_injection(self) -> bool:
        """Return ``True`` if it is safe to inject a Pluto message right now."""
        now = time.monotonic()

        if self.state == AGENT_STATE_ASKING_USER:
            return False

        if now - self._user_typing_time < 5.0:
            return False

        if self.state == AGENT_STATE_READY:
            return True

        if now - self._last_output_time >= self.silence_timeout:
            return True

        return False
