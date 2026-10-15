import contextlib
import json
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_twisted
from prometheus_client import REGISTRY
from twisted.internet import reactor as _reactor
from twisted.internet.protocol import Factory, Protocol
from twisted.internet.task import deferLater
from twisted.web.resource import IResource, Resource
from twisted.web.server import NOT_DONE_YET, Site

from zuno_register.upstream import Upstream, UpstreamUnavailable

reactor: Any = _reactor  # the reactor module has no usable static type


def sample(name, **labels):
    """A metric's current value, 0 before it is first observed."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Echo(Resource):
    isLeaf = True

    def render(self, request):
        body = request.content.read()
        return json.dumps(
            {
                "method": request.method.decode(),
                "path": request.path.decode(),
                "authorization": request.getHeader("Authorization"),
                "content_type": request.getHeader("Content-Type"),
                "body": body.decode(),
            }
        ).encode()


class Busy(Resource):
    isLeaf = True

    def render(self, request):
        request.setResponseCode(503)
        return b"busy"


class Stall(Resource):
    """Never finishes; with send_headers it sends headers and part of a body first."""

    isLeaf = True

    def __init__(self, send_headers):
        super().__init__()
        self.send_headers = send_headers
        self.requests = []
        self.closed = 0

    def render(self, request):
        def note_closed(_):
            self.closed += 1

        self.requests.append(request)
        request.notifyFinish().addBoth(note_closed)
        if self.send_headers:
            request.write(b"partial")
        return NOT_DONE_YET


class BigStall(Resource):
    """Writes an oversized body and then stalls, never finishing."""

    isLeaf = True

    def __init__(self):
        super().__init__()
        self.requests = []

    def render(self, request):
        self.requests.append(request)
        request.notifyFinish().addErrback(lambda _: None)
        request.write(b"x" * (2 * 1024 * 1024))
        return NOT_DONE_YET


class Big(Resource):
    isLeaf = True

    def render(self, request):
        return b"x" * (2 * 1024 * 1024)


class Redirect(Resource):
    isLeaf = True

    def render(self, request):
        request.setResponseCode(302)
        request.setHeader(b"Location", b"/echo")
        return b""


class Canned(Protocol):
    """Answers any request with a fixed response, then closes the connection."""

    response = b""
    replied = False

    def dataReceived(self, data):
        if self.replied:
            return
        self.replied = True
        self.transport.write(self.response)  # type: ignore[union-attr,call-arg,misc]
        self.transport.loseConnection()  # type: ignore[union-attr,misc]


class Truncated(Canned):
    """Headers and a body the connection ends early: Twisted's PotentialDataLoss."""

    response = b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\npartial"


@pytest.fixture
def server():
    stall, stall_body, big_stall = Stall(send_headers=False), Stall(send_headers=True), BigStall()
    root = Resource()
    children: dict[bytes, Resource] = {
        b"echo": Echo(),
        b"busy": Busy(),
        b"stall": stall,
        b"stall-body": stall_body,
        b"big": Big(),
        b"big-stall": big_stall,
        b"redirect": Redirect(),
    }
    for name, child in children.items():
        root.putChild(name, cast(IResource, child))
    port = reactor.listenTCP(0, Site(root), interface="127.0.0.1")
    yield SimpleNamespace(
        base=f"http://127.0.0.1:{port.getHost().port}",
        stall=stall,
        stall_body=stall_body,
    )
    for unfinished in (stall, stall_body, big_stall):
        for request in unfinished.requests:
            with contextlib.suppress(Exception):
                request.finish()
    pytest_twisted.blockon(port.stopListening())


@pytest.fixture
def client():
    """Builds Upstreams and closes their connection pools when the test ends."""
    made = []

    def make(**kw):
        upstream = Upstream(**kw)
        made.append(upstream)
        return upstream

    yield make
    for upstream in made:
        pytest_twisted.blockon(upstream.close())


@pytest.fixture
def truncating():
    port = reactor.listenTCP(0, Factory.forProtocol(Truncated), interface="127.0.0.1")
    yield f"http://127.0.0.1:{port.getHost().port}/"
    pytest_twisted.blockon(port.stopListening())


@pytest_twisted.ensureDeferred
async def test_forwards_method_headers_and_body(server, client):
    resp = await client(timeout=5).request(
        "POST",
        server.base + "/echo",
        {"Authorization": "Bearer s3cret", "Content-Type": "application/json"},
        b'{"a":1}',
        api="test",
    )
    assert resp.status == 200
    assert json.loads(resp.body) == {
        "method": "POST",
        "path": "/echo",
        "authorization": "Bearer s3cret",
        "content_type": "application/json",
        "body": '{"a":1}',
    }


@pytest_twisted.ensureDeferred
async def test_get_sends_no_body(server, client):
    resp = await client(timeout=5).request("GET", server.base + "/echo", {}, None, api="test")
    echoed = json.loads(resp.body)
    assert (echoed["method"], echoed["body"], echoed["content_type"]) == ("GET", "", None)


@pytest_twisted.ensureDeferred
async def test_error_status_is_relayed_not_raised(server, client):
    before = sample("zuno_register_upstream_requests_total", api="test", code="503")
    resp = await client(timeout=5).request("GET", server.base + "/busy", {}, None, api="test")
    assert (resp.status, resp.body) == (503, b"busy")
    assert sample("zuno_register_upstream_requests_total", api="test", code="503") - before == 1


@pytest_twisted.ensureDeferred
async def test_headers_timeout_is_unavailable(server, client):
    before = sample("zuno_register_upstream_errors_total", api="test", reason="timeout")
    observed = sample("zuno_register_upstream_seconds_count", api="test")
    with pytest.raises(UpstreamUnavailable) as exc:
        await client(timeout=0.2).request("GET", server.base + "/stall", {}, None, api="test")
    assert exc.value.reason == "timeout"
    assert sample("zuno_register_upstream_errors_total", api="test", reason="timeout") - before == 1
    # Failed attempts are timed too, not only the ones that answer.
    assert sample("zuno_register_upstream_seconds_count", api="test") - observed == 1
    await deferLater(reactor, 0.2, lambda: None)
    assert server.stall.closed == 1


def test_idle_pooled_connections_expire_before_the_peer_closes_them(client):
    # Cloudflare closes idle keep-alives on its own schedule; a connection
    # reused after that fails without a response byte. One minute keeps the
    # pool well inside the window.
    assert client(timeout=5)._pool.cachedConnectionTimeout == 60


@pytest_twisted.ensureDeferred
async def test_body_timeout_is_unavailable(server, client):
    with pytest.raises(UpstreamUnavailable) as exc:
        await client(timeout=0.2).request("GET", server.base + "/stall-body", {}, None, api="test")
    assert exc.value.reason == "timeout"
    await deferLater(reactor, 0.2, lambda: None)
    assert server.stall_body.closed == 1


@pytest_twisted.ensureDeferred
async def test_connect_failure_is_unavailable(client):
    before = sample("zuno_register_upstream_errors_total", api="test", reason="connect")
    with pytest.raises(UpstreamUnavailable) as exc:
        await client(timeout=2).request("GET", "http://127.0.0.1:1/", {}, None, api="test")
    assert exc.value.reason == "connect"
    assert sample("zuno_register_upstream_errors_total", api="test", reason="connect") - before == 1


@pytest_twisted.ensureDeferred
async def test_oversized_body_is_unavailable(server, client):
    with pytest.raises(UpstreamUnavailable) as exc:
        await client(timeout=5, max_body=1024).request(
            "GET", server.base + "/big", {}, None, api="test"
        )
    assert exc.value.reason == "too_large"


@pytest_twisted.ensureDeferred
async def test_oversized_body_that_stalls_is_too_large_not_a_timeout(server, client):
    started = time.monotonic()
    with pytest.raises(UpstreamUnavailable) as exc:
        await client(timeout=5, max_body=1024).request(
            "GET", server.base + "/big-stall", {}, None, api="test"
        )
    assert exc.value.reason == "too_large"
    assert time.monotonic() - started < 2


@pytest_twisted.ensureDeferred
async def test_truncated_body_is_unavailable(truncating, client):
    with pytest.raises(UpstreamUnavailable) as exc:
        await client(timeout=5).request("GET", truncating, {}, None, api="test")
    assert exc.value.reason == "other"


@pytest_twisted.ensureDeferred
async def test_unencodable_header_is_unavailable(server, client):
    with pytest.raises(UpstreamUnavailable) as exc:
        await client(timeout=5).request(
            "GET", server.base + "/echo", {"X-Snow": "☃"}, None, api="test"
        )
    assert exc.value.reason == "other"


@pytest_twisted.ensureDeferred
async def test_redirect_is_relayed_not_followed(server, client):
    resp = await client(timeout=5).request("GET", server.base + "/redirect", {}, None, api="test")
    assert resp.status == 302
