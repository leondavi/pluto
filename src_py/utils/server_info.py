#!/usr/bin/env python3
"""Query the Pluto server for detailed server information (server_info op).

Returns a JSON object with version, OTP release, node name, IPs, uptime,
memory, live counters, etc.  Used by PlutoServer.sh --status to render a
rich status display.

Exit codes:
  0  — success, JSON printed to stdout
  1  — server unreachable or returned an error
"""

import json
import socket
import sys

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9000
TIMEOUT_S = 3


def server_info(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=TIMEOUT_S):
    """Send a server_info request and return the parsed JSON response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(json.dumps({"op": "server_info"}).encode() + b"\n")

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk

        if not buf.strip():
            raise ConnectionError("Empty response from server")

        return json.loads(buf.decode())
    finally:
        sock.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Query Pluto server info")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S,
                        help="Connection timeout in seconds")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress error messages on failure")
    args = parser.parse_args()

    try:
        info = server_info(args.host, args.port, args.timeout)
        print(json.dumps(info))
        return 0
    except Exception as exc:
        if not args.quiet:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
