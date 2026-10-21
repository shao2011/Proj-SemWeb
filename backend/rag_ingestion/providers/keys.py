"""Multiple API keys per provider: parsing, round-robin, and sticky selection.

Two different call patterns need two different strategies. A stateless call
(embedding, reranking, OCR) has nothing to lose by moving to a different key
on every call, so `KeyRotator` just cycles. An LLM call inside a growing
conversation does: switching the account mid-thread throws away whatever
provider-side prompt cache had built up on the account used for earlier
turns, so `StickyKeyPool` pins one conversation to one key instead.
"""

from __future__ import annotations

import hashlib
import threading

from rag_ingestion.config import parse_keys

#: `parse_keys` turns one environment string into a list, so it belongs with
#: the rest of the configuration parsing and lives in `config` now. It is
#: re-exported here because that is where callers have always found it, and
#: because `config` must not import this package: doing so would run
#: `providers/__init__`, which reaches `chunking` and back into `config`.
__all__ = ["KeyRotator", "StickyKeyPool", "parse_keys"]


class KeyRotator:
    """Round-robin over a fixed key list. Thread-safe: a lock guards the index
    so concurrent callers (e.g. the upload executor's worker threads) never
    read a half-advanced position."""

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("At least one API key is required")
        self._keys = list(keys)
        self._index = 0
        self._lock = threading.Lock()

    def next_key(self) -> str:
        with self._lock:
            key = self._keys[self._index % len(self._keys)]
            self._index += 1
            return key


class StickyKeyPool:
    """Deterministic key selection by a sticky identifier (e.g. `thread_id`).

    The same sticky value always resolves to the same key, so one
    conversation's calls stay on one account. Different sticky values spread
    across the pool. `sticky=None` (no natural correlation id, e.g. a
    stateless one-shot route) falls back to round-robin.
    """

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("At least one API key is required")
        self._keys = list(keys)
        self._fallback = KeyRotator(keys)

    def key_for(self, sticky: str | None) -> str:
        if sticky is None:
            return self._fallback.next_key()
        # Python's built-in `hash()` is randomized per process for strings,
        # so the same thread could map to a different key after a restart.
        # A stable digest keeps the mapping fixed across the process lifetime.
        digest = hashlib.sha256(sticky.encode()).digest()
        index = int.from_bytes(digest[:8], "big") % len(self._keys)
        return self._keys[index]
