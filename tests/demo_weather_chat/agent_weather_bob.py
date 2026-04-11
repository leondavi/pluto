#!/usr/bin/env python3
"""
Agent: weather-bob
═══════════════════
20-turn weather conversation with weather-alice via Pluto server.
Bob answers on odd turns (1,3,5,...,19) and asks on even turns (2,4,6,...,20).
All coordination (messaging, locking the shared transcript) goes through Pluto.
"""

import os
import sys
import threading
import time

from pluto_client import PlutoClient

HOST = os.environ.get("PLUTO_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLUTO_PORT", "9000"))
WORK = os.environ.get("WORK_DIR", "/tmp/pluto_weather_chat")

PEER = "weather-alice"
MY_ID = "weather-bob"

# (turn, city, question, answer)
# answer = what the ANSWERER says on that turn
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

# ── threaded message handling ─────────────────────────────────────────────────
messages = []
msg_event = threading.Event()
_msg_lock = threading.Lock()


def on_msg(event):
    with _msg_lock:
        messages.append(event)
    msg_event.set()


def wait_msg(from_agent, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _msg_lock:
            for i, m in enumerate(messages):
                if m.get("from") == from_agent:
                    return messages.pop(i)
        msg_event.clear()
        msg_event.wait(timeout=1)
    raise TimeoutError(f"Timeout waiting for message from {from_agent}")


def log(msg):
    print(f"[{MY_ID}] {msg}", flush=True)


def main():
    client = PlutoClient(host=HOST, port=PORT, agent_id=MY_ID, timeout=15.0)
    client.on_message(on_msg)
    client.connect()
    log(f"Connected (session={client.session_id})")

    try:
        os.makedirs(WORK, exist_ok=True)

        # ── handshake ────────────────────────────────────────────────
        client.send(PEER, {"type": "ready", "agent": MY_ID})
        log("Sent ready signal, waiting for peer ...")
        wait_msg(PEER, timeout=30)
        log("Peer is ready — starting 20-turn weather chat\n")

        for turn_num, city, question, answer in TURNS:
            bob_is_asker = (turn_num % 2 == 0)

            if bob_is_asker:
                # ── Bob asks ─────────────────────────────────────────
                log(f"Turn {turn_num:2d}: Asking {PEER} about {city} ...")
                client.send(PEER, {
                    "type": "weather_question",
                    "turn": turn_num,
                    "city": city,
                    "text": question,
                })
                resp = wait_msg(PEER, timeout=30)
                answer_text = resp["payload"]["text"]
                log(f"Turn {turn_num:2d}: ← {answer_text[:70]}")

                # Log Q&A to shared transcript (locked)
                lock = client.acquire("weather:transcript", mode="write", ttl_ms=10000)
                with open(os.path.join(WORK, "transcript.txt"), "a") as f:
                    f.write(f"[Turn {turn_num:2d}] {MY_ID} → {PEER}: {question}\n")
                    f.write(f"[Turn {turn_num:2d}] {PEER} → {MY_ID}: {answer_text}\n\n")
                client.release(lock)
            else:
                # ── Bob answers ──────────────────────────────────────
                q_msg = wait_msg(PEER, timeout=30)
                q_text = q_msg["payload"]["text"]
                log(f"Turn {turn_num:2d}: Received question about {city}")
                client.send(PEER, {
                    "type": "weather_answer",
                    "turn": turn_num,
                    "city": city,
                    "text": answer,
                })
                log(f"Turn {turn_num:2d}: → Answered: {answer[:70]}")

        log("\nAll 20 turns complete!")

    finally:
        client.disconnect()
        log("Disconnected")


if __name__ == "__main__":
    main()
