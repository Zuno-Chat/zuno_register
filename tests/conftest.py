from __future__ import annotations

import time
from dataclasses import dataclass, field
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

from synapse.module_api import ModuleApi
from twisted.internet.defer import Deferred, fail, succeed

from zuno_register.config import BrevoConfig, Config
from zuno_register.mail import Mailer, SendFailed
from zuno_register.redis import _COUNT, Records
from zuno_register.synapse_private import TokenStore
from zuno_register.upstream import NO_RETRY, RetryPolicy, Upstream, UpstreamResponse

BREVO = BrevoConfig(api_key="k", sender_email="noreply@zuno.chat")


def config(**kw: Any) -> Config:
    return Config(brevo=BREVO, **kw)


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeConnection:
    """The txredisapi slice Records uses, over a dict with expiry, driven by a clock.

    `fail` makes every command fail with that exception; `hang` makes it
    never answer (for the timeout path); `before_set` runs just before a SET,
    to stage a lost race.
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.data: dict[str, tuple[Any, float | None]] = {}
        self.fail: Exception | None = None
        self.hang = False
        self.before_set: Any = None
        self.commands: list[str] = []

    def _answer(self, name: str, value: Any) -> Deferred[Any]:
        self.commands.append(name)
        if self.fail is not None:
            return fail(self.fail)
        if self.hang:
            return Deferred()
        return succeed(value)

    def _live(self, key: str) -> Any:
        entry = self.data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and expires <= self.clock():
            del self.data[key]
            return None
        return value

    def ttl(self, key: str) -> float | None:
        entry = self.data.get(key)
        return None if entry is None or entry[1] is None else entry[1] - self.clock()

    def eval(self, script: str, keys: list[str], args: list[Any]) -> Deferred[Any]:
        assert script == _COUNT
        (key,), (window,) = keys, args
        n = int(self._live(key) or 0) + 1
        expires = self.data[key][1] if n > 1 else self.clock() + int(window)
        self.data[key] = (n, expires)
        return self._answer("eval", n)

    def decr(self, key: str, amount: int = 1) -> Deferred[Any]:
        n = int(self._live(key) or 0) - amount
        expires = self.data[key][1] if key in self.data else None
        self.data[key] = (n, expires)
        return self._answer("decr", n)

    def get(self, key: str) -> Deferred[Any]:
        value = self._live(key)
        # txredisapi's convertNumbers turns an all-digit reply into an int.
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        return self._answer("get", value)

    def set(
        self,
        key: str,
        value: Any,
        expire: int | None = None,
        pexpire: int | None = None,
        only_if_not_exists: bool = False,
        only_if_exists: bool = False,
    ) -> Deferred[Any]:
        if self.before_set is not None:
            self.before_set()
        if only_if_not_exists and self._live(key) is not None:
            return self._answer("set", None)
        self.data[key] = (value, None if expire is None else self.clock() + expire)
        return self._answer("set", "OK")

    def delete(self, key: str, *more: str) -> Deferred[Any]:
        n = 0
        for k in (key, *more):
            if self.data.pop(k, None) is not None:
                n += 1
        return self._answer("delete", n)


def records(conn: FakeConnection, prefix: str = "t:", timeout: float = 5.0) -> Records:
    return Records(conn, prefix, timeout)


@dataclass
class Token:
    uses_allowed: int | None
    expiry_ms: int | None
    pending: int = 0
    completed: int = 0


class FakeStore:
    """The registration-token slice of Synapse's store. `fail` makes every call raise."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.tokens: dict[str, Token] = {}
        self.fail: Exception | None = None
        self.clock = clock or FakeClock(time.time())

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def create_registration_token(
        self, token: str, uses_allowed: int | None, expiry_time: int | None
    ) -> bool:
        self._check()
        if token in self.tokens:
            return False
        self.tokens[token] = Token(uses_allowed, expiry_time)
        return True

    async def registration_token_is_valid(self, token: str) -> bool:
        self._check()
        t = self.tokens.get(token)
        if t is None:
            return False
        if t.expiry_ms and t.expiry_ms < int(self.clock() * 1000):
            return False
        return not (t.uses_allowed and t.pending + t.completed >= t.uses_allowed)

    async def delete_registration_token(self, token: str) -> bool:
        self._check()
        return self.tokens.pop(token, None) is not None

    def consume(self, token: str) -> None:
        self.tokens[token].completed += 1


def as_store(fake: FakeStore) -> TokenStore:
    return cast(TokenStore, fake)


@dataclass
class FakeMailer:
    sent: list[tuple[str, str, int]] = field(default_factory=list)
    fail: bool = False

    async def send(self, to: str, code: str, hours: int) -> None:
        if self.fail:
            raise SendFailed("status 500")
        self.sent.append((to, code, hours))


def as_mailer(fake: FakeMailer) -> Mailer:
    return cast(Mailer, fake)


DEFAULT_REPLY = UpstreamResponse(201, "application/json", b'{"messageId":"m"}')


@dataclass
class FakeUpstream:
    """Records what a caller sent and answers from `replies`, DEFAULT_REPLY once they run out."""

    calls: list[dict] = field(default_factory=list)
    replies: list[UpstreamResponse | Exception] = field(default_factory=list)

    async def request(
        self,
        method,
        url,
        headers,
        body,
        *,
        api,
        retry: RetryPolicy = NO_RETRY,
        timeout: float | None = None,
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "api": api,
                "retry": retry,
                "timeout": timeout,
            }
        )
        reply = self.replies.pop(0) if self.replies else DEFAULT_REPLY
        if isinstance(reply, Exception):
            raise reply
        return reply


def as_upstream(fake: FakeUpstream) -> Upstream:
    return cast(Upstream, fake)


class FakeRequest:
    """The slice of SynapseRequest that RegisterResource touches."""

    def __init__(
        self,
        method: str = "POST",
        postpath: list[str] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        client: str | None = "203.0.113.7",
    ) -> None:
        self.method = method.encode()
        self.postpath = [p.encode() for p in (postpath or [])]
        self.content = BytesIO(body)
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._client = client
        self.code = 200
        self.response_headers: dict[str, str] = {}
        self.written = b""
        self.finished = False
        self._disconnected = False

    def getHeader(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    def getClientAddress(self) -> Any:
        return SimpleNamespace(host=self._client) if self._client else SimpleNamespace()

    def setResponseCode(self, code: int, message: bytes | None = None) -> None:
        self.code = code

    def setHeader(self, name: bytes | str, value: bytes | str) -> None:
        key = name.decode() if isinstance(name, bytes) else name
        val = value.decode() if isinstance(value, bytes) else value
        self.response_headers[key.lower()] = val

    def write(self, data: bytes) -> None:
        self.written += data

    def finish(self) -> None:
        self.finished = True


def post(
    body: bytes | str = b'{"email":"alice@example.com"}',
    *,
    content_type: str | None = "application/json",
    client: str | None = "203.0.113.7",
    postpath: list[str] | None = None,
) -> FakeRequest:
    headers = {"Content-Type": content_type} if content_type else {}
    raw = body.encode() if isinstance(body, str) else body
    return FakeRequest("POST", postpath, raw, headers, client)


SECRET = b"record-secret"


class FakeModuleApi:
    def __init__(self, redis_enabled: bool = True) -> None:
        self.registered: list[tuple[str, object]] = []
        self.server_name = "zuno.test"
        self.store = FakeStore()
        self.connection = FakeConnection()
        self._hs = SimpleNamespace(
            config=SimpleNamespace(
                redis=SimpleNamespace(redis_enabled=redis_enabled),
                key=SimpleNamespace(macaroon_secret_key=b"macaroon"),
            ),
            get_datastores=lambda: SimpleNamespace(main=self.store),
            get_outbound_redis_connection=lambda: self.connection,
        )

    def register_web_resource(self, path: str, resource: object) -> None:
        self.registered.append((path, resource))


def as_api(fake: FakeModuleApi) -> ModuleApi:
    """The fakes implement the slice the module uses; the cast keeps arg-type checked elsewhere."""
    return cast(ModuleApi, fake)
