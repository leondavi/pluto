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
        "lock_granted", "wait_timeout",
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
        # Highest seq that is settled locally: injected into the agent or
        # classified noise. The server ack trails it, clamped below any
        # message still buffered (see _safe_ack_cursor).
        self._settled_hwm: int = 0
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

    def restore_from_snapshot(self, plut: dict) -> dict:
        """Apply a previously saved .plut snapshot to this connection.

        Must be called after :py:meth:`connect`. Returns the server response
        (with ``reclaimed_locks`` / ``lost_locks``). Raises if no client.
        """
        if self._client is None:
            raise RuntimeError("not connected to Pluto")
        return self._client.restore_from_snapshot(plut)

    def save_snapshot_files(self, output_dir: str) -> tuple:
        """Take a snapshot and write ``<agent_id>.plut`` + recovery markdown."""
        if self._client is None:
            raise RuntimeError("not connected to Pluto")
        return self._client.save_snapshot_files(output_dir)

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
            # The server's periodic "keep pinging" broadcast targets TCP
            # sessions; pre-v0.5.0 servers could leak it to HTTP agents.
            if (top_event == "broadcast" and msg.get("from") == "pluto"
                    and payload.get("type") == "heartbeat_reminder"):
                return True
        return False

    def _should_skip(self, msg: dict) -> bool:
        """Noise, or a task assignment broadcast addressed to another agent.

        The server announces every assignment to every agent; typing other
        agents' assignments into this agent's terminal only distracts it.
        Status updates (``task_updated``) are kept: an orchestrator running
        under the wrapper needs them, and the wrapper cannot tell which
        tasks its agent assigned.
        """
        if self._is_noise(msg):
            return True
        payload = msg.get("payload")
        return (
            msg.get("event") == "broadcast"
            and isinstance(payload, dict)
            and payload.get("event") == "task_assigned"
            and payload.get("assignee") != self.agent_id
        )

    def _safe_ack_cursor(self, candidate: int) -> int:
        """Clamp an ack cursor below every still-buffered message.

        ``ack(up_to)`` deletes EVERY queued message with seq <= up_to on
        the server. Acking past a message that is buffered here but not yet
        injected would drop it server-side, so a failed injection or a
        wrapper restart would lose it. Caller must hold ``self._lock``.
        """
        pending = [int(m["seq_token"]) for m in self._messages if "seq_token" in m]
        if pending:
            candidate = min(candidate, min(pending) - 1)
        return candidate

    def _ack_up_to(self, up_to: int) -> None:
        """Ack through *up_to* and advance the peek cursor.

        Best effort: on failure the cursor stays put and the next ingest
        or delivery retries, since both recompute from ``_settled_hwm``.
        """
        if up_to <= self._last_acked_seq or self._client is None:
            return
        try:
            self._client.ack(up_to)
            self._last_acked_seq = up_to
        except Exception as exc:
            logger.warning("Pluto ack(up_to=%d) failed: %s — will retry", up_to, exc)

    def _ingest(self, msgs: list[dict]) -> None:
        """Buffer fresh actionable messages from one peek and settle noise.

        Noise is acked only up to the safe cursor; anything above it is
        re-peeked and re-classified next cycle, which is harmless.
        """
        actionable = [m for m in msgs if not self._should_skip(m)]
        noise_seqs = [
            int(m["seq_token"]) for m in msgs
            if self._should_skip(m) and "seq_token" in m
        ]
        fresh = []
        with self._lock:
            for m in actionable:
                seq = m.get("seq_token")
                if seq is None or int(seq) in self._seen_seqs:
                    continue
                self._seen_seqs.add(int(seq))
                fresh.append(m)
            self._messages.extend(fresh)
            if noise_seqs:
                self._settled_hwm = max(self._settled_hwm, max(noise_seqs))
            up_to = self._safe_ack_cursor(self._settled_hwm)
        self._ack_up_to(up_to)
        if self.verbose and (actionable or noise_seqs):
            logger.debug(
                "Pluto peek: %d actionable (+%d fresh), %d noise (acked to %d)",
                len(actionable), len(fresh), len(noise_seqs), up_to,
            )

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
                # Fresh session, fresh seq space: reset every cursor.
                self._last_acked_seq = 0
                self._settled_hwm = 0
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
        EPOCH_CHECK_EVERY = 30  # ticks ≈ 30 s at PEEK_INTERVAL_S=1.0
        epoch_tick = 0
        while self._running and self._client:
            # Periodic server-epoch check: if the server was restarted
            # the cached token is dead, but waiting for a real call to
            # fail wastes a roundtrip. Probe /health and re-register up
            # front on mismatch.
            epoch_tick += 1
            if (
                epoch_tick >= EPOCH_CHECK_EVERY
                and self._client is not None
                and self._client.server_epoch is not None
            ):
                epoch_tick = 0
                try:
                    live = self._client.fetch_server_epoch()
                    if live and live != self._client.server_epoch:
                        logger.warning(
                            "Pluto server_epoch changed (cached=%s live=%s); "
                            "re-registering",
                            self._client.server_epoch[:8],
                            live[:8],
                        )
                        if not self._reregister():
                            time.sleep(SESSION_RETRY_BACKOFF_S)
                        continue
                except Exception:
                    # Probe failures are benign — fall through to the
                    # normal peek path, which will surface the real error.
                    pass
            try:
                msgs = self._client.peek(since_token=self._last_acked_seq)
                if msgs:
                    self._ingest(msgs)
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
        """Mark *messages* as successfully injected and ack them.

        They leave the local buffer immediately. The server-side ack is
        clamped below any message still buffered (see
        :meth:`_safe_ack_cursor`), so confirming a later message never
        deletes an earlier one that has not been injected yet.
        """
        seqs = {int(m["seq_token"]) for m in messages if "seq_token" in m}
        if not seqs:
            return
        with self._lock:
            self._messages = [
                m for m in self._messages
                if int(m.get("seq_token", -1)) not in seqs
            ]
            self._settled_hwm = max(self._settled_hwm, max(seqs))
            up_to = self._safe_ack_cursor(self._settled_hwm)
        self._ack_up_to(up_to)

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
