"""Module configuration: the ``config`` map under ``modules:`` in homeserver.yaml.

Every problem is collected into one ConfigError so Synapse refuses to start
with the complete list rather than one item per restart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.module_api.errors import ConfigError

DEFAULT_BASE_URL = "https://api.brevo.com"
DAY = 86400.0

_DURATION = re.compile(r"^(\d+)(ms|s|m|h|d)?$")
_UNIT_SECONDS = {None: 1.0, "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": DAY}
_BREVO_KEYS = {
    "api_key",
    "api_key_path",
    "sender_email",
    "sender_name",
    "template_id",
    "base_url",
    "timeout",
}
_TOP_KEYS = {
    "brevo",
    "token_ttl",
    "sends_per_address",
    "daily_cap",
    "rate_limit",
    "redis_timeout",
}


@dataclass(frozen=True)
class BrevoConfig:
    api_key: str
    sender_email: str
    sender_name: str = "Zuno"
    # 0: the built-in copy; else that Brevo template gets {code, expires_in_hours}
    template_id: int = 0
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 10.0  # seconds; a send is one attempt


@dataclass(frozen=True)
class RateLimitConfig:
    """Per client IP. A day of it must stay under daily_cap, or one address can drain the cap."""

    per_second: float = 1 / 600  # one per ten minutes
    burst: int = 3


@dataclass(frozen=True)
class Config:
    brevo: BrevoConfig | None
    token_ttl: float = DAY  # seconds; also the per-address window, so a silent 202 has a live code
    sends_per_address: int = 3  # emails per address per token_ttl
    daily_cap: int = 500  # emails per day across every caller; 0 disables
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    redis_timeout: float = 2.0  # seconds, per command


def parse_duration(value: object) -> float:
    """Seconds from a number of seconds or a Synapse-style string: 500ms, 10s, 2h, 1d."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"negative duration: {value!r}")
        return float(value)
    if isinstance(value, str):
        m = _DURATION.match(value.strip())
        if m:
            return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    raise ValueError(f"not a duration: {value!r}")


def parse_config(raw: object) -> Config:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("zuno_register: config must be a map")
    errors = [f"unknown key {key!r}" for key in sorted(set(raw) - _TOP_KEYS)]
    brevo = _parse_brevo(raw["brevo"], errors) if "brevo" in raw else None
    token_ttl = _duration(raw, "token_ttl", Config.token_ttl, errors)
    redis_timeout = _duration(raw, "redis_timeout", Config.redis_timeout, errors)
    sends = _int(raw, "sends_per_address", Config.sends_per_address, 1, errors)
    daily_cap = _int(raw, "daily_cap", Config.daily_cap, 0, errors)
    rate_limit = _parse_rate_limit(raw.get("rate_limit"), errors)
    if token_ttl < 3600:
        errors.append("token_ttl must be at least 1h: the mail states the lifetime in whole hours")
    if redis_timeout <= 0:
        errors.append("redis_timeout must be positive")
    if rate_limit.burst < sends:
        errors.append(
            "rate_limit: burst must be at least sends_per_address, or the re-requests "
            "from one address are refused before its last code is sent"
        )
    per_ip_day = rate_limit.per_second * DAY + rate_limit.burst
    if daily_cap and per_ip_day >= daily_cap:
        errors.append(
            f"rate_limit: a day of one address ({per_ip_day:.0f} requests) must stay "
            f"under daily_cap ({daily_cap}), or one address can close signup for everyone"
        )
    if errors:
        raise ConfigError("zuno_register: " + "; ".join(errors))
    return Config(
        brevo=brevo,
        token_ttl=token_ttl,
        sends_per_address=sends,
        daily_cap=daily_cap,
        rate_limit=rate_limit,
        redis_timeout=redis_timeout,
    )


def _parse_brevo(section: Any, errors: list[str]) -> BrevoConfig | None:
    if not isinstance(section, dict):
        errors.append("brevo must be a map")
        return None
    before = len(errors)
    for key in sorted(set(section) - _BREVO_KEYS):
        errors.append(f"brevo: unknown key {key!r}")
    api_key = _secret(section, "api_key", errors)
    sender_email = _required_str(section, "sender_email", errors)
    if sender_email is not None and "@" not in sender_email:
        errors.append("brevo: sender_email must be an email address")
    sender_name = section.get("sender_name", BrevoConfig.sender_name)
    if not isinstance(sender_name, str) or not sender_name.strip():
        errors.append("brevo: sender_name must be a non-empty string")
        sender_name = BrevoConfig.sender_name
    template_id = _int(section, "template_id", BrevoConfig.template_id, 0, errors, "brevo: ")
    base_url = section.get("base_url", DEFAULT_BASE_URL)
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        errors.append("brevo: base_url must be an http(s) URL")
        base_url = DEFAULT_BASE_URL
    timeout = _duration(section, "timeout", BrevoConfig.timeout, errors, "brevo: ")
    if timeout <= 0:
        errors.append("brevo: timeout must be positive")
    if len(errors) > before or api_key is None or sender_email is None:
        return None
    return BrevoConfig(
        api_key=api_key,
        sender_email=sender_email,
        sender_name=sender_name.strip(),
        template_id=template_id,
        base_url=base_url.rstrip("/"),
        timeout=timeout,
    )


def _required_str(section: dict[str, Any], key: str, errors: list[str]) -> str | None:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"brevo: {key} must be a non-empty string")
        return None
    return value.strip()


def _secret(section: dict[str, Any], key: str, errors: list[str]) -> str | None:
    """Exactly one of ``key`` (inline) or ``key_path`` (file, one trailing newline dropped)."""
    inline, path = section.get(key), section.get(f"{key}_path")
    if (inline is None) == (path is None):
        errors.append(f"brevo: set exactly one of {key} and {key}_path")
        return None
    if inline is not None:
        if not isinstance(inline, str) or not inline:
            errors.append(f"brevo: {key} must be a non-empty string")
            return None
        return inline
    if not isinstance(path, str) or not path:
        errors.append(f"brevo: {key}_path must be a file path")
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        errors.append(f"brevo: {key}_path: {e}")
        return None
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        errors.append(f"brevo: {key}_path: {path} is empty")
        return None
    return text


def _duration(
    section: dict[str, Any], key: str, default: float, errors: list[str], prefix: str = ""
) -> float:
    """An unparsable value falls back to the default; the recorded error is what stops startup."""
    if key not in section:
        return default
    try:
        return parse_duration(section[key])
    except ValueError as e:
        errors.append(f"{prefix}{key}: {e}")
        return default


def _int(
    section: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    errors: list[str],
    prefix: str = "",
) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.append(f"{prefix}{key} must be an integer of at least {minimum}")
        return default
    return value


def _parse_rate_limit(section: Any, errors: list[str]) -> RateLimitConfig:
    defaults = RateLimitConfig()
    if section is None:
        return defaults
    if not isinstance(section, dict):
        errors.append("rate_limit must be a map")
        return defaults
    before = len(errors)
    for key in sorted(set(section) - {"per_second", "burst"}):
        errors.append(f"rate_limit: unknown key {key!r}")
    per_second = section.get("per_second", defaults.per_second)
    burst = section.get("burst", defaults.burst)
    if isinstance(per_second, bool) or not isinstance(per_second, (int, float)) or per_second <= 0:
        errors.append("rate_limit: per_second must be a positive number")
    if isinstance(burst, bool) or not isinstance(burst, int) or burst < 1:
        errors.append("rate_limit: burst must be an integer of at least 1")
    if len(errors) > before:
        return defaults
    return RateLimitConfig(per_second=float(per_second), burst=burst)
