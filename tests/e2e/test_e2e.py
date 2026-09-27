"""The module inside a real Synapse. Needs Docker; run with `make e2e`.

Runs synapse:v1.161.0 with the module installed and a Redis container, both
on the host network (SQLite), against a stub Brevo. Proves loading,
parse_config, the private store and Redis handles, and the end-to-end
contract: the emailed code registers a user through Synapse's own
m.login.registration_token stage. Ports are overridable so two runs can
share a machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
SYNAPSE_PORT = int(os.environ.get("ZUNO_E2E_SYNAPSE_PORT", "18018"))
REDIS_PORT = int(os.environ.get("ZUNO_E2E_REDIS_PORT", "18379"))
STUB_PORT = int(os.environ.get("ZUNO_E2E_STUB_PORT", "18110"))
IMAGE = f"zuno-register-e2e-{SYNAPSE_PORT}"
CONTAINER = f"zuno-register-e2e-{SYNAPSE_PORT}"
REDIS_CONTAINER = f"zuno-register-e2e-redis-{SYNAPSE_PORT}"
SYNAPSE = f"http://127.0.0.1:{SYNAPSE_PORT}"
TOKEN = f"{SYNAPSE}/_synapse/client/zuno/register/token"
REGISTER = f"{SYNAPSE}/_matrix/client/v3/register"

HOMESERVER = f"""\
server_name: e2e
report_stats: false
pid_file: /data/homeserver.pid
signing_key_path: /data/signing.key
media_store_path: /data/media
database:
  name: sqlite3
  args:
    database: /data/homeserver.db
listeners:
  - port: {SYNAPSE_PORT}
    type: http
    bind_addresses: ['127.0.0.1']
    resources:
      - names: [client]
redis:
  enabled: true
  host: 127.0.0.1
  port: {REDIS_PORT}
enable_registration: true
registration_requires_token: true
macaroon_secret_key: e2e-macaroon
form_secret: e2e-form
rc_registration:
  per_second: 100
  burst_count: 100
rc_registration_token_validity:
  per_second: 100
  burst_count: 100
modules:
  - module: zuno_register.ZunoRegister
    config:
      brevo:
        api_key: BREVO-KEY
        sender_email: noreply@e2e.test
        base_url: http://127.0.0.1:{STUB_PORT}
      sends_per_address: 2
      rate_limit:
        per_second: 1
        burst: 10
"""


class Stub(BaseHTTPRequestHandler):
    seen: list[dict] = []
    status = 201

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        Stub.seen.append({"path": self.path, "api_key": self.headers.get("api-key"), "body": body})
        payload = b'{"messageId":"m1"}'
        self.send_response(Stub.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


def docker(*args: str, check: bool = True, **kw):
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True, **kw)


def http(method: str, url: str, body=None, content_type: str | None = "application/json"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def wait_for_synapse(deadline: float) -> None:
    while True:
        try:
            urllib.request.urlopen(f"{SYNAPSE}/health", timeout=2)
            return
        except Exception:
            if time.time() > deadline:
                logs = docker("logs", CONTAINER, check=False)
                raise RuntimeError(
                    "synapse did not become healthy:\n" + logs.stdout + logs.stderr
                ) from None
            time.sleep(1)


@pytest.fixture(scope="module")
def synapse():
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH")
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    data = Path(tempfile.mkdtemp(prefix="zuno-register-e2e-"))
    (data / "homeserver.yaml").write_text(HOMESERVER)
    as_user = ["--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{data}:/data"]
    homeserver = [
        "--entrypoint",
        "python",
        IMAGE,
        "-m",
        "synapse.app.homeserver",
        "--config-path",
        "/data/homeserver.yaml",
    ]
    try:
        docker("build", "-f", "tests/e2e/Dockerfile", "-t", IMAGE, ".", cwd=ROOT)
        docker("rm", "-f", CONTAINER, REDIS_CONTAINER, check=False)
        docker(
            "run",
            "-d",
            "--name",
            REDIS_CONTAINER,
            "--network",
            "host",
            "redis:8-alpine",
            "redis-server",
            "--port",
            str(REDIS_PORT),
            "--bind",
            "127.0.0.1",
        )
        docker("run", "--rm", *as_user, *homeserver, "--generate-keys")
        docker("run", "-d", "--name", CONTAINER, "--network", "host", *as_user, *homeserver)
        wait_for_synapse(time.time() + 90)
        yield SYNAPSE
    finally:
        docker("rm", "-f", CONTAINER, REDIS_CONTAINER, check=False)
        stub.shutdown()
        stub.server_close()
        shutil.rmtree(data, ignore_errors=True)


def request_code(email: str):
    return http("POST", TOKEN, {"email": email})


def last_code() -> str:
    body = Stub.seen[-1]["body"]
    return str(body["textContent"].split("\n", 1)[0])


def test_wrong_content_type_is_415(synapse):
    status, body = http("POST", TOKEN, {"email": "a@e2e.test"}, content_type="text/plain")
    assert (status, body["errcode"]) == (415, "M_UNKNOWN")


def test_other_paths_are_404(synapse):
    status, body = http("GET", TOKEN)
    assert (status, body["errcode"]) == (404, "M_UNRECOGNIZED")
    status, body = http("POST", TOKEN + "/x", {"email": "a@e2e.test"})
    assert (status, body["errcode"]) == (404, "M_UNRECOGNIZED")


def test_bad_email_is_400(synapse):
    status, body = request_code("nope")
    assert (status, body["errcode"]) == (400, "M_INVALID_PARAM")


def test_a_code_is_mailed_resent_then_silenced_and_registers_a_user(synapse):
    Stub.seen.clear()
    assert request_code("alice@e2e.test") == (202, {})
    assert request_code("Alice+again@e2e.test") == (202, {})
    assert request_code("alice@e2e.test") == (202, {})  # over sends_per_address: silent
    assert len(Stub.seen) == 2
    assert Stub.seen[0]["api_key"] == "BREVO-KEY"
    assert Stub.seen[0]["path"] == "/v3/smtp/email"
    assert Stub.seen[1]["body"]["to"] == [{"email": "Alice+again@e2e.test"}]
    code = last_code()
    assert last_code() == Stub.seen[0]["body"]["textContent"].split("\n", 1)[0]

    status, body = http("POST", REGISTER, {"username": "alice", "password": "wonderland"})
    assert status == 401 and "m.login.registration_token" in json.dumps(body["flows"])
    session = body["session"]
    status, body = http(
        "POST",
        REGISTER,
        {
            "username": "alice",
            "password": "wonderland",
            "auth": {"type": "m.login.registration_token", "token": code, "session": session},
        },
    )
    assert status == 200 and body["user_id"] == "@alice:e2e", body

    # Spent: the same code is refused in a fresh session.
    _, body = http("POST", REGISTER, {"username": "bob", "password": "wonderland"})
    status, body = http(
        "POST",
        REGISTER,
        {
            "username": "bob",
            "password": "wonderland",
            "auth": {
                "type": "m.login.registration_token",
                "token": code,
                "session": body["session"],
            },
        },
    )
    assert status != 200, body


def test_a_failed_send_is_502_and_the_next_attempt_resends_the_same_code(synapse):
    Stub.seen.clear()
    Stub.status = 500
    try:
        status, body = request_code("carol@e2e.test")
        assert (status, body["errcode"]) == (502, "M_UNKNOWN")
    finally:
        Stub.status = 201
    assert request_code("carol@e2e.test") == (202, {})
    first, second = (s["body"]["textContent"].split("\n", 1)[0] for s in Stub.seen)
    assert first == second
