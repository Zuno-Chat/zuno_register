"""Brevo transactional send (POST /v3/smtp/email). One attempt: a send is not idempotent."""

from __future__ import annotations

import html
import json
import logging

from .config import BrevoConfig
from .upstream import NO_RETRY, Upstream, UpstreamUnavailable

_logger = logging.getLogger(__name__)


class SendFailed(Exception):
    """Brevo unreachable or refused the send. Never carries the address or the code."""


def message(code: str, hours: int) -> tuple[str, str, str]:
    """(subject, html, text) for the built-in copy."""
    expiry = f"{hours} {'hour' if hours == 1 else 'hours'}"
    subject = "Your Zuno code"
    text = (
        f"{code}\n\nEnter this in the app to finish signing up. "
        f"It works once and expires in {expiry}.\n\nDidn't ask for this? Ignore this email.\n"
    )
    body = (
        '<p style="font-size:28px;letter-spacing:4px;font-family:monospace">'
        f"<strong>{html.escape(code)}</strong></p>"
        f"<p>Enter this in the app to finish signing up. It works once and expires in {expiry}.</p>"
        "<p>Didn't ask for this? Ignore this email.</p>"
    )
    return subject, body, text


class Mailer:
    def __init__(self, cfg: BrevoConfig, upstream: Upstream) -> None:
        self._cfg = cfg
        self._url = f"{cfg.base_url}/v3/smtp/email"
        self._headers = {
            "api-key": cfg.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._upstream = upstream

    def payload(self, to: str, code: str, hours: int) -> dict[str, object]:
        body: dict[str, object] = {
            "sender": {"email": self._cfg.sender_email, "name": self._cfg.sender_name},
            "to": [{"email": to}],
        }
        if self._cfg.template_id:
            body["templateId"] = self._cfg.template_id
            body["params"] = {"code": code, "expires_in_hours": hours}
        else:
            subject, html_body, text = message(code, hours)
            body.update(subject=subject, htmlContent=html_body, textContent=text)
        return body

    async def send(self, to: str, code: str, hours: int) -> None:
        raw = json.dumps(self.payload(to, code, hours)).encode()
        try:
            response = await self._upstream.request(
                "POST", self._url, self._headers, raw, api="brevo", retry=NO_RETRY
            )
        except UpstreamUnavailable as e:
            raise SendFailed(e.reason) from e
        if not 200 <= response.status < 300:
            # Brevo's body may echo the address; only the status is logged.
            _logger.warning("brevo refused a send: status=%d", response.status)
            raise SendFailed(f"status {response.status}")
