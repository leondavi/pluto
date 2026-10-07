"""Command-line interface behind ``PlutoClient.sh`` / ``pluto_client.py``.

Subcommands: ``ping``, ``list``, ``stats`` (TCP) and ``guide`` (offline).
"""

import argparse
import sys

from pluto_client_def import (
    CLI_DESCRIPTION,
    CLI_EPILOG,
    DEFAULT_AGENT_ID,
    DEFAULT_GUIDE_OUTPUT_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    PLUTO_LOGO,
)
from pluto_sdk.errors import PlutoError
from pluto_sdk.guide import generate_agent_guide
from pluto_sdk.tcp_client import PlutoClient


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pluto_client",
        description=CLI_DESCRIPTION,
        epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, metavar="HOST",
                        help=f"Pluto server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="PORT",
                        help=f"Pluto server port (default: {DEFAULT_PORT})")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID, metavar="ID",
                        dest="agent_id",
                        help=f"Agent identifier used for registration (default: {DEFAULT_AGENT_ID})")

    subparsers = parser.add_subparsers(dest="command", metavar="{ping,list,stats,guide}")

    # ping
    subparsers.add_parser(
        "ping",
        help="Verify connectivity to a Pluto server.",
        description="Register with the server and confirm the connection is live.",
    )

    # list
    subparsers.add_parser(
        "list",
        help="List all agent IDs currently connected to the server.",
        description="Connect to the server and return the list of active agents.",
    )

    # stats
    subparsers.add_parser(
        "stats",
        help="Query server statistics (locks, messages, deadlocks, per-agent).",
        description="Connect to the server and retrieve runtime statistics.",
    )

    # guide
    guide_p = subparsers.add_parser(
        "guide",
        help="Generate the Pluto agent guide to a file (and print to stdout).",
        description=(
            "Render the agent guide template with the given host/port values,\n"
            "write the result to OUTPUT, and also print it to stdout."
        ),
    )
    guide_p.add_argument(
        "--output", default=DEFAULT_GUIDE_OUTPUT_PATH, metavar="PATH",
        help=f"Destination file for the rendered guide (default: {DEFAULT_GUIDE_OUTPUT_PATH})",
    )

    return parser


def _print_stats(data):
    """Pretty-print server statistics."""
    counters = data.get("counters", {})
    live = data.get("live", {})
    agent_stats = data.get("agent_stats", {})
    uptime_ms = data.get("uptime_ms", 0)

    uptime_s = uptime_ms / 1000 if uptime_ms else 0
    mins, secs = divmod(int(uptime_s), 60)
    hours, mins = divmod(mins, 60)

    print(f"\n  ╔══════════════════════════════════════════╗")
    print(f"  ║         PLUTO SERVER STATISTICS          ║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  Uptime: {hours:02d}h {mins:02d}m {secs:02d}s" + " " * (26 - len(f"{hours:02d}h {mins:02d}m {secs:02d}s")) + "║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  LIVE SNAPSHOT                           ║")
    print(f"  ║    Active locks      : {str(live.get('active_locks', 0)):>16s} ║")
    print(f"  ║    Connected agents  : {str(live.get('connected_agents', 0)):>16s} ║")
    print(f"  ║    Total agents      : {str(live.get('total_agents', 0)):>16s} ║")
    print(f"  ║    Pending waiters   : {str(live.get('pending_waiters', 0)):>16s} ║")
    print(f"  ║    Wait graph edges  : {str(live.get('wait_graph_edges', 0)):>16s} ║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  COUNTERS                                ║")
    for key in sorted(counters.keys()):
        val = counters[key]
        label = key.replace("_", " ").title()
        print(f"  ║    {label:<22s}: {str(val):>10s} ║")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  PER-AGENT STATS                         ║")
    if agent_stats:
        for aid in sorted(agent_stats.keys()):
            stats = agent_stats[aid]
            print(f"  ║  [{aid}]" + " " * max(0, 36 - len(aid)) + "║")
            for k in sorted(stats.keys()):
                label = k.replace("_", " ").title()
                print(f"  ║      {label:<20s}: {str(stats[k]):>8s} ║")
    else:
        print(f"  ║    (none)                                ║")
    print(f"  ╚══════════════════════════════════════════╝\n")


def main() -> None:
    """Entry point: parse argv and run the selected subcommand."""
    print(PLUTO_LOGO)

    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "guide":
        content = generate_agent_guide(
            output_path=args.output,
            host=args.host,
            port=args.port,
        )
        print(content)
        print(f"[pluto] Guide written to: {args.output}")
        return

    # ping and list both require a server connection
    try:
        with PlutoClient(host=args.host, port=args.port, agent_id=args.agent_id) as client:
            print(f"[pluto] Connected  host={args.host}  port={args.port}"
                  f"  session_id={client.session_id}")

            if args.command == "list":
                agents = client.list_agents()
                if agents:
                    print(f"[pluto] Connected agents ({len(agents)}):")
                    for agent in agents:
                        print(f"         · {agent}")
                else:
                    print("[pluto] No agents currently connected.")
            elif args.command == "stats":
                data = client.stats()
                _print_stats(data)
            else:
                print("[pluto] Registration OK — Pluto is reachable.")

    except (OSError, PlutoError) as exc:
        print(f"[pluto] Error: {exc}", file=sys.stderr)
        sys.exit(1)
