"""
Canonical pytest suite for the `src.cache` toolkit.

This file is the SINGLE source of truth used by the demo harness to grade
EVERY setup (Haiku team, Haiku solo, Sonnet solo, Opus solo). The harness
copies it verbatim into each workspace's `tests/test_cache_toolkit.py`
immediately before invoking pytest, overwriting anything the agent may
have written there.

Goal: every setup is judged against IDENTICAL acceptance criteria, so the
"# tests passed" column is comparable across columns.

The cases mirror the bullet list in TASK_SPEC inside
test_haiku_vs_sonnet.py and only depend on the documented public surface
of `LruTtlCache`, `SieveCache`, and `src.cache.persist`.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

import pytest

from src.cache.lru_ttl import LruTtlCache  # noqa: E402
from src.cache.sieve import SieveCache  # noqa: E402
from src.cache import persist  # noqa: E402


def test_get_returns_none_on_miss():
    c = LruTtlCache(maxsize=4, ttl_seconds=10)
    assert c.get("missing") is None


def test_put_then_get_returns_value():
    c = LruTtlCache(maxsize=4, ttl_seconds=10)
    c.put("a", 1)
    assert c.get("a") == 1


def test_lru_eviction_when_full():
    c = LruTtlCache(maxsize=2, ttl_seconds=10)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)            # should evict 'a' (oldest)
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_get_refreshes_lru_position():
    c = LruTtlCache(maxsize=2, ttl_seconds=10)
    c.put("a", 1)
    c.put("b", 2)
    # Touch 'a' so it becomes most-recently-used; 'b' is now LRU.
    assert c.get("a") == 1
    c.put("c", 3)            # should evict 'b', not 'a'
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3


def test_ttl_expiry_returns_none():
    c = LruTtlCache(maxsize=4, ttl_seconds=0.05)
    c.put("a", 1)
    time.sleep(0.15)
    assert c.get("a") is None
    stats = c.stats()
    assert stats.get("expired", 0) >= 1


def test_stats_counts_hits_and_misses():
    c = LruTtlCache(maxsize=4, ttl_seconds=10)
    c.put("a", 1)
    c.get("a")               # hit
    c.get("a")               # hit
    c.get("missing")         # miss
    s = c.stats()
    assert s.get("hits", 0) == 2
    assert s.get("misses", 0) == 1


def test_thread_safety_does_not_corrupt():
    c = LruTtlCache(maxsize=16, ttl_seconds=10)
    errors: list[BaseException] = []

    def worker(seed: int) -> None:
        try:
            for i in range(200):
                k = f"k{(seed * 31 + i) % 32}"
                if i % 2 == 0:
                    c.put(k, i)
                else:
                    c.get(k)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"thread errors: {errors!r}"
    assert len(c) <= 16


def test_value_error_on_invalid_args():
    with pytest.raises(ValueError):
        LruTtlCache(maxsize=0, ttl_seconds=10)
    with pytest.raises(ValueError):
        LruTtlCache(maxsize=4, ttl_seconds=0)


# ── SieveCache ──────────────────────────────────────────────────────────────


def test_sieve_basic_put_get():
    c = SieveCache(maxsize=4)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1
    assert c.get("b") == 2
    assert c.get("missing") is None


def test_sieve_evicts_unvisited_first():
    # SIEVE keeps a "visited" bit per entry. On overflow, the hand walks
    # the queue clearing visited bits until it finds an unvisited entry,
    # which is evicted. Inserting d below should evict 'b' (the only
    # never-touched key in the queue at that moment), not 'a'.
    c = SieveCache(maxsize=3)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    c.get("a")   # mark 'a' visited
    c.get("c")   # mark 'c' visited
    c.put("d", 4)  # full -> hand finds 'b' (unvisited), evicts it
    assert c.get("b") is None, "expected SIEVE to evict the unvisited entry"
    assert c.get("a") == 1
    assert c.get("c") == 3
    assert c.get("d") == 4


def test_sieve_len_and_clear():
    c = SieveCache(maxsize=4)
    for k, v in [("a", 1), ("b", 2), ("c", 3)]:
        c.put(k, v)
    assert len(c) == 3
    c.clear()
    assert len(c) == 0
    assert c.get("a") is None


# ── persist ─────────────────────────────────────────────────────────────────


def test_persist_roundtrip_lru():
    c = LruTtlCache(maxsize=4, ttl_seconds=60)
    c.put("a", 1)
    c.put("b", "two")
    c.put("c", [1, 2, 3])
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "snap.json")
        persist.dump(c, path)
        assert os.path.isfile(path)
        restored = persist.load(path)
    assert isinstance(restored, LruTtlCache)
    assert restored.get("a") == 1
    assert restored.get("b") == "two"
    assert restored.get("c") == [1, 2, 3]


def test_persist_roundtrip_sieve():
    c = SieveCache(maxsize=8)
    c.put("x", 10)
    c.put("y", 20)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "snap.json")
        persist.dump(c, path)
        restored = persist.load(path)
    assert isinstance(restored, SieveCache)
    assert restored.get("x") == 10
    assert restored.get("y") == 20


def test_persist_load_missing_file_raises():
    with pytest.raises((FileNotFoundError, OSError)):
        persist.load("/tmp/_pluto_demo_does_not_exist_xyz.json")
