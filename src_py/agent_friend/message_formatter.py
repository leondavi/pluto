"""MessageFormatter — turn Pluto protocol messages into natural-language prompts."""

import json


class MessageFormatter:
    """
    Turn Pluto protocol messages (JSON dicts) into natural-language text
    that any LLM-based agent can understand and act on.
    """

    # Compact JSON separators — save tokens by skipping whitespace around : and ,
    _JSON_SEPS = (",", ":")

    @staticmethod
    def _j(payload) -> str:
        return json.dumps(payload, separators=MessageFormatter._JSON_SEPS)

    @staticmethod
    def format(messages: list[dict]) -> str:
        """Format one or more Pluto messages into a single injection string."""
        parts: list[str] = []

        for msg in messages:
            event = msg.get("event", "message")
            sender = msg.get("from", "unknown")
            payload = msg.get("payload", {})

            if event == "message":
                if isinstance(payload, dict) and payload.get("event") in (
                    "delivery_ack", "status_update", "heartbeat",
                ):
                    continue
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
