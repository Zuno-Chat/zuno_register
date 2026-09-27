"""Per-address records in Redis, on Synapse's own connection.

Counters are one Lua call so the window is set atomically with the first
count; a crash between INCR and EXPIRE could otherwise leave a key that never
expires. Every command is bounded by a timeout and any failure is a
RedisUnavailable, which the resource answers as 502.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from synapse.module_api import make_deferred_yieldable
from twisted.internet.defer import CancelledError, Deferred
from twisted.internet.defer import TimeoutError as DeferredTimeoutError

from .metrics import UPSTREAM_ERRORS, UPSTREAM_SECONDS

_logger = logging.getLogger(__name__)

# Redis EVAL runs this fixed Lua script server-side; nothing from a request
# reaches it except the key name and the window length.
_COUNT = (
    "local n = redis.call('INCR', KEYS[1]) "
    "if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end "
    "return n"
)


class RedisUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Records:
    def __init__(
        self, connection: Any, prefix: str, timeout: float, *, reactor: Any = None
    ) -> None:
        if reactor is None:
            from twisted.internet import reactor as global_reactor

            reactor = global_reactor
        self._conn = connection
        self._prefix = prefix
        self._timeout = timeout
        self._reactor = reactor

    async def count(self, key: str, window: float) -> int:
        """Increment ``key`` and return the new count; the first count starts the window."""
        n = await self._run(lambda: self._conn.eval(_COUNT, [self._prefix + key], [int(window)]))
        return int(n)

    async def uncount(self, key: str) -> None:
        """Give a count back; at zero the key goes, so a window never survives on a refund."""
        n = await self._run(lambda: self._conn.decr(self._prefix + key))
        if int(n) <= 0:
            await self._run(lambda: self._conn.delete(self._prefix + key))

    async def get_code(self, key: str) -> str | None:
        value = await self._run(lambda: self._conn.get(self._prefix + key))
        # txredisapi converts an all-digit reply to a number; a code has no
        # leading zero (0 is not in the alphabet), so str() round-trips it.
        return None if value is None else str(value)

    async def put_code(self, key: str, code: str, ttl: float, *, only_if_absent: bool) -> str:
        """Store ``code`` for ``ttl``; returns the code now held (a rival's, on a lost race)."""
        ok = await self._run(
            lambda: self._conn.set(
                self._prefix + key, code, expire=int(ttl), only_if_not_exists=only_if_absent
            )
        )
        if ok:
            return code
        held = await self.get_code(key)
        return held if held is not None else code

    async def _run(self, command: Callable[[], Deferred[Any]]) -> Any:
        # The command is issued inside the try: a handler with no live
        # connection raises before it returns a Deferred.
        started = time.monotonic()
        try:
            d = command()
            d.addTimeout(self._timeout, self._reactor)
            return await make_deferred_yieldable(d)
        except Exception as e:
            reason = "timeout" if isinstance(e, (DeferredTimeoutError, CancelledError)) else "error"
            UPSTREAM_ERRORS.labels("redis", reason).inc()
            _logger.warning("redis unavailable: reason=%s error=%s", reason, type(e).__name__)
            raise RedisUnavailable(reason) from e
        finally:
            UPSTREAM_SECONDS.labels("redis").observe(time.monotonic() - started)
