# Weather Chat Demo — 2-Agent Conversational Coordination

## Overview

Two **real Copilot CLI agents** (**weather-alice** and **weather-bob**) hold a **20-turn weather conversation** about cities around the world. Each agent is a separate `copilot` process (or standalone Python subprocess) that connects to the real Pluto Erlang server over TCP.

Every exchange — questions, answers, and transcript writes — goes exclusively through Pluto's messaging and locking primitives. No shared memory, no files used for signalling.

| Property | Value |
|---|---|
| Agents | 2 (weather-alice, weather-bob) |
| Turns | 20 (alternating asker/answerer) |
| Agent runtime | Real Copilot CLI (`copilot -p ...`) or direct Python subprocess |
| Coordination | Pluto TCP messaging + write locks on shared transcript |
| Total messages | 42 (40 Q&A + 2 handshake) |
| Total lock cycles | 20 (1 per turn — asker logs to transcript) |
| Deadlocks | 0 |
| Duplicates | 0 |

---

## Architecture

```
┌──────────────────┐
│   run_weather_    │─────────────────────────────────────────────┐
│   chat.py         │                                             │
│   (launcher)      │                                             │
└────────┬─────────┘                                             │
         │  spawns 2 processes                                    │
         │                                                        │
    ┌────▼──────────────┐    ┌─────────────────────┐              │
    │  weather-alice     │    │  weather-bob         │              │
    │  (copilot -p ...)  │    │  (copilot -p ...)    │              │
    │                    │    │                      │              │
    │ Asks on odd turns  │    │ Asks on even turns   │              │
    │ Answers on even    │    │ Answers on odd       │              │
    └───────┬────────────┘    └───────┬──────────────┘              │
            │  TCP :9000              │  TCP :9000                  │
            │                         │                             │
            ▼                         ▼                             │
    ┌──────────────────────────────────────────┐                    │
    │           Pluto Server (Erlang)           │                    │
    │                                          │                    │
    │  Messages: send / wait_msg               │                    │
    │  Locks:    weather:transcript (write)     │◄───── stats query ─┘
    │  Stats:    per-agent counters             │
    └──────────────────────────────────────────┘
```

---

## Cities Visited (20 Turns)

| Turn | Asker | City | Temperature |
|------|-------|------|-------------|
| 1 | weather-alice | Tokyo | 22°C |
| 2 | weather-bob | Paris | 18°C |
| 3 | weather-alice | New York | 14°C |
| 4 | weather-bob | Sydney | 27°C |
| 5 | weather-alice | London | 11°C |
| 6 | weather-bob | Cairo | 38°C |
| 7 | weather-alice | Rio de Janeiro | 30°C |
| 8 | weather-bob | Moscow | -5°C |
| 9 | weather-alice | Mumbai | 29°C |
| 10 | weather-bob | Beijing | 20°C |
| 11 | weather-alice | Toronto | 8°C |
| 12 | weather-bob | Berlin | 12°C |
| 13 | weather-alice | Dubai | 42°C |
| 14 | weather-bob | Seoul | 16°C |
| 15 | weather-alice | Rome | 24°C |
| 16 | weather-bob | Bangkok | 35°C |
| 17 | weather-alice | Cape Town | 17°C |
| 18 | weather-bob | Buenos Aires | 19°C |
| 19 | weather-alice | Singapore | 32°C |
| 20 | weather-bob | Reykjavik | 2°C |

---

## Per-Turn Message Flow

Each of the 20 turns follows this exact protocol:

```
TURN t (asker = alice if odd, bob if even):

  1. Asker  ──► Pluto ──► Answerer:   {"type":"weather_question", "turn":t, "city":"...", "text":"..."}
  2. Answerer ──► Pluto ──► Asker:     {"type":"weather_answer",   "turn":t, "city":"...", "text":"..."}
  3. Asker  ──► Pluto: acquire("weather:transcript", mode="write")
  4. Asker  ──► filesystem: append Q&A to transcript.txt
  5. Asker  ──► Pluto: release(lock_ref)
```

Additionally, before turn 1 both agents exchange a `{"type":"ready"}` handshake message to synchronise startup.

---

## Resource Locking Pattern

```
weather:transcript (write lock):
  Acquired once per turn by the asker after receiving the answer.
  Protects the shared transcript.txt from concurrent writes.

  Turn  1 — weather-alice acquires, writes, releases
  Turn  2 — weather-bob   acquires, writes, releases
  Turn  3 — weather-alice acquires, writes, releases
  ...
  Turn 20 — weather-bob   acquires, writes, releases

No contention expected (only one agent writes per turn),
but the lock guarantees correctness if timing drifts.
```

---

## Test Results

```
========================================================================
  WEATHER CHAT DEMO — 2 Agents × 20 Turns
  Mode: DIRECT (subprocess)
  Server: 127.0.0.1:9000
========================================================================

  VERIFICATION
  ─────────────────────────────────────────────────────
  PASS  All 20 turns present in transcript
  PASS  All 20 cities mentioned
  PASS  Both agent names present
  PASS  Both agents exited successfully

  Checks: 43 passed, 0 failed
  Agents: ALL OK

  ✓ TEST PASSED
```

### Pluto Server Statistics

```
  Test duration:        1.38s

  ┌─────────────────────────────────────────────┐
  │  GLOBAL COUNTERS                            │
  ├─────────────────────────────────────────────┤
  │  Agents registered:          3              │
  │  Locks acquired:            20              │
  │  Locks released:            20              │
  │  Lock waits (contention):    0              │
  │  Messages sent:             42              │
  │  Messages received:         42              │
  │  Broadcasts sent:            0              │
  │  Total requests:            87              │
  │  Deadlocks detected:         0              │
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │  LIVE SNAPSHOT (post-test)                  │
  ├─────────────────────────────────────────────┤
  │  Active locks:               0              │
  │  Connected agents:           1              │
  │  Total agents seen:          3              │
  │  Pending waiters:            0              │
  └─────────────────────────────────────────────┘
```

### Per-Agent Breakdown

```
  Agent                 Locks Acq  Locks Rel  Msgs Sent  Msgs Recv
  ──────────────────────────────────────────────────────────────────
  weather-alice                10         10         21         21
  weather-bob                  10         10         21         21
  ──────────────────────────────────────────────────────────────────
```

Each agent:
- Acquired **10 locks** (one per turn as asker, 10 turns each)
- Sent **21 messages** (1 ready + 10 questions + 10 answers)
- Received **21 messages** (1 ready + 10 questions + 10 answers)
- All locks released — zero leaks

### Full Stats JSON

```json
{
  "agent_stats": {
    "weather-alice": {
      "disconnections": 1,
      "locks_acquired": 10,
      "locks_released": 10,
      "messages_received": 21,
      "messages_sent": 21,
      "registrations": 1
    },
    "weather-bob": {
      "disconnections": 1,
      "locks_acquired": 10,
      "locks_released": 10,
      "messages_received": 21,
      "messages_sent": 21,
      "registrations": 1
    }
  },
  "counters": {
    "agents_registered": 3,
    "broadcasts_sent": 0,
    "deadlocks_detected": 0,
    "lock_waits": 0,
    "locks_acquired": 20,
    "locks_released": 20,
    "messages_received": 42,
    "messages_sent": 42,
    "total_requests": 87
  },
  "live": {
    "active_locks": 0,
    "connected_agents": 1,
    "pending_waiters": 0,
    "total_agents": 3
  },
  "status": "ok"
}
```

---

## Sample Transcript (All 20 Turns)

```
[Turn  1] weather-alice → weather-bob: What's the weather like in Tokyo right now?
[Turn  1] weather-bob → weather-alice: Partly cloudy, 22°C, light breeze from the east, cherry blossoms in bloom

[Turn  2] weather-bob → weather-alice: How's the weather in Paris today?
[Turn  2] weather-alice → weather-bob: Clear skies, 18°C, gentle southwest wind, perfect for a Seine-side stroll

[Turn  3] weather-alice → weather-bob: What are conditions like in New York?
[Turn  3] weather-bob → weather-alice: Overcast with light rain, 14°C, gusty winds, grab an umbrella for Central Park

[Turn  4] weather-bob → weather-alice: How's the weather in Sydney?
[Turn  4] weather-alice → weather-bob: Sunny and warm, 27°C, refreshing sea breeze off Bondi Beach

[Turn  5] weather-alice → weather-bob: What's London weather like this morning?
[Turn  5] weather-bob → weather-alice: Foggy morning, 11°C, calm winds, classic London pea-souper clearing by noon

[Turn  6] weather-bob → weather-alice: What's the temperature in Cairo?
[Turn  6] weather-alice → weather-bob: Hot and dry, 38°C, dusty haze over the pyramids, stay hydrated

[Turn  7] weather-alice → weather-bob: How's the weather in Rio de Janeiro?
[Turn  7] weather-bob → weather-alice: Tropical showers, 30°C, high humidity, expect sunshine between rain bursts

[Turn  8] weather-bob → weather-alice: What are conditions in Moscow today?
[Turn  8] weather-alice → weather-bob: Light snow, -5°C, crisp north wind, the Kremlin looks magical in white

[Turn  9] weather-alice → weather-bob: What's the forecast for Mumbai?
[Turn  9] weather-bob → weather-alice: Monsoon rain, 29°C, heavy downpour, roads may be waterlogged

[Turn 10] weather-bob → weather-alice: How's the weather in Beijing?
[Turn 10] weather-alice → weather-bob: Hazy, 20°C, light wind, air quality moderate, masks advisable

[Turn 11] weather-alice → weather-bob: What's the weather like in Toronto?
[Turn 11] weather-bob → weather-alice: Crisp autumn day, 8°C, clear skies, perfect for a walk along the lakeshore

[Turn 12] weather-bob → weather-alice: How's the weather in Berlin?
[Turn 12] weather-alice → weather-bob: Steady drizzle, 12°C, overcast skies, a cozy day for museum hopping

[Turn 13] weather-alice → weather-bob: What's the temperature in Dubai today?
[Turn 13] weather-bob → weather-alice: Scorching sun, 42°C, dry desert heat, stay indoors during peak hours

[Turn 14] weather-bob → weather-alice: How's the weather in Seoul?
[Turn 14] weather-alice → weather-bob: Cherry blossom season, 16°C, mild breeze, Yeouido Park is stunning

[Turn 15] weather-alice → weather-bob: What's the weather like in Rome?
[Turn 15] weather-bob → weather-alice: Mediterranean warmth, 24°C, sunny skies, gelato weather by the Colosseum

[Turn 16] weather-bob → weather-alice: How's the weather in Bangkok today?
[Turn 16] weather-alice → weather-bob: Steamy heat, 35°C, afternoon thunderstorm building, seek shelter by 3 PM

[Turn 17] weather-alice → weather-bob: What are conditions like in Cape Town?
[Turn 17] weather-bob → weather-alice: Windy and cool, 17°C, partly cloudy, Table Mountain hidden by cloud cloth

[Turn 18] weather-bob → weather-alice: How's the weather in Buenos Aires?
[Turn 18] weather-alice → weather-bob: Pleasant autumn, 19°C, light clouds, great day for tango in San Telmo

[Turn 19] weather-alice → weather-bob: What's the weather in Singapore right now?
[Turn 19] weather-bob → weather-alice: Equatorial heat, 32°C, sudden rain burst, carry an umbrella always

[Turn 20] weather-bob → weather-alice: How's the weather in Reykjavik?
[Turn 20] weather-alice → weather-bob: Near-freezing mist, 2°C, northern lights may be visible tonight, bundle up
```

---

## How to Run

### Prerequisites
- Pluto server running (Erlang/OTP)
- Python 3.10+
- Copilot CLI on `PATH` (for `--copilot` mode; installed via VS Code Copilot extension)

### Start the server
```bash
./PlutoServer.sh --daemon
```

### Run with direct subprocesses (default)
```bash
python tests/demo_weather_chat/run_weather_chat.py
```

### Run with real Copilot CLI agents
```bash
python tests/demo_weather_chat/run_weather_chat.py --copilot
```

### Custom host / port
```bash
python tests/demo_weather_chat/run_weather_chat.py --host 10.0.0.5 --port 4000
```

---

## Key Takeaways

1. **Turn-based coordination is natural with Pluto messaging.** The `send` / `wait_msg` pattern creates clean synchronisation between agents without polling or shared state.

2. **Write locks on the transcript prevent data corruption.** Even though only one agent writes per turn, the lock guarantees correctness if timing drifts — no torn writes or interleaved lines.

3. **Zero deadlocks, zero contention.** The alternating-turn design means only one agent ever needs the transcript lock at a time. Pluto's FIFO queue would handle contention gracefully if it occurred.

4. **42 messages in 1.38 seconds.** Pluto adds negligible latency (~0.5 ms per TCP round-trip on localhost). The entire 20-turn conversation completes in under 2 seconds.

5. **Symmetric agent design.** Both agents run identical logic with mirrored roles. The only difference is which turns they ask vs. answer. This makes the pattern easy to extend to N agents or more turns.
