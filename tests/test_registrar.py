import pytest
import pytest_twisted
from conftest import FakeConnection, FakeMailer, FakeStore, as_mailer, as_store, config, records
from prometheus_client import REGISTRY

from zuno_register import registrar as registrar_module
from zuno_register.code import ALPHABET, LENGTH
from zuno_register.registrar import DAILY_WINDOW, DailyCapExceeded, Registrar, Unavailable

KEY = "a" * 64
SENDS, CODE, DAILY = f"t:sends:{KEY}", f"t:code:{KEY}", "t:daily:2023-11-14"


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Setup:
    def __init__(self, **cfg):
        self.conn = FakeConnection()
        self.store = FakeStore(self.conn.clock)
        self.mailer = FakeMailer()
        self.cfg = config(**cfg)
        self.registrar = Registrar(
            self.cfg,
            as_store(self.store),
            records(self.conn),
            as_mailer(self.mailer),
            clock=self.conn.clock,
        )

    async def run(self, addr="alice@example.com", key=KEY):
        return await self.registrar(addr, key)

    def count(self, key):
        return self.conn._live(key)


@pytest_twisted.ensureDeferred
async def test_first_request_mints_stores_and_mails_one_code():
    s = Setup()
    assert await s.run() == "ok"
    (to, code, hours), *_ = s.mailer.sent
    assert (to, hours, len(s.mailer.sent)) == ("alice@example.com", 24, 1)
    assert len(code) == LENGTH and set(code) <= set(ALPHABET)
    assert s.conn._live(CODE) == code and s.conn.ttl(CODE) == 86400
    assert s.count(SENDS) == 1 and s.conn.ttl(SENDS) == 86400
    assert s.count(DAILY) == 1 and s.conn.ttl(DAILY) == DAILY_WINDOW
    token = s.store.tokens[code]
    assert token.uses_allowed == 1 and token.expiry_ms == (s.conn.clock() + 86400) * 1000


@pytest_twisted.ensureDeferred
async def test_a_re_request_resends_the_same_live_code():
    s = Setup()
    await s.run()
    assert await s.run() == "resent"
    assert [c for _, c, _ in s.mailer.sent] == [s.conn._live(CODE)] * 2
    assert len(s.store.tokens) == 1
    assert s.count(SENDS) == 2 and s.count(DAILY) == 2


@pytest_twisted.ensureDeferred
async def test_over_the_send_limit_is_silent_and_sends_nothing():
    s = Setup(sends_per_address=2)
    assert [await s.run() for _ in range(4)] == ["ok", "resent", "email_limited", "email_limited"]
    assert len(s.mailer.sent) == 2
    assert s.count(SENDS) == 2 and s.count(DAILY) == 2


@pytest_twisted.ensureDeferred
async def test_a_consumed_code_is_replaced():
    s = Setup()
    await s.run()
    first = s.conn._live(CODE)
    s.store.consume(first)
    assert await s.run() == "ok"
    second = s.conn._live(CODE)
    assert second != first and s.mailer.sent[-1][1] == second
    assert set(s.store.tokens) == {first, second}


@pytest_twisted.ensureDeferred
async def test_an_expired_record_gets_a_fresh_code():
    s = Setup()
    await s.run()
    first = s.conn._live(CODE)
    s.conn.clock.now += 86400 + 1
    assert await s.run() == "ok"
    assert s.conn._live(CODE) != first
    assert s.count(SENDS) == 1  # the window restarted


@pytest_twisted.ensureDeferred
async def test_a_failed_send_refunds_both_counters_and_keeps_the_code():
    s = Setup()
    await s.run()
    code = s.conn._live(CODE)
    s.mailer.fail = True
    with pytest.raises(Unavailable) as exc:
        await s.run()
    assert exc.value.api == "brevo"
    assert s.count(SENDS) == 1 and s.count(DAILY) == 1
    assert s.conn._live(CODE) == code and code in s.store.tokens
    s.mailer.fail = False
    assert await s.run() == "resent"
    assert s.mailer.sent[-1][1] == code


@pytest_twisted.ensureDeferred
async def test_a_refund_never_leaves_a_negative_counter():
    s = Setup()
    s.mailer.fail = True
    with pytest.raises(Unavailable):
        await s.run()
    assert SENDS not in s.conn.data and DAILY not in s.conn.data


@pytest_twisted.ensureDeferred
async def test_the_daily_cap_is_429_and_spends_no_address():
    s = Setup(daily_cap=1)
    await s.run()
    with pytest.raises(DailyCapExceeded):
        await s.run(addr="bob@example.com", key="b" * 64)
    assert s.count(DAILY) == 1
    assert f"t:sends:{'b' * 64}" not in s.conn.data
    assert len(s.mailer.sent) == 1


@pytest_twisted.ensureDeferred
async def test_a_zero_daily_cap_keeps_no_daily_key():
    s = Setup(daily_cap=0)
    await s.run()
    assert not [k for k in s.conn.data if "daily" in k]


@pytest_twisted.ensureDeferred
async def test_redis_failure_is_unavailable():
    s = Setup()
    s.conn.fail = ConnectionError("gone")
    with pytest.raises(Unavailable) as exc:
        await s.run()
    assert exc.value.api == "redis" and s.mailer.sent == []


@pytest_twisted.ensureDeferred
async def test_store_failure_is_unavailable_and_refunds():
    s = Setup()
    s.store.fail = RuntimeError("db down")
    with pytest.raises(Unavailable) as exc:
        await s.run()
    assert exc.value.api == "store"
    assert SENDS not in s.conn.data and DAILY not in s.conn.data
    assert s.mailer.sent == []


@pytest_twisted.ensureDeferred
async def test_two_collisions_are_a_store_failure(monkeypatch):
    s = Setup()
    s.store.tokens["SAMECODE22"] = s.store.tokens["OTHER22222"] = None  # type: ignore[assignment]
    codes = iter(["SAMECODE22", "OTHER22222"])
    monkeypatch.setattr(registrar_module, "new_code", lambda: next(codes))
    before = sample("zuno_register_upstream_errors_total", api="store", reason="collision")
    with pytest.raises(Unavailable) as exc:
        await s.run()
    assert exc.value.api == "store"
    assert sample("zuno_register_upstream_errors_total", api="store", reason="collision") == (
        before + 1
    )


@pytest_twisted.ensureDeferred
async def test_one_collision_retries_once(monkeypatch):
    s = Setup()
    await s.store.create_registration_token("SAMECODE22", 1, None)
    codes = iter(["SAMECODE22", "FRESH22222"])
    monkeypatch.setattr(registrar_module, "new_code", lambda: next(codes))
    assert await s.run() == "ok"
    assert s.mailer.sent[0][1] == "FRESH22222"


@pytest_twisted.ensureDeferred
async def test_a_lost_race_sends_the_winners_code_and_drops_ours():
    s = Setup()

    def someone_else_wins():
        s.conn.before_set = None
        s.conn.data[CODE] = ("WINNER2222", s.conn.clock() + 86400)
        s.store.tokens["WINNER2222"] = None  # type: ignore[assignment]

    s.conn.before_set = someone_else_wins
    assert await s.run() == "resent"
    assert s.mailer.sent[0][1] == "WINNER2222"
    assert set(s.store.tokens) == {"WINNER2222"}
