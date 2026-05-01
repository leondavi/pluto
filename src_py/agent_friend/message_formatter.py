"""MessageFormatter — turn Pluto protocol messages into agent-readable text.

Two output formats are supported:

* ``natural``       — human-readable text with ``[Pluto ...]`` headers, designed
                      for any unmodified LLM-based agent CLI.
* ``deterministic`` — marker-bracketed frames, one per message, suitable for
                      programmatic parsing by an agent that knows the
                      protocol::

                          <S<PLUTO seq=42>>
                          {"event":"task_assigned","seq_token":42,...}
                          <E<PLUTO seq=42>>

  The ``seq`` value inside the markers is the same as ``seq_token`` from
  ``/agents/peek``, so the agent can ack idempotently via
  ``POST /agents/ack {"up_to_seq": N}``.  Note that PlutoAgentFriend
  flattens newlines when injecting, so parsers must tolerate frames that
  arrive as inline text on a single line — see
  ``library/protocol.md`` §7 for the regex.
"""

import json


# Filtered out of every output mode — these are infrastructure noise
# the agent should never see.
_NOISE_PAYLOAD_EVENTS = {"delivery_ack", "status_update", "heartbeat"}

INJECT_FORMAT_NATURAL = "natural"
INJECT_FORMAT_DETERMINISTIC = "deterministic"
INJECT_FORMATS = (INJECT_FORMAT_NATURAL, INJECT_FORMAT_DETERMINISTIC)


class MessageFormatter:
    """
    Turn Pluto protocol messages (JSON dicts) into text the wrapper can
    inject into an agent's stdin, in either natural-language or
    deterministic marker-bracketed form.
    """

    # Compact JSON separators — save tokens by skipping whitespace around : and ,
    _JSON_SEPS = (",", ":")

    @staticmethod
    def _j(payload) -> str:
        return json.dumps(payload, separators=MessageFormatter._JSON_SEPS)

    @staticmethod
    def _is_noise(msg: dict) -> bool:
        payload = msg.get("payload")
        if isinstance(payload, dict) and payload.get("event") in _NOISE_PAYLOAD_EVENTS:
            return True
        if msg.get("event") in _NOISE_PAYLOAD_EVENTS:
            return True
        return False

    @staticmethod
    def format(messages: list[dict], mode: str = INJECT_FORMAT_NATURAL) -> str:
        """Format messages for injection. ``mode`` selects the wire format."""
        if mode == INJECT_FORMAT_DETERMINISTIC:
            return MessageFormatter.format_deterministic(messages)
        return MessageFormatter.format_natural(messages)

    @staticmethod
    def format_natural(messages: list[dict]) -> str:
        """Format messages as natural-language text with ``[Pluto ...]`` headers."""
        parts: list[str] = []

        for msg in messages:
            if MessageFormatter._is_noise(msg):
                continue

            event = msg.get("event", "message")
            sender = msg.get("from", "unknown")
            payload = msg.get("payload", {})

            if event == "message":
                parts.append(
                    f"[Pluto msg from {sender}]\n"
                    f"{MessageFormatter._j(payload)}"
                )
            elif event == "broadcast":
                parts.append(
                    f"[Pluto bcast from {sender}]\n"
                    f"{MessageFormatter._j(payload)}"
                )
            elif event == "task_assigned":
                task_id = msg.get("task_id", "?")
                desc = msg.get("description", "")
                parts.append(
                    f"[Pluto task {task_id}]\n"
                    f"From: {sender}\n"
                    f"Desc: {desc}\n"
                    f"Payload: {MessageFormatter._j(payload)}\n"
                    f"\nWhen done, update with "
                    f'pluto_task_update("{task_id}","completed",'
                    f'{{"result":...}}).'
                )
            elif event == "topic_message":
                topic = msg.get("topic", "?")
                parts.append(
                    f"[Pluto topic '{topic}' from {sender}]\n"
                    f"{MessageFormatter._j(payload)}"
                )
            else:
                continue

        header = "Pluto coordination msgs, process each:\n\n"
        return header + "\n\n".join(parts)

    @staticmethod
    def format_deterministic(messages: list[dict]) -> str:
        """Format messages as marker-bracketed frames for deterministic parsing.

        Each non-noise message with a ``seq_token`` produces one frame::

            <S<PLUTO seq=N>>
            {compact JSON of the full server message dict}
            <E<PLUTO seq=N>>

        Messages without a ``seq_token`` are skipped (the marker contract
        requires one).  The agent is expected to extract the JSON body
        between the markers; ack is the wrapper's responsibility but the
        agent MAY ack via ``POST /agents/ack {"up_to_seq": N}``.
        """
        frames: list[str] = []

        for msg in messages:
            if MessageFormatter._is_noise(msg):
                continue
            seq = msg.get("seq_token")
            if seq is None:
                continue
            try:
                seq_int = int(seq)
            except (TypeError, ValueError):
                continue

            body = MessageFormatter._j(msg)
            frames.append(
                f"<S<PLUTO seq={seq_int}>>\n{body}\n<E<PLUTO seq={seq_int}>>"
            )

        if not frames:
            return ""
        return "\n".join(frames)
