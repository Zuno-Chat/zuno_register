import pytest
import pytest_twisted
from conftest import FakeConnection, records
from prometheus_client import REGISTRY

from zuno_register.redis import RedisUnavailable


def errors(reason):
    return (
        REGISTRY.get_sample_value(
            "zuno_register_upstream_errors_total", {"api": "redis", "reason": reason}
        )
        or 0.0
    )


@pytest_twisted.ensureDeferred
async def test_count_starts_the_window_once_and_uncount_gives_back():
    conn = FakeConnection()
    r = records(conn)
    assert [await r.count("k", 100) for _ in range(3)] == [1, 2, 3]
    conn.clock.now += 50
    assert await r.count("k", 100) == 4
    assert conn.ttl("t:k") == 50  # the second count did not restart the window
    await r.uncount("k")
    assert conn._live("t:k") == 3 and conn.ttl("t:k") == 50
    for _ in range(3):
        await r.uncount("k")
    assert "t:k" not in conn.data


@pytest_twisted.ensureDeferred
async def test_uncount_after_the_window_leaves_nothing_behind():
    conn = FakeConnection()
    r = records(conn)
    await r.count("k", 10)
    conn.clock.now += 11
    await r.uncount("k")
    assert "t:k" not in conn.data
    assert await r.count("k", 10) == 1


@pytest_twisted.ensureDeferred
async def test_put_code_only_if_absent_returns_the_holder():
    conn = FakeConnection()
    r = records(conn)
    assert await r.put_code("c", "FIRST22222", 60, only_if_absent=True) == "FIRST22222"
    assert await r.put_code("c", "SECOND2222", 60, only_if_absent=True) == "FIRST22222"
    assert await r.put_code("c", "THIRD22222", 60, only_if_absent=False) == "THIRD22222"
    assert await r.get_code("c") == "THIRD22222" and conn.ttl("t:c") == 60
    conn.clock.now += 61
    assert await r.get_code("c") is None


@pytest_twisted.ensureDeferred
async def test_an_all_digit_code_survives_number_conversion():
    conn = FakeConnection()
    r = records(conn)
    await r.put_code("c", "2345678923", 60, only_if_absent=True)
    assert await r.get_code("c") == "2345678923"


@pytest_twisted.ensureDeferred
async def test_a_failed_command_is_unavailable_with_reason_error():
    conn = FakeConnection()
    conn.fail = ConnectionError("no connection")
    before = errors("error")
    with pytest.raises(RedisUnavailable) as exc:
        await records(conn).count("k", 10)
    assert exc.value.reason == "error" and errors("error") == before + 1


@pytest_twisted.ensureDeferred
async def test_a_synchronous_raise_is_unavailable_too():
    class Broken:
        def eval(self, *a, **kw):
            raise ConnectionError("not connected")

    with pytest.raises(RedisUnavailable) as exc:
        await records(Broken()).count("k", 10)  # type: ignore[arg-type]
    assert exc.value.reason == "error"


@pytest_twisted.ensureDeferred
async def test_a_silent_redis_is_a_timeout():
    conn = FakeConnection()
    conn.hang = True
    before = errors("timeout")
    with pytest.raises(RedisUnavailable) as exc:
        await records(conn, timeout=0.05).get_code("c")
    assert exc.value.reason == "timeout" and errors("timeout") == before + 1
