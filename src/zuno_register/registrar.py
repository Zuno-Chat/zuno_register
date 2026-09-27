"""The flow behind one accepted address: limits, one live code per address, the send.

Order matters: the per-address send counter is spent first and refunded by
every exit that sent nothing, so an outage never locks an address out and a
drained daily cap cannot be used to spend a stranger's record.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .code import new_code
from .config import Config
from .mail import Mailer, SendFailed
from .metrics import UPSTREAM_ERRORS, UPSTREAM_SECONDS
from .redis import Records, RedisUnavailable
from .synapse_private import TokenStore

_logger = logging.getLogger(__name__)

DAILY_WINDOW = 2 * 86400.0  # a day's key outlives its day by one, then goes
_MINT_ATTEMPTS = 2  # two collisions in a row is not a real outcome at this entropy


class DailyCapExceeded(Exception):
    pass


class Unavailable(Exception):
    """Redis, the store or Brevo failed; ``api`` is the metric label."""

    def __init__(self, api: str) -> None:
        super().__init__(api)
        self.api = api


class Registrar:
    def __init__(
        self,
        cfg: Config,
        store: TokenStore,
        records: Records,
        mailer: Mailer,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._records = records
        self._mailer = mailer
        self._clock = clock

    async def __call__(self, addr: str, key: str) -> str:
        """The metric result: ``ok`` (new code sent), ``resent`` or ``email_limited``."""
        sends_key = f"sends:{key}"
        try:
            sends = await self._records.count(sends_key, self._cfg.token_ttl)
            if sends > self._cfg.sends_per_address:
                await self._records.uncount(sends_key)
                return "email_limited"
        except RedisUnavailable:
            raise Unavailable("redis") from None

        counted = [sends_key]
        try:
            if self._cfg.daily_cap:
                daily_key = f"daily:{datetime.fromtimestamp(self._clock(), UTC):%Y-%m-%d}"
                if await self._records.count(daily_key, DAILY_WINDOW) > self._cfg.daily_cap:
                    await self._records.uncount(daily_key)
                    raise DailyCapExceeded()
                counted.append(daily_key)
            code, reused = await self._code(f"code:{key}")
            await self._mailer.send(addr, code, int(self._cfg.token_ttl // 3600))
        except Exception as e:
            # Twisted's CancelledError is an Exception, so a client that
            # disconnects mid-flight still gets its address refunded.
            await self._refund(counted)
            if isinstance(e, RedisUnavailable):
                raise Unavailable("redis") from e
            if isinstance(e, SendFailed):
                raise Unavailable("brevo") from e
            raise
        return "resent" if reused else "ok"

    async def _refund(self, keys: list[str]) -> None:
        for key in keys:
            try:
                await self._records.uncount(key)
            except RedisUnavailable:
                pass  # already counted and logged; the window ends on its own

    async def _code(self, code_key: str) -> tuple[str, bool]:
        existing = await self._records.get_code(code_key)
        if existing is not None and await self._store_call(
            "registration_token_is_valid", self._store.registration_token_is_valid(existing)
        ):
            return existing, True
        code = await self._mint()
        # A consumed code is replaced outright; only a fresh record is a race to win.
        held = await self._records.put_code(
            code_key, code, self._cfg.token_ttl, only_if_absent=existing is None
        )
        if held != code:
            await self._store_call(
                "delete_registration_token", self._store.delete_registration_token(code)
            )
            return held, True
        return code, False

    async def _mint(self) -> str:
        expiry_ms = int((self._clock() + self._cfg.token_ttl) * 1000)
        for _ in range(_MINT_ATTEMPTS):
            code = new_code()
            if await self._store_call(
                "create_registration_token",
                self._store.create_registration_token(code, 1, expiry_ms),
            ):
                return code
        UPSTREAM_ERRORS.labels("store", "collision").inc()
        raise Unavailable("store")

    async def _store_call[T](self, name: str, call: Awaitable[T]) -> T:
        started = time.monotonic()
        try:
            return await call
        except Exception as e:
            UPSTREAM_ERRORS.labels("store", "error").inc()
            # The type only: a database error's text can carry the token.
            _logger.warning("store %s failed: %s", name, type(e).__name__)
            raise Unavailable("store") from e
        finally:
            UPSTREAM_SECONDS.labels("store").observe(time.monotonic() - started)
