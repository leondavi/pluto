"""MessageFormatter — turn Pluto protocol messages into natural-language prompts."""

import json


class MessageFormatter:
    """
    Turn Pluto protocol messages (JSON dicts) into natural-language text
    that any LLM-based agent can understand and act on.
    """

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
                    f"[Pluto Message from {sender}]\n"
                    f"{json.dumps(payload, indent=2)}"
                )
            elif event == "broadcast":
                parts.append(
                    f"[Pluto Broadcast from {sender}]\n"
                    f"{json.dumps(payload, indent=2)}"
                )
            elif event == "task_assigned":
                task_id = msg.get("task_id", "?")
                desc = msg.get("description", "")
                parts.append(
                    f"[Pluto Task Assignment - {task_id}]\n"
                    f"From: {sender}\n"
                    f"Description: {desc}\n"
                    f"Payload: {json.dumps(payload, indent=2)}\n"
                    f"\nWork on this task. When done, update it with "
                    f'pluto_task_update("{task_id}", "completed", '
                    f'{{"result": ...}}).'
                )
            elif event == "topic_message":
                topic = msg.get("topic", "?")
                parts.append(
                    f"[Pluto Topic '{topic}' from {sender}]\n"
                    f"{json.dumps(payload, indent=2)}"
                )
            else:
                continue

        header = (
            "You have received the following Pluto coordination messages. "
            "Process them and take appropriate action.\n\n"
        )
        return header + "\n\n".join(parts)
