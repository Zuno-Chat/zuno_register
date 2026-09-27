import json
from dataclasses import replace

import pytest
import pytest_twisted
from conftest import BREVO, FakeUpstream, as_upstream

from zuno_register.mail import Mailer, SendFailed, message
from zuno_register.upstream import NO_RETRY, UpstreamResponse, UpstreamUnavailable


def test_message_copy():
    subject, html, text = message("ABCD&EFGHJ", 24)
    assert subject == "Your Zuno code"
    assert text.startswith("ABCD&EFGHJ\n\n") and "expires in 24 hours" in text
    assert "<strong>ABCD&amp;EFGHJ</strong>" in html and "expires in 24 hours" in html
    assert "expires in 1 hour." in message("X", 1)[2]


def test_payload_built_in_copy_and_template():
    plain = Mailer(BREVO, as_upstream(FakeUpstream())).payload("a@b.io", "CODE", 24)
    assert plain["sender"] == {"email": "noreply@zuno.chat", "name": "Zuno"}
    assert plain["to"] == [{"email": "a@b.io"}]
    assert plain["subject"] == "Your Zuno code" and "CODE" in str(plain["htmlContent"])
    templated = Mailer(replace(BREVO, template_id=7), as_upstream(FakeUpstream())).payload(
        "a@b.io", "CODE", 24
    )
    assert templated["templateId"] == 7
    assert templated["params"] == {"code": "CODE", "expires_in_hours": 24}
    assert "subject" not in templated


@pytest_twisted.ensureDeferred
async def test_send_posts_once_with_the_api_key():
    up = FakeUpstream()
    await Mailer(BREVO, as_upstream(up)).send("a@b.io", "CODE", 24)
    (call,) = up.calls
    assert (call["method"], call["url"], call["api"]) == (
        "POST",
        "https://api.brevo.com/v3/smtp/email",
        "brevo",
    )
    assert call["headers"]["api-key"] == "k" and call["retry"] == NO_RETRY
    assert json.loads(call["body"])["to"] == [{"email": "a@b.io"}]


@pytest_twisted.ensureDeferred
async def test_a_refusal_or_outage_is_send_failed():
    up = FakeUpstream(replies=[UpstreamResponse(400, "application/json", b'{"code":"bad"}')])
    with pytest.raises(SendFailed):
        await Mailer(BREVO, as_upstream(up)).send("a@b.io", "CODE", 24)
    up = FakeUpstream(replies=[UpstreamUnavailable("timeout")])
    with pytest.raises(SendFailed):
        await Mailer(BREVO, as_upstream(up)).send("a@b.io", "CODE", 24)
