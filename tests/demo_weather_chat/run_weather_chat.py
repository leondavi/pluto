#!/usr/bin/env python3
"""
Weather Chat Demo — 2 Agents × 20 Turns via Pluto
====================================================

Two agents (weather-alice and weather-bob) have a 20-turn conversation
asking each other about the weather in cities around the world.

Modes:
  --direct   (default) Launch standalone Python agent scripts as subprocesses.
  --copilot           Launch real Copilot CLI agents via AgentWrapper.

All coordination (messaging, transcript locking) goes through the real
Pluto Erlang server on port 9000.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC_PY = os.path.join(_PROJECT, "src_py")
_TESTS = os.path.join(_PROJECT, "tests")

if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

PLUTO_HOST = "127.0.0.1"
PLUTO_PORT = 9000
WORK_DIR = "/tmp/pluto_weather_chat"

AGENT_SCRIPTS = {
    "weather-alice": os.path.join(_HERE, "agent_weather_alice.py"),
    "weather-bob":   os.path.join(_HERE, "agent_weather_bob.py"),
}

NUM_TURNS = 20
EXPECTED_MESSAGES = NUM_TURNS * 2 + 2   # 40 Q&A messages + 2 ready handshakes
EXPECTED_LOCKS    = NUM_TURNS            # 1 transcript lock per turn (asker writes)

# ── Cities for reference ──────────────────────────────────────────────────────
CITIES = [
    "Tokyo", "Paris", "New York", "Sydney", "London",
    "Cairo", "Rio de Janeiro", "Moscow", "Mumbai", "Beijing",
    "Toronto", "Berlin", "Dubai", "Seoul", "Rome",
    "Bangkok", "Cape Town", "Buenos Aires", "Singapore", "Reykjavik",
]

# ── Copilot CLI task prompts ─────────────────────────────────────────────────

_TURN_DATA = textwrap.dedent("""\
TURNS = [
    (1,  "Tokyo",          "What's the weather like in Tokyo right now?",
                           "Partly cloudy, 22°C, light breeze from the east, cherry blossoms in bloom"),
    (2,  "Paris",          "How's the weather in Paris today?",
                           "Clear skies, 18°C, gentle southwest wind, perfect for a Seine-side stroll"),
    (3,  "New York",       "What are conditions like in New York?",
                           "Overcast with light rain, 14°C, gusty winds, grab an umbrella for Central Park"),
    (4,  "Sydney",         "How's the weather in Sydney?",
                           "Sunny and warm, 27°C, refreshing sea breeze off Bondi Beach"),
    (5,  "London",         "What's London weather like this morning?",
                           "Foggy morning, 11°C, calm winds, classic London pea-souper clearing by noon"),
    (6,  "Cairo",          "What's the temperature in Cairo?",
                           "Hot and dry, 38°C, dusty haze over the pyramids, stay hydrated"),
    (7,  "Rio de Janeiro", "How's the weather in Rio de Janeiro?",
                           "Tropical showers, 30°C, high humidity, expect sunshine between rain bursts"),
    (8,  "Moscow",         "What are conditions in Moscow today?",
                           "Light snow, -5°C, crisp north wind, the Kremlin looks magical in white"),
    (9,  "Mumbai",         "What's the forecast for Mumbai?",
                           "Monsoon rain, 29°C, heavy downpour, roads may be waterlogged"),
    (10, "Beijing",        "How's the weather in Beijing?",
                           "Hazy, 20°C, light wind, air quality moderate, masks advisable"),
    (11, "Toronto",        "What's the weather like in Toronto?",
                           "Crisp autumn day, 8°C, clear skies, perfect for a walk along the lakeshore"),
    (12, "Berlin",         "How's the weather in Berlin?",
                           "Steady drizzle, 12°C, overcast skies, a cozy day for museum hopping"),
    (13, "Dubai",          "What's the temperature in Dubai today?",
                           "Scorching sun, 42°C, dry desert heat, stay indoors during peak hours"),
    (14, "Seoul",          "How's the weather in Seoul?",
                           "Cherry blossom season, 16°C, mild breeze, Yeouido Park is stunning"),
    (15, "Rome",           "What's the weather like in Rome?",
                           "Mediterranean warmth, 24°C, sunny skies, gelato weather by the Colosseum"),
    (16, "Bangkok",        "How's the weather in Bangkok today?",
                           "Steamy heat, 35°C, afternoon thunderstorm building, seek shelter by 3 PM"),
    (17, "Cape Town",      "What are conditions like in Cape Town?",
                           "Windy and cool, 17°C, partly cloudy, Table Mountain hidden by cloud cloth"),
    (18, "Buenos Aires",   "How's the weather in Buenos Aires?",
                           "Pleasant autumn, 19°C, light clouds, great day for tango in San Telmo"),
    (19, "Singapore",      "What's the weather in Singapore right now?",
                           "Equatorial heat, 32°C, sudden rain burst, carry an umbrella always"),
    (20, "Reykjavik",      "How's the weather in Reykjavik?",
                           "Near-freezing mist, 2°C, northern lights may be visible tonight, bundle up"),
]
""")

ALICE_COPILOT_TASK = (
    "You are 'weather-alice'. You will have a 20-turn weather conversation "
    "with 'weather-bob' through Pluto.\n\n"
    "## Protocol\n"
    "1. Register as 'weather-alice', set up message handling.\n"
    "2. Send a ready signal to 'weather-bob': "
    '{"type":"ready","agent":"weather-alice"}\n'
    "3. Wait for a ready message from 'weather-bob'.\n"
    "4. Loop through TURNS below.  On ODD turns (1,3,5,...,19) you are the "
    "ASKER: send the question, wait for the answer, then acquire lock "
    "'weather:transcript', append Q&A to transcript.txt, release lock.  "
    "On EVEN turns (2,4,6,...,20) you are the ANSWERER: wait for the "
    "question, send the answer from the TURNS table.\n"
    "5. Disconnect when all turns are done.\n\n"
    "## Turn Data\n```python\n" + _TURN_DATA + "```\n\n"
    "## Message formats\n"
    "Question: "
    '{"type":"weather_question","turn":<N>,"city":"<city>","text":"<question>"}\n'
    "Answer:   "
    '{"type":"weather_answer","turn":<N>,"city":"<city>","text":"<answer>"}\n\n'
    "## Transcript format (one entry per turn, appended by the ASKER)\n"
    "[Turn  N] weather-alice → weather-bob: <question>\\n"
    "[Turn  N] weather-bob → weather-alice: <answer_text>\\n\\n\n"
)

BOB_COPILOT_TASK = (
    "You are 'weather-bob'. You will have a 20-turn weather conversation "
    "with 'weather-alice' through Pluto.\n\n"
    "## Protocol\n"
    "1. Register as 'weather-bob', set up message handling.\n"
    "2. Send a ready signal to 'weather-alice': "
    '{"type":"ready","agent":"weather-bob"}\n'
    "3. Wait for a ready message from 'weather-alice'.\n"
    "4. Loop through TURNS below.  On EVEN turns (2,4,6,...,20) you are the "
    "ASKER: send the question, wait for the answer, then acquire lock "
    "'weather:transcript', append Q&A to transcript.txt, release lock.  "
    "On ODD turns (1,3,5,...,19) you are the ANSWERER: wait for the "
    "question, send the answer from the TURNS table.\n"
    "5. Disconnect when all turns are done.\n\n"
    "## Turn Data\n```python\n" + _TURN_DATA + "```\n\n"
    "## Message formats\n"
    "Question: "
    '{"type":"weather_question","turn":<N>,"city":"<city>","text":"<question>"}\n'
    "Answer:   "
    '{"type":"weather_answer","turn":<N>,"city":"<city>","text":"<answer>"}\n\n'
    "## Transcript format (one entry per turn, appended by the ASKER)\n"
    "[Turn  N] weather-bob → weather-alice: <question>\\n"
    "[Turn  N] weather-alice → weather-bob: <answer_text>\\n\\n\n"
)


# ── Direct-mode launcher ─────────────────────────────────────────────────────

def run_direct(host, port, work_dir):
    """Launch agents as plain Python subprocesses."""
    env = os.environ.copy()
    env["PYTHONPATH"] = _SRC_PY + ":" + env.get("PYTHONPATH", "")
    env["PLUTO_HOST"] = host
    env["PLUTO_PORT"] = str(port)
    env["WORK_DIR"] = work_dir

    procs = {}
    # Start bob first (he waits for alice's question on turn 1)
    print("[launcher] Starting weather-bob ...")
    procs["weather-bob"] = subprocess.Popen(
        [sys.executable, AGENT_SCRIPTS["weather-bob"]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )
    time.sleep(0.3)

    print("[launcher] Starting weather-alice ...")
    procs["weather-alice"] = subprocess.Popen(
        [sys.executable, AGENT_SCRIPTS["weather-alice"]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )

    outputs = {}
    for name, proc in procs.items():
        stdout, _ = proc.communicate(timeout=120)
        outputs[name] = {"stdout": stdout, "returncode": proc.returncode}

    return outputs


# ── Copilot-mode launcher ────────────────────────────────────────────────────

def run_copilot(host, port, work_dir):
    """Launch agents as real Copilot CLI processes via AgentWrapper."""
    from agent_wrapper import AgentWrapper

    wrapper = AgentWrapper(host=host, port=port)
    agents = [
        {"agent_id": "weather-bob",   "task": BOB_COPILOT_TASK,   "start_delay_s": 0},
        {"agent_id": "weather-alice", "task": ALICE_COPILOT_TASK, "start_delay_s": 3},
    ]
    results = wrapper.run_copilot_agents(agents, work_dir, timeout=300)
    outputs = {}
    for a in results["agents"]:
        outputs[a["agent_id"]] = {
            "stdout": a["stdout"],
            "returncode": a["returncode"],
        }
    return outputs


# ── Verification & reporting ─────────────────────────────────────────────────

def verify_and_report(outputs, host, port, work_dir, elapsed):
    """Check transcript, query stats, print full report."""
    from pluto_client import PlutoClient

    print("\n" + "=" * 72)
    print("  AGENT LOGS")
    print("=" * 72)
    for name in ["weather-alice", "weather-bob"]:
        info = outputs.get(name, {})
        print(f"\n{'─' * 34} {name} {'─' * 34}")
        print(info.get("stdout", "(no output)").rstrip())

    # ── Query Pluto stats ─────────────────────────────────────────────
    with PlutoClient(host=host, port=int(port), agent_id="reporter") as client:
        stats = client.stats()

    # ── Verify transcript ─────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  VERIFICATION")
    print("=" * 72)

    transcript_path = os.path.join(work_dir, "transcript.txt")
    passed = 0
    failed = 0

    # Check 1: transcript exists
    if os.path.exists(transcript_path):
        with open(transcript_path) as f:
            content = f.read()

        # Check 2: all 20 turns present
        for t in range(1, NUM_TURNS + 1):
            marker = f"[Turn {t:2d}]"
            if marker in content:
                passed += 1
            else:
                print(f"  FAIL  Turn {t} missing from transcript")
                failed += 1

        # Check 3: all 20 cities mentioned
        for city in CITIES:
            if city.lower() in content.lower():
                passed += 1
            else:
                print(f"  FAIL  City '{city}' missing from transcript")
                failed += 1

        # Check 4: both agent names present
        for name in ["weather-alice", "weather-bob"]:
            if name in content:
                passed += 1
            else:
                print(f"  FAIL  Agent '{name}' missing from transcript")
                failed += 1

        if failed == 0:
            print(f"  PASS  All {NUM_TURNS} turns present in transcript")
            print(f"  PASS  All {len(CITIES)} cities mentioned")
            print(f"  PASS  Both agent names present")
    else:
        print(f"  FAIL  transcript.txt not found at {transcript_path}")
        failed += 1

    # Check 5: both agents exited with rc=0
    agents_ok = all(o.get("returncode") == 0 for o in outputs.values())
    if agents_ok:
        print(f"  PASS  Both agents exited successfully")
        passed += 1
    else:
        for name, o in outputs.items():
            if o.get("returncode") != 0:
                print(f"  FAIL  {name} exited with rc={o['returncode']}")
        failed += 1

    total_checks = passed + failed

    # ── Print Pluto Statistics ────────────────────────────────────────
    counters = stats.get("counters", {})
    agent_stats = stats.get("agent_stats", {})
    live = stats.get("live", {})

    print("\n" + "=" * 72)
    print("  PLUTO SERVER STATISTICS")
    print("=" * 72)
    print(f"""
  Test duration:        {elapsed:.2f}s
  Server uptime:        {stats.get('uptime_ms', 0) / 1000:.1f}s

  ┌─────────────────────────────────────────────┐
  │  GLOBAL COUNTERS                            │
  ├─────────────────────────────────────────────┤
  │  Agents registered:     {counters.get('agents_registered', 0):>6}              │
  │  Locks acquired:        {counters.get('locks_acquired', 0):>6}              │
  │  Locks released:        {counters.get('locks_released', 0):>6}              │
  │  Lock waits (contention):{counters.get('lock_waits', 0):>5}              │
  │  Messages sent:         {counters.get('messages_sent', 0):>6}              │
  │  Messages received:     {counters.get('messages_received', 0):>6}              │
  │  Broadcasts sent:       {counters.get('broadcasts_sent', 0):>6}              │
  │  Total requests:        {counters.get('total_requests', 0):>6}              │
  │  Deadlocks detected:    {counters.get('deadlocks_detected', 0):>6}              │
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │  LIVE SNAPSHOT                              │
  ├─────────────────────────────────────────────┤
  │  Active locks:          {live.get('active_locks', 0):>6}              │
  │  Connected agents:      {live.get('connected_agents', 0):>6}              │
  │  Total agents seen:     {live.get('total_agents', 0):>6}              │
  │  Pending waiters:       {live.get('pending_waiters', 0):>6}              │
  └─────────────────────────────────────────────┘
""")

    print("  PER-AGENT BREAKDOWN:")
    print("  " + "─" * 70)
    header = f"  {'Agent':<20} {'Locks Acq':>10} {'Locks Rel':>10} {'Msgs Sent':>10} {'Msgs Recv':>10}"
    print(header)
    print("  " + "─" * 70)
    for aid in sorted(agent_stats.keys()):
        s = agent_stats[aid]
        print(f"  {aid:<20} {s.get('locks_acquired', 0):>10} {s.get('locks_released', 0):>10} "
              f"{s.get('messages_sent', 0):>10} {s.get('messages_received', 0):>10}")
    print("  " + "─" * 70)

    # ── Transcript ────────────────────────────────────────────────────
    if os.path.exists(transcript_path):
        print("\n" + "=" * 72)
        print("  FULL TRANSCRIPT")
        print("=" * 72)
        with open(transcript_path) as f:
            print(f.read())

    # ── Final verdict ─────────────────────────────────────────────────
    overall = (failed == 0) and agents_ok
    print(f"\n  Checks: {passed} passed, {failed} failed")
    print(f"  Agents: {'ALL OK' if agents_ok else 'SOME FAILED'}")
    print(f"\n  {'✓ TEST PASSED' if overall else '✗ TEST FAILED'}")
    print("=" * 72)

    # Dump full stats JSON
    print("\n  Full stats JSON:")
    print(textwrap.indent(json.dumps(stats, indent=2), "    "))

    return 0 if overall else 1


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Weather Chat Demo — 2 agents × 20 turns via Pluto"
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--direct", action="store_true", default=True,
        help="Launch agents as Python subprocesses (default)",
    )
    mode_group.add_argument(
        "--copilot", action="store_true",
        help="Launch agents as real Copilot CLI processes",
    )
    parser.add_argument("--host", default=PLUTO_HOST)
    parser.add_argument("--port", type=int, default=PLUTO_PORT)
    args = parser.parse_args()

    work_dir = WORK_DIR
    if os.path.exists(work_dir):
        for f in os.listdir(work_dir):
            os.remove(os.path.join(work_dir, f))
    os.makedirs(work_dir, exist_ok=True)

    print("=" * 72)
    print("  WEATHER CHAT DEMO — 2 Agents × 20 Turns")
    mode_label = "COPILOT CLI" if args.copilot else "DIRECT (subprocess)"
    print(f"  Mode: {mode_label}")
    print(f"  Server: {args.host}:{args.port}")
    print("=" * 72)
    print()

    start_time = time.time()

    if args.copilot:
        outputs = run_copilot(args.host, args.port, work_dir)
    else:
        outputs = run_direct(args.host, args.port, work_dir)

    elapsed = time.time() - start_time

    return verify_and_report(outputs, args.host, args.port, work_dir, elapsed)


if __name__ == "__main__":
    sys.exit(main())
