"""PlutoConnection — HTTP session management, peek/ack polling, in-flight buffer."""

import logging
import threading
import time

from pluto_client import PlutoError, PlutoHttpClient

logger = logging.getLogger("pluto_agent_friend")


class PlutoConnection:
    """
    Manage a persistent HTTP session with the Pluto coordination server.

    At-least-once delivery: peeked messages stay on the server until the
    wrapper successfully injects them and calls :meth:`confirm_delivered`.
    """

    # Events that the agent should never see.
    _NOISE_PAYLOAD_EVENTS = {
        "delivery_ack", "status_update", "heartbeat",
    }

    # Events that carry actionable content for the agent.
    _ACTIONABLE_EVENTS = {
        "message", "broadcast", "task_assigned", "topic_message",
    }

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
        self._seen_seqs: set[int] = set()
        self._last_acked_seq: int = 0
        self._lock = threading.Lock()

    # ── Connection lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        """Register with the Pluto server.  Returns ``True`` on success."""
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

    @property
    def full_token(self) -> str:
        """Return the full session token (for agent API calls)."""
        if self._client and self._client.token:
            return self._client.token
        return ""

    # ── Polling ───────────────────────────────────────────────────────────

    def start_polling(self) -> None:
        """Launch the background long-poll thread."""
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="pluto-poll"
        )
        self._poll_thread.start()

    @classmethod
    def _is_noise(cls, msg: dict) -> bool:
        """Return True if *msg* is infrastructure noise (delivery_ack etc.)."""
        top_event = msg.get("event", "message")
        if top_event not in cls._ACTIONABLE_EVENTS:
            return True
        payload = msg.get("payload") or {}
        if isinstance(payload, dict):
            if payload.get("event") in cls._NOISE_PAYLOAD_EVENTS:
                return True
        return False

    @staticmethod
    def _is_session_lost(exc: BaseException) -> bool:
        """Return True if *exc* indicates the server has forgotten our token.

        Triggers re-registration. Covers both:
          - HTTP 404/401 with reason "session_not_found" (server restart, TTL
            expiry on the server side, or token wiped)
          - Connection-refused style errors during a peek that already had a
            valid token (server bounced; will need a fresh registration once
            it comes back).
        """
        text = str(exc).lower()
        return (
            "session_not_found" in text
            or "404" in text
            or "401" in text
            or "not registered" in text
        )

    def _reregister(self) -> bool:
        """Drop the current HTTP client and create a fresh registration.

        Used when the server has lost our session (restart, TTL expiry).
        Returns True on success. The previous ack-cursor is reset because
        the new session has its own seq_token space.
        """
        logger.warning(
            "Pluto session lost; re-registering as %s ...", self.agent_id
        )
        try:
            if self._client is not None:
                try:
                    self._client.unregister()
                except Exception:
                    pass
            self._client = None
            ok = self.connect()
            if ok:
                self._last_acked_seq = 0
                self._seen_seqs.clear()
                logger.warning(
                    "Pluto re-registered; new token %s",
                    (self._client.token[:12] + "...") if self._client else "?",
                )
            return ok
        except Exception as exc:
            logger.warning("Pluto re-register failed: %s", exc)
            return False

    def _poll_loop(self) -> None:
        """Background: periodically *peek* (non-destructive) the inbox."""
        PEEK_INTERVAL_S = 1.0
        SESSION_RETRY_BACKOFF_S = 5.0
        while self._running and self._client:
            try:
                msgs = self._client.peek(since_token=self._last_acked_seq)
                if msgs:
                    actionable = [m for m in msgs if not self._is_noise(m)]
                    noise_seqs = [
                        int(m["seq_token"]) for m in msgs
                        if self._is_noise(m) and "seq_token" in m
                    ]
                    with self._lock:
                        fresh = []
                        for m in actionable:
                            seq = m.get("seq_token")
                            if seq is None or seq in self._seen_seqs:
                                continue
                            self._seen_seqs.add(int(seq))
                            fresh.append(m)
                        if fresh:
                            self._messages.extend(fresh)
                    if noise_seqs:
                        try:
                            self._client.ack(max(noise_seqs))
                        except Exception:
                            pass
                    if self.verbose and (actionable or noise_seqs):
                        logger.debug(
                            "Pluto peek: %d actionable (+%d fresh), "
                            "%d noise acked",
                            len(actionable), len(fresh) if actionable else 0,
                            len(noise_seqs),
                        )
            except (PlutoError, Exception) as exc:
                if self._is_session_lost(exc):
                    if not self._reregister():
                        time.sleep(SESSION_RETRY_BACKOFF_S)
                    continue
                logger.warning("Pluto peek error: %s", exc)
                time.sleep(SESSION_RETRY_BACKOFF_S)
                continue
            time.sleep(PEEK_INTERVAL_S)

    def drain_messages(self) -> list[dict]:
        """Return all currently buffered messages without acking them."""
        with self._lock:
            return list(self._messages)

    def confirm_delivered(self, messages: list[dict]) -> None:
        """Mark *messages* as successfully injected and ack them."""
        seqs = [int(m["seq_token"]) for m in messages if "seq_token" in m]
        if not seqs:
            return
        up_to = max(seqs)
        try:
            if self._client is not None:
                self._client.ack(up_to)
                self._last_acked_seq = max(self._last_acked_seq, up_to)
        except Exception as exc:
            logger.warning(
                "Pluto ack(up_to=%d) failed: %s — will retry next peek",
                up_to, exc,
            )
            return
        acked = set(seqs)
        with self._lock:
            self._messages = [
                m for m in self._messages
                if int(m.get("seq_token", -1)) not in acked
            ]

    def abort_delivery(self, messages: list[dict]) -> None:
        """Record that *messages* could not be delivered; keep them in buffer."""
        with self._lock:
            present = {
                int(m.get("seq_token", -1)) for m in self._messages
            }
            for m in messages:
                seq = m.get("seq_token")
                if seq is not None and int(seq) not in present:
                    self._messages.append(m)

    def has_messages(self) -> bool:
        """Check if there are pending (unacked) messages."""
        with self._lock:
            return bool(self._messages)
