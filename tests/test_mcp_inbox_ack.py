"""Tests for the InboxManager safe-cursor ack model.

The ack cursor may only advance to just below the lowest undelivered
buffered seq (or to the highest seen seq once the buffer is empty), so
a server-side range ack can never destroy a message the agent has not
yet received. Ack failures must heal on later cycles, and dedupe /
telemetry state must be pruned below the acked cursor.
"""

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_PROJECT, "src_py"))
sys.path.insert(0, _THIS_DIR)  # sibling import of test_mcp_friend under pytest

try:
    from agent_mcp_friend.inbox import InboxManager
except ImportError as exc:
    raise unittest.SkipTest(
        f"agent_mcp_friend package not importable (mcp SDK not installed?): {exc}"
    )

from test_mcp_friend import FakeHttpClient


class FlakyAckClient(FakeHttpClient):
    """FakeHttpClient whose ack can be made to fail on demand."""

    def __init__(self):
        super().__init__()
        self.fail_acks = False

    def ack(self, up_to_seq: int) -> int:
        if self.fail_acks:
            raise RuntimeError("ack endpoint down")
        return super().ack(up_to_seq)


def _msg(seq: int, sender: str = "peer") -> dict:
    return {
        "event": "message", "from": sender,
        "payload": {"text": f"m{seq}"}, "seq_token": seq,
    }


def _noise(seq: int) -> dict:
    return {"event": "delivery_ack", "seq_token": seq}


class TestSafeCursorAck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlakyAckClient()
        self.inbox = InboxManager(self.client)

    async def test_noise_ack_never_exceeds_undelivered_buffered_seq(self):
        # Actionable seq 5 still buffered; noise seq 6 lands above it.
        # The old max(noise_seqs) ack would have range-deleted seq 5 on
        # the server. The cursor must stop at 4.
        await self.inbox._absorb([_msg(5), _noise(6)])
        self.assertEqual(self.client.acks, [4])
        # Draining seq 5 settles everything seen so far, including the
        # noise at 6.
        await self.inbox.drain()
        self.assertEqual(self.client.acks, [4, 6])

    async def test_noise_only_acks_to_max_seen(self):
        await self.inbox._absorb([_noise(3), _noise(4)])
        self.assertEqual(self.client.acks, [4])

    async def test_pop_acks_only_below_new_head(self):
        await self.inbox._absorb([_msg(10), _msg(11)])
        await self.inbox.pop_one()
        self.assertEqual(self.client.acks, [10])  # 11 still buffered
        await self.inbox.pop_one()
        self.assertEqual(self.client.acks, [10, 11])

    async def test_ack_failure_is_retried_and_heals(self):
        self.client.fail_acks = True
        await self.inbox._absorb([_msg(1)])
        msgs = await self.inbox.drain()  # delivery succeeds, ack fails
        self.assertEqual(len(msgs), 1)
        self.assertEqual(self.client.acks, [])
        self.assertEqual(self.inbox._last_acked_seq, 0)
        self.assertTrue(self.inbox._ack_retry_needed)
        # Server heals; the next drain-path ack covers the full cursor.
        self.client.fail_acks = False
        await self.inbox._absorb([_msg(2)])
        await self.inbox.drain()
        self.assertEqual(self.client.acks, [2])
        self.assertEqual(self.inbox._last_acked_seq, 2)
        self.assertFalse(self.inbox._ack_retry_needed)

    async def test_failed_ack_does_not_redeliver_in_process(self):
        # The message was already handed to the agent; a failed ack must
        # not cause the next peek to re-deliver it (dedupe holds until
        # the cursor actually advances past it).
        self.client.fail_acks = True
        await self.inbox._absorb([_msg(1)])
        await self.inbox.drain()
        await self.inbox._absorb([_msg(1)])  # server re-peeks unacked seq
        self.assertEqual(await self.inbox.peek_only(), [])

    async def test_seen_seqs_pruned_after_ack(self):
        await self.inbox._absorb([_msg(1), _msg(2)])
        self.assertEqual(self.inbox._seen_seqs, {1, 2})
        await self.inbox.drain()
        self.assertEqual(self.inbox._seen_seqs, set())

    async def test_landed_at_compacted_without_notifier(self):
        self.assertIsNone(self.inbox._notifier)
        await self.inbox._absorb([_msg(1), _msg(2)])
        self.assertEqual(set(self.inbox._landed_at), {1, 2})
        await self.inbox.drain()
        self.assertEqual(self.inbox._landed_at, {})

    async def test_cursor_never_regresses(self):
        await self.inbox._absorb([_msg(3)])
        await self.inbox.drain()
        self.assertEqual(self.client.acks, [3])
        # A later settle with nothing new must not re-ack.
        await self.inbox.drain()
        self.assertEqual(self.client.acks, [3])


if __name__ == "__main__":
    unittest.main()
