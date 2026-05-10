#!/usr/bin/env python3
"""Query the Pluto server for runtime statistics (stats op).

Returns a JSON object with global counters (messages, locks, deadlocks,
agent registrations), per-agent counters, and a live snapshot
(connected agents, active locks, pending waiters). Used by
PlutoServer.sh --stats to render a stats dashboard.

Output modes:
  --json       Print the raw stats JSON (default).
  --render     Print a colourised dashboard (used by PlutoServer.sh --stats).

Exit codes:
  0  — success
  1  — server unreachable or returned an error
"""

import json
import socket
import sys

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9000
TIMEOUT_S = 3

CYAN  = "\033[0;36m"
GREEN = "\033[0;32m"
BOLD  = "\033[1m"
NC    = "\033[0m"


def server_stats(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=TIMEOUT_S):
    """Send a stats request and return the parsed JSON response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(json.dumps({"op": "stats"}).encode() + b"\n")

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


def _fmt_uptime(ms):
    s = ms // 1000
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _fmt_rate(n, per_seconds, uptime_s):
    """Average rate over the uptime, projected to per_seconds.

    Returns "n/a (need ≥<window>)" when uptime hasn't yet covered a full
    window — extrapolating 2 messages in 3 minutes to 900/day is
    misleading, not informative.
    """
    if uptime_s < per_seconds:
        return f"n/a (uptime <{_fmt_window(per_seconds)})"
    rate = n / uptime_s * per_seconds
    if rate >= 100:
        return f"{rate:,.0f}"
    if rate >= 10:
        return f"{rate:,.1f}"
    return f"{rate:,.2f}"


def _fmt_window(seconds):
    """Compact label for a rate window."""
    if seconds >= 86400:
        return "1d"
    if seconds >= 3600:
        return "1h"
    if seconds >= 60:
        return "1m"
    return "1s"


def render_dashboard(data, host, tcp_port, http_port):
    """Render the stats payload as a colourised dashboard to stdout."""
    uptime_ms = int(data.get("uptime_ms", 0))
    # Avoid div-by-zero for fresh starts; floor to 1 ms.
    uptime_s = max(uptime_ms / 1000.0, 1e-9)
    counters = data.get("counters", {})
    live = data.get("live", {})
    agents = data.get("agent_stats", {})

    def g(key, default=0):
        return counters.get(key, default)

    print()
    print(CYAN + r"""
    ╔════════════════════════════════════════════════╗
    ║              PLUTO SERVER — STATS              ║
    ╚════════════════════════════════════════════════╝""" + NC)
    print()
    print(f"  {GREEN}▸ Server{NC}")
    print(f"    Endpoint:         {CYAN}{host}:{tcp_port}{NC} (TCP) / "
          f"{host}:{http_port} (HTTP)")
    print(f"    Uptime:           {GREEN}{_fmt_uptime(uptime_ms)}{NC}")
    print()

    sent = g("messages_sent")
    recv = g("messages_received")
    bcast = g("broadcasts_sent")
    total_msgs = sent + recv + bcast

    print(f"  {GREEN}▸ Messaging{NC}")
    print(f"    {'Messages sent':<19} {CYAN}{sent:>10,}{NC}")
    print(f"    {'Messages received':<19} {CYAN}{recv:>10,}{NC}")
    print(f"    {'Broadcasts':<19} {CYAN}{bcast:>10,}{NC}")
    print(f"    {BOLD}{'Total messages':<19}{NC} {CYAN}{total_msgs:>10,}{NC}")
    print(f"    {'avg msg/sec':<19} {_fmt_rate(total_msgs, 1, uptime_s):>22}")
    print(f"    {'avg msg/min':<19} {_fmt_rate(total_msgs, 60, uptime_s):>22}")
    print(f"    {'avg msg/hour':<19} {_fmt_rate(total_msgs, 3600, uptime_s):>22}")
    print(f"    {'avg msg/day':<19} {_fmt_rate(total_msgs, 86400, uptime_s):>22}")
    print()

    print(f"  {GREEN}▸ Locks{NC}")
    print(f"    {'Acquired':<19} {CYAN}{g('locks_acquired'):>10,}{NC}")
    print(f"    {'Released':<19} {CYAN}{g('locks_released'):>10,}{NC}")
    print(f"    {'Timed out (TTL)':<19} {CYAN}{g('locks_expired'):>10,}{NC}")
    print(f"    {'Renewed':<19} {CYAN}{g('locks_renewed'):>10,}{NC}")
    print(f"    {'Wait events':<19} {CYAN}{g('lock_waits'):>10,}{NC}")
    print()

    print(f"  {GREEN}▸ Deadlocks{NC}")
    print(f"    {'Detected':<19} {CYAN}{g('deadlocks_detected'):>10,}{NC}")
    print(f"    {'Victims':<19} {CYAN}{g('deadlock_victims'):>10,}{NC}")
    print()

    total_req = g('total_requests')
    coord_req = g('coordination_requests')
    maint_req = total_req - coord_req

    print(f"  {GREEN}▸ Agents{NC}")
    print(f"    {'Registered (Total)':<19} {CYAN}{g('agents_registered'):>10,}{NC}")
    print(f"    {'Unregistered (clean)':<21} {CYAN}{g('agents_unregistered_clean'):>8,}{NC}")
    print(f"    {'Disconnected':<19} {CYAN}{g('agents_disconnected'):>10,}{NC}")
    print()
    print(f"  {GREEN}▸ Requests{NC}")
    print(f"    {BOLD}{'Coordination':<19}{NC} {CYAN}{coord_req:>10,}{NC}  "
          f"(register/unregister, locks, messaging, tasks, pub/sub)")
    print(f"    {'Maintenance':<19} {CYAN}{maint_req:>10,}{NC}  "
          f"(ping, stats, queries, admin, ack)")
    print(f"    {'Total':<19} {CYAN}{total_req:>10,}{NC}")
    print()

    total_agents = live.get('total_agents', 0)
    selftest_agents = live.get('selftest_agents', 0)
    selftest_note = f"  ({selftest_agents} selftest)" if selftest_agents > 0 else ""

    print(f"  {GREEN}▸ Live{NC}")
    print(f"    {'Connected agents':<19} {CYAN}{live.get('connected_agents', 0):>10,}{NC}")
    print(f"    {'Total agents':<19} {CYAN}{total_agents:>10,}{NC}{selftest_note}")
    print(f"    {'Active locks':<19} {CYAN}{live.get('active_locks', 0):>10,}{NC}")
    print(f"    {'Pending waiters':<19} {CYAN}{live.get('pending_waiters', 0):>10,}{NC}")
    print(f"    {'Wait-graph edges':<19} {CYAN}{live.get('wait_graph_edges', 0):>10,}{NC}")
    print()

    if agents:
        def activity(s):
            return (s.get("messages_sent", 0)
                    + s.get("messages_received", 0)
                    + s.get("broadcasts_sent", 0)
                    + s.get("locks_acquired", 0))

        ranked = sorted(agents.items(), key=lambda kv: activity(kv[1]), reverse=True)
        top = [(a, s) for a, s in ranked if activity(s) > 0][:10]
        if top:
            print(f"  {GREEN}▸ Top agents (by activity){NC}")
            print(f"    {'agent_id':<24} {'sent':>8} {'recv':>8} {'bcast':>8} {'locks':>8}")
            print(f"    {'-'*24} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
            for agent_id, s in top:
                print(f"    {agent_id[:24]:<24} "
                      f"{s.get('messages_sent', 0):>8,} "
                      f"{s.get('messages_received', 0):>8,} "
                      f"{s.get('broadcasts_sent', 0):>8,} "
                      f"{s.get('locks_acquired', 0):>8,}")
            print()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Query Pluto server stats")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
    parser.add_argument("--http-port", type=int, default=0,
                        help="HTTP port (display only, used by --render)")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S,
                        help="Connection timeout in seconds")
    parser.add_argument("--render", action="store_true",
                        help="Render a colourised dashboard instead of JSON")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress error messages on failure")
    args = parser.parse_args()

    try:
        stats = server_stats(args.host, args.port, args.timeout)
    except Exception as exc:
        if not args.quiet:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.render:
        render_dashboard(stats, args.host, args.port,
                         args.http_port or args.port)
    else:
        print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
