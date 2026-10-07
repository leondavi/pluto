"""Bounded pool of HTTP/1.1 keep-alive connections.

One pool is created per :class:`~pluto_sdk.http_client.PlutoHttpClient`.
Reusing sockets matters: the ``urllib.urlopen``-per-call pattern this
replaced left hundreds of ``TIME_WAIT`` sockets per minute under load.
"""

import contextlib
import http.client
import queue
from typing import Optional


class HTTPConnectionPool:
    """Small bounded pool of long-lived ``http.client.HTTPConnection``s.

    Created once per ``PlutoHttpClient`` instance and shared across all
    callers (asyncio.to_thread tasks in the MCP adapter, the
    threading.Thread poll loop in the AgentFriend wrapper, and the
    AutoSnapshotter). Uses ``queue.LifoQueue`` for free-list bookkeeping
    — its internal lock makes ``get_nowait``/``put_nowait`` safe under
    arbitrary thread mixes without us adding our own locks.

    Connections are returned to the pool after a clean response so the
    next call reuses the same TCP socket. Any HTTP/connection-level
    error during a request marks the connection bad and discards it
    instead of returning it.
    """

    def __init__(self, host: str, port: int, *,
                 size: int = 4, default_timeout: float = 10.0):
        self._host = host
        self._port = port
        self._default_timeout = default_timeout
        self._size = max(1, int(size))
        self._free: "queue.LifoQueue[http.client.HTTPConnection]" = (
            queue.LifoQueue(maxsize=self._size)
        )

    def _new_conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            self._host, self._port, timeout=self._default_timeout,
        )

    @contextlib.contextmanager
    def acquire(self, *, read_timeout: Optional[float] = None):
        """Yield a connection, returning it to the pool on success."""
        try:
            conn = self._free.get_nowait()
        except queue.Empty:
            conn = self._new_conn()
        # Per-call socket read timeout overrides the connection default.
        # Only effective once the underlying socket exists (i.e. on
        # reused connections); brand-new ones use the default until the
        # first request completes.
        if read_timeout is not None and conn.sock is not None:
            try:
                conn.sock.settimeout(read_timeout)
            except OSError:
                pass
        bad = False
        try:
            yield conn
        except BaseException:
            bad = True
            raise
        finally:
            if bad:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                # Restore the default timeout so the next consumer isn't
                # surprised by a long-poll's relaxed deadline.
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(self._default_timeout)
                    except OSError:
                        pass
                try:
                    self._free.put_nowait(conn)
                except queue.Full:
                    # Pool already saturated — close the overflow.
                    try:
                        conn.close()
                    except Exception:
                        pass

    def close_all(self) -> None:
        """Close every pooled connection. Called from ``unregister``
        so we don't leave half-open sockets at shutdown."""
        while True:
            try:
                conn = self._free.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:
                pass
