#!/usr/bin/env python3
"""Ping the Pluto server and report its status."""

import json
import socket
import sys
import time

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9000
TIMEOUT_S = 3


def ping(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
         timeout: float = TIMEOUT_S) -> dict:
    """Send a ping to the Pluto server and return the parsed response.

    Raises ConnectionError on failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        t0 = time.monotonic()
        sock.connect((host, port))
        sock.sendall(json.dumps({"op": "ping"}).encode() + b"\n")

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk

        elapsed_ms = (time.monotonic() - t0) * 1000
        if not buf.strip():
            raise ConnectionError("Empty response from server")

        resp = json.loads(buf.decode())
        resp["rtt_ms"] = round(elapsed_ms, 1)
        return resp
    finally:
        sock.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ping the Pluto server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S, help="Timeout in seconds")
    parser.add_argument("-q", "--quiet", action="store_true", help="Exit code only")
    args = parser.parse_args()

    try:
        resp = ping(args.host, args.port, args.timeout)
    except (ConnectionError, OSError) as exc:
        if not args.quiet:
            print(f"\033[0;31m✗ Pluto server at {args.host}:{args.port} is DOWN\033[0m")
            print(f"  {exc}")
        return 1

    status = resp.get("status", "?")
    if status != "pong":
        if not args.quiet:
            print(f"\033[0;33m⚠ Unexpected status: {status}\033[0m")
            print(f"  {json.dumps(resp, indent=2)}")
        return 2

    if not args.quiet:
        print(f"\033[0;32m✓ Pluto server at {args.host}:{args.port} is UP\033[0m")
        print(f"  Status   : {status}")
        print(f"  RTT      : {resp['rtt_ms']} ms")
        hb = resp.get("heartbeat_interval_ms")
        if hb is not None:
            print(f"  Heartbeat: {hb} ms")
        ts = resp.get("ts")
        if ts is not None:
            print(f"  Server ts: {ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
