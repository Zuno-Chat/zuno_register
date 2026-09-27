import pytest
from synapse.module_api.errors import ConfigError

from zuno_register.config import BrevoConfig, Config, RateLimitConfig, parse_config, parse_duration

BREVO = {"api_key": "k", "sender_email": "noreply@zuno.chat"}


def test_defaults():
    cfg = parse_config({"brevo": BREVO})
    assert cfg == Config(
        brevo=BrevoConfig(api_key="k", sender_email="noreply@zuno.chat"),
        token_ttl=86400.0,
        sends_per_address=3,
        daily_cap=500,
        rate_limit=RateLimitConfig(),
        redis_timeout=2.0,
    )


def test_empty_config_registers_nothing():
    assert parse_config(None).brevo is None
    assert parse_config({}).brevo is None


def test_everything_set(tmp_path):
    secret = tmp_path / "key"
    secret.write_text("from-file\n")
    cfg = parse_config(
        {
            "brevo": {
                "api_key_path": str(secret),
                "sender_email": "hi@zuno.chat",
                "sender_name": "Zuno Chat",
                "template_id": 12,
                "base_url": "https://brevo.test/",
                "timeout": "3s",
            },
            "token_ttl": "2h",
            "sends_per_address": 2,
            "daily_cap": 0,
            "rate_limit": {"per_second": 0.5, "burst": 4},
            "redis_timeout": "500ms",
        }
    )
    assert cfg.brevo == BrevoConfig(
        "from-file", "hi@zuno.chat", "Zuno Chat", 12, "https://brevo.test", 3.0
    )
    assert (cfg.token_ttl, cfg.sends_per_address, cfg.daily_cap) == (7200.0, 2, 0)
    assert cfg.rate_limit == RateLimitConfig(0.5, 4) and cfg.redis_timeout == 0.5


def test_durations():
    assert [parse_duration(v) for v in ("500ms", "10s", "2m", "2h", "1d", 30, 1.5)] == [
        0.5,
        10.0,
        120.0,
        7200.0,
        86400.0,
        30.0,
        1.5,
    ]
    for bad in ("", "abc", "10x", -1, True, None):
        with pytest.raises(ValueError):
            parse_duration(bad)


def error(raw):
    with pytest.raises(ConfigError) as exc:
        parse_config(raw)
    return str(exc.value)


def test_every_problem_is_reported_at_once():
    msg = error(
        {
            "brevo": {"api_key": "", "sender_email": "nope", "bogus": 1},
            "token_ttl": "10m",
            "sends_per_address": 0,
            "daily_cap": -1,
            "rate_limit": {"per_second": 0, "burst": "x"},
            "redis_timeout": 0,
            "extra": True,
        }
    )
    for part in (
        "unknown key 'extra'",
        "brevo: unknown key 'bogus'",
        "brevo: api_key must be a non-empty string",
        "brevo: sender_email must be an email address",
        "token_ttl must be at least 1h",
        "sends_per_address must be an integer of at least 1",
        "daily_cap must be an integer of at least 0",
        "rate_limit: per_second must be a positive number",
        "rate_limit: burst must be an integer of at least 1",
        "redis_timeout must be positive",
    ):
        assert part in msg


def test_secret_needs_exactly_one_source(tmp_path):
    assert "exactly one of api_key and api_key_path" in error({"brevo": {"sender_email": "a@b.c"}})
    assert "exactly one of api_key and api_key_path" in error(
        {"brevo": {"api_key": "k", "api_key_path": "/x", "sender_email": "a@b.c"}}
    )
    assert "api_key_path" in error(
        {"brevo": {"api_key_path": str(tmp_path / "missing"), "sender_email": "a@b.c"}}
    )
    empty = tmp_path / "empty"
    empty.write_text("\n")
    assert "is empty" in error({"brevo": {"api_key_path": str(empty), "sender_email": "a@b.c"}})


def test_cross_field_rules():
    assert "burst must be at least sends_per_address" in error(
        {"brevo": BREVO, "sends_per_address": 5, "rate_limit": {"per_second": 0.001, "burst": 4}}
    )
    assert "must stay under daily_cap" in error(
        {"brevo": BREVO, "daily_cap": 100, "rate_limit": {"per_second": 0.01, "burst": 3}}
    )
    # A zero cap turns the second rule off.
    parse_config({"brevo": BREVO, "daily_cap": 0, "rate_limit": {"per_second": 1, "burst": 3}})


def test_not_a_map():
    assert "config must be a map" in error([])
    assert "brevo must be a map" in error({"brevo": "x"})
    assert "rate_limit must be a map" in error({"brevo": BREVO, "rate_limit": 3})
