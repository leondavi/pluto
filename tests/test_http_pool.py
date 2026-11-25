"""read_timeout must apply to a brand-new pooled connection too."""

import http.server
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_py"))

from pluto_sdk.http_pool import HTTPConnectionPool  # noqa: E402


class _Slow(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        time.sleep(0.6)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def test_read_timeout_on_new_connection_and_restored():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pool = HTTPConnectionPool("127.0.0.1", srv.server_port, default_timeout=0.2)
    try:
        with pool.acquire(read_timeout=3) as conn:
            conn.request("GET", "/")
            assert conn.getresponse().read() == b""
        assert conn.timeout == 0.2 and conn.sock.gettimeout() == 0.2
    finally:
        pool.close_all()
        srv.shutdown()
