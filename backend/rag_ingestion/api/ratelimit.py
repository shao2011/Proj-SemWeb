"""In-process sliding-window rate limits.

One process holds every bucket, which is right for the single-worker
deployment and wrong for anything with more: two workers would each hand out
the full allowance. Moving to Redis is the fix if that ever changes.

The per-IP bucket reads the leftmost `X-Forwarded-For` entry. Behind a proxy
that is the visitor, but anyone who reaches the API directly writes that
header themselves and can claim any address they like. The per-IP bucket is
therefore best effort only; the global anonymous cap is what actually bounds
anonymous spend.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence

from fastapi import HTTPException, Request

from rag_ingestion.logger import get_logger

#: (key, limit, window seconds). The key is `kind` or `kind:identity`; only
#: the kind is ever logged.
Rule = tuple[str, int, float]

#: How often idle buckets are swept, in clock seconds. Touching a key prunes
#: it, but a key that is never touched again (one forged IP) would otherwise
#: sit in memory forever.
_SWEEP_INTERVAL_SECONDS = 60.0

_log = get_logger(__name__)


class _Bucket:
    __slots__ = ("window", "hits")

    def __init__(self, window: float) -> None:
        self.window = window
        self.hits: deque[float] = deque()

    def prune(self, now: float) -> None:
        cutoff = now - self.window
        while self.hits and self.hits[0] <= cutoff:
            self.hits.popleft()


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)

    def check(self, key: str, limit: int, window: float, *, cost: int = 1) -> float | None:
        return self.check_many([(key, limit, window)], cost=cost)

    def check_many(self, rules: Sequence[Rule], *, cost: int = 1) -> float | None:
        """Charge `cost` to every rule's bucket and return None, or charge
        nothing and return the seconds until every rule would have room."""
        refused = self.refusals(rules, cost=cost)
        if not refused:
            return None
        return max(wait for _rule, wait in refused)

    def refusals(
        self, rules: Sequence[Rule], *, cost: int = 1
    ) -> list[tuple[Rule, float]]:
        """The rules without room, each with its wait. Empty means every
        bucket was charged; otherwise none was."""
        with self._lock:
            now = self._clock()
            if now - self._last_sweep >= _SWEEP_INTERVAL_SECONDS:
                self._sweep(now)
            refused: list[tuple[Rule, float]] = []
            for rule in rules:
                key, limit, window = rule
                bucket = self._buckets.get(key)
                hits = 0
                if bucket is not None:
                    bucket.prune(now)
                    hits = len(bucket.hits)
                    if not hits:
                        del self._buckets[key]
                        bucket = None
                if hits + cost <= limit:
                    continue
                if cost > limit or bucket is None:
                    refused.append((rule, float(window)))
                    continue
                # The (hits + cost - limit)-th oldest hit has to leave first.
                oldest_needed = bucket.hits[hits + cost - limit - 1]
                refused.append((rule, oldest_needed + window - now))
            if refused:
                return refused
            for key, _limit, window in rules:
                bucket = self._buckets.setdefault(key, _Bucket(window))
                bucket.hits.extend([now] * cost)
            return []

    def _sweep(self, now: float) -> None:
        for key in list(self._buckets):
            bucket = self._buckets[key]
            bucket.prune(now)
            if not bucket.hits:
                del self._buckets[key]
        self._last_sweep = now


def enforce(limiter: RateLimiter, rules: Sequence[Rule], *, cost: int = 1) -> None:
    """429 `rate_limited` with whole-second `Retry-After` when any rule is full.

    A limit of 0 is switched off and never checked.
    """
    active = [rule for rule in rules if rule[1] > 0]
    if not active:
        return
    refused = limiter.refusals(active, cost=cost)
    if not refused:
        return
    for (key, limit, window), _wait in refused:
        _log.warning(
            "Rate limit tripped: %s (%d per %ds)",
            key.partition(":")[0],
            limit,
            window,
        )
    wait = max(wait for _rule, wait in refused)
    raise HTTPException(
        status_code=429,
        detail="rate_limited",
        headers={"Retry-After": str(max(1, math.ceil(wait)))},
    )


def ip_bucket(address: str) -> str:
    """IPv4 as is; IPv6 grouped by /64, which one subscriber usually holds
    whole, so per-address buckets would hand them 2^64 allowances."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 6:
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def client_ip(request: Request) -> str:
    """The leftmost `X-Forwarded-For` entry, else the socket peer, bucketed.

    Client-controlled when the API is reached directly; see the module note.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    first = forwarded.split(",")[0].strip()
    if not first:
        first = request.client.host if request.client is not None else "unknown"
    return ip_bucket(first)
