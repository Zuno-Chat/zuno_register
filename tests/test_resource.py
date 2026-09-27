import hashlib
import hmac

import pytest
import pytest_twisted
from conftest import SECRET, FakeRequest, post
from prometheus_client import REGISTRY
from synapse.module_api.errors import Codes, SynapseError

from zuno_register.ratelimit import RateLimiter
from zuno_register.registrar import DailyCapExceeded, Unavailable
from zuno_register.resource import RegisterResource, ip_key

ALICE_KEY = hmac.new(SECRET, b"alice@example.com", hashlib.sha256).hexdigest()


def sample(result):
    return REGISTRY.get_sample_value("zuno_register_requests_total", {"result": result}) or 0.0


class Recorder:
    def __init__(self, outcome="ok"):
        self.calls = []
        self.outcome = outcome

    async def __call__(self, addr, key):
        self.calls.append((addr, key))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def resource(outcome="ok", *, per_second=100.0, burst=100, **kw):
    handler = Recorder(outcome)
    res = RegisterResource(RateLimiter(per_second, burst), handler, record_secret=SECRET, **kw)
    return res, handler


async def expect_error(res, req, code, errcode):
    with pytest.raises(SynapseError) as exc:
        await res._serve(req)
    assert (exc.value.code, exc.value.errcode) == (code, errcode)
    return exc.value


def test_ip_key_groups_ipv6_by_64_and_unwraps_mapped_v4():
    assert ip_key("203.0.113.7") == "203.0.113.7"
    assert ip_key("2001:db8:1:2:aaaa::1") == ip_key("2001:db8:1:2:bbbb::2") == "2001:db8:1:2::/64"
    assert ip_key("2001:db8:1:3::1") != ip_key("2001:db8:1:2::1")
    assert ip_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert ip_key(None) == "" and ip_key("not-an-ip") == "not-an-ip"


@pytest_twisted.ensureDeferred
async def test_happy_path_answers_202_and_hashes_the_address():
    res, handler = resource()
    req = post('{"email": " Alice+news@Example.com "}')
    before = sample("ok")
    await res._serve(req)
    assert handler.calls == [("Alice+news@Example.com", ALICE_KEY)]
    assert (req.code, req.written, req.finished) == (202, b"{}", True)
    assert req.response_headers == {
        "content-type": "application/json",
        "cache-control": "no-store",
        "content-length": "2",
    }
    assert sample("ok") == before + 1


@pytest_twisted.ensureDeferred
async def test_other_verbs_and_sub_paths_are_404():
    res, handler = resource()
    await expect_error(res, FakeRequest("GET"), 404, Codes.UNRECOGNIZED)
    await expect_error(res, post(postpath=["x"]), 404, Codes.UNRECOGNIZED)
    await expect_error(res, post(postpath=[""]), 404, Codes.UNRECOGNIZED)
    assert handler.calls == []


@pytest_twisted.ensureDeferred
async def test_content_type_must_be_json():
    res, handler = resource()
    await expect_error(res, post(content_type=None), 415, Codes.UNKNOWN)
    await expect_error(res, post(content_type="text/plain"), 415, Codes.UNKNOWN)
    await res._serve(post(content_type="Application/JSON; charset=utf-8"))
    assert len(handler.calls) == 1


@pytest_twisted.ensureDeferred
async def test_oversized_body_is_413():
    res, handler = resource(max_body=64)
    await expect_error(res, post(b'{"email":"' + b"a" * 60 + b'@x.io"}'), 413, Codes.TOO_LARGE)
    assert handler.calls == []


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'{"email": 5}',
        b'{"email": "alice@example.com", "display_name": "A"}',
        b'{"email": "nope"}',
        b'{"email": "alice@localhost"}',
        b'{"email": "Alice <alice@example.com>"}',
        b'{"email": "a\\nb@example.com"}',
    ],
)
@pytest_twisted.ensureDeferred
async def test_bad_bodies_are_400(body):
    res, handler = resource()
    await expect_error(res, post(body), 400, Codes.INVALID_PARAM)
    assert handler.calls == []


@pytest_twisted.ensureDeferred
async def test_per_ip_limit_answers_429_with_retry_after_before_the_registrar():
    res, handler = resource(per_second=1, burst=1)
    await res._serve(post())
    err = await expect_error(res, post(), 429, Codes.LIMIT_EXCEEDED)
    assert err.error_dict(None)["retry_after_ms"] == 1000
    assert err.headers == {"Retry-After": "1"}
    await res._serve(post(client="203.0.113.8"))
    assert len(handler.calls) == 2


@pytest_twisted.ensureDeferred
async def test_an_invalid_body_never_spends_the_ip_allowance():
    res, _ = resource(per_second=1, burst=1)
    await expect_error(res, post(b"{}"), 400, Codes.INVALID_PARAM)
    await res._serve(post())


@pytest_twisted.ensureDeferred
async def test_daily_cap_is_429():
    res, _ = resource(DailyCapExceeded())
    before = sample("global_limited")
    await expect_error(res, post(), 429, Codes.LIMIT_EXCEEDED)
    assert sample("global_limited") == before + 1


@pytest_twisted.ensureDeferred
async def test_unavailable_is_502_labelled_by_api():
    res, _ = resource(Unavailable("brevo"))
    before = sample("brevo_error")
    err = await expect_error(res, post(), 502, Codes.UNKNOWN)
    assert err.msg == "Registration service unavailable"
    assert sample("brevo_error") == before + 1


@pytest_twisted.ensureDeferred
async def test_a_silent_accept_looks_exactly_like_a_send():
    sent, _ = resource("ok")
    silent, _ = resource("email_limited")
    a, b = post(), post()
    await sent._serve(a)
    await silent._serve(b)
    assert (a.code, a.written, a.response_headers) == (b.code, b.written, b.response_headers)


@pytest_twisted.ensureDeferred
async def test_a_disconnected_client_gets_no_write():
    res, _ = resource()
    req = post()
    req._disconnected = True
    await res._serve(req)
    assert (req.written, req.finished) == (b"", False)
