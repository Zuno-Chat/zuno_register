"""Request glue: route → content type → body → address → per-IP limit → registrar → 202.

Public and unauthenticated by nature: the caller has no account yet. Errors
are raised as SynapseError; DirectServeJsonResource renders them as Matrix
JSON and runs the handler under Synapse's logging-context rules. Neither the
address nor the code ever reaches a log line or an error body.
"""

from __future__ import annotations

import ipaddress
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from synapse.module_api import DirectServeJsonResource
from synapse.module_api.errors import Codes, SynapseError

from .email import InvalidEmail, normalize, record_key
from .metrics import REQUESTS
from .ratelimit import RateLimiter
from .registrar import DailyCapExceeded, Unavailable

MAX_REQUEST_BODY = 1024  # the body only ever carries one address

Registrar = Callable[[str, str], Awaitable[str]]


def ip_key(host: str | None) -> str:
    """The limiter key for a client address: IPv6 by its /64, so one host cannot roam a prefix."""
    if not host:
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if not isinstance(ip, ipaddress.IPv6Address):
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network((ip, 64), strict=False))


def _media_type(header: str | None) -> str:
    return (header or "").split(";", 1)[0].strip().lower()


class RegisterResource(DirectServeJsonResource):
    """A leaf at the exact path: anything below it, or any other verb, is the same 404."""

    isLeaf = True

    def __init__(
        self,
        limiter: RateLimiter,
        registrar: Registrar,
        *,
        record_secret: bytes,
        max_body: int = MAX_REQUEST_BODY,
    ) -> None:
        super().__init__()
        self._limiter = limiter
        self._registrar = registrar
        self._record_secret = record_secret
        self._max_body = max_body

    async def _async_render(self, request: Any) -> None:
        # Overrides the per-method dispatch so an unsupported verb is a 404
        # like an unmatched path, never a 405.
        await self._serve(request)

    async def _serve(self, request: Any) -> None:
        if request.method != b"POST" or request.postpath:
            raise SynapseError(404, "Unrecognized request", Codes.UNRECOGNIZED)

        # A browser sends cross-site JSON only after a CORS preflight, which
        # this route never answers; a "simple" type would skip it and let any
        # web page spend its visitors' per-IP allowance.
        if _media_type(request.getHeader("Content-Type")) != "application/json":
            self._fail("unsupported_media_type", 415, "Content type must be application/json")

        request.content.seek(0)
        body = request.content.read(self._max_body + 1)
        if len(body) > self._max_body:
            self._fail("too_large", 413, "Request body too large", Codes.TOO_LARGE)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"email"}:
                raise InvalidEmail()
            addr, canonical = normalize(payload["email"])
        except (ValueError, InvalidEmail):
            self._fail("invalid", 400, "Invalid email address", Codes.INVALID_PARAM)
        key = record_key(self._record_secret, canonical)

        wait_ms = self._limiter.allow(ip_key(getattr(request.getClientAddress(), "host", None)))
        if wait_ms is not None:
            self._fail(
                "ip_limited",
                429,
                "Too many requests",
                Codes.LIMIT_EXCEEDED,
                additional_fields={"retry_after_ms": wait_ms},
                headers={"Retry-After": str(math.ceil(wait_ms / 1000))},
            )

        try:
            result = await self._registrar(addr, key)
        except DailyCapExceeded:
            self._fail("global_limited", 429, "Too many requests", Codes.LIMIT_EXCEEDED)
        except Unavailable as e:
            self._fail(f"{e.api}_error", 502, "Registration service unavailable")

        REQUESTS.labels(result).inc()
        if request._disconnected:
            return
        request.setResponseCode(202)
        request.setHeader(b"Content-Type", b"application/json")
        request.setHeader(b"Cache-Control", b"no-store")
        request.setHeader(b"Content-Length", b"2")
        request.write(b"{}")
        request.finish()

    def _fail(
        self, result: str, status: int, msg: str, errcode: str = Codes.UNKNOWN, **kw: Any
    ) -> NoReturn:
        REQUESTS.labels(result).inc()
        raise SynapseError(status, msg, errcode, **kw)
