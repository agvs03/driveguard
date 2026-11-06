from driveguard.clock import FakeClock
from driveguard.pipeline.broker import SqliteBroker

T = "t"


async def _broker(tmp_path, clock=None):
    clock = clock or FakeClock()
    return await SqliteBroker(tmp_path / "q.db", clock).connect(), clock


async def test_publish_claim_ack(tmp_path):
    b, _ = await _broker(tmp_path)
    await b.publish_many(T, [(None, "a"), (None, "b")])
    msgs = await b.claim(T, 10, 30)
    assert [m.body for m in msgs] == ["a", "b"] and all(m.attempts == 1 for m in msgs)
    assert all([await b.ack(m) for m in msgs])
    assert await b.claim(T, 10, 30) == []
    assert (await b.counts(T))["done"] == 2
    await b.close()


async def test_claimed_message_invisible_until_lease_expires(tmp_path):
    b, clock = await _broker(tmp_path)
    await b.publish(T, None, "x")
    first = await b.claim(T, 10, visibility_timeout_s=30)
    assert await b.claim(T, 10, 30) == []  # leased
    clock.advance(31)
    again = await b.claim(T, 10, 30)  # worker presumed dead -> redelivered
    assert [m.body for m in again] == ["x"] and again[0].attempts == 2
    assert not await b.ack(first[0]), "stale lease must not ack the new delivery"
    assert await b.ack(again[0])
    await b.close()


async def test_nack_delays_redelivery(tmp_path):
    b, clock = await _broker(tmp_path)
    await b.publish(T, None, "x")
    [m] = await b.claim(T, 1, 30)
    assert await b.nack(m, delay_s=5, error="boom")
    assert await b.claim(T, 1, 30) == []
    assert await b.next_visible_at_ms(T) == clock.now_ms() + 5000
    clock.advance(5)
    [m2] = await b.claim(T, 1, 30)
    assert m2.attempts == 2
    await b.close()


async def test_dead_letter_and_replay(tmp_path):
    b, _ = await _broker(tmp_path)
    await b.publish(T, "k", "poison")
    [m] = await b.claim(T, 1, 30)
    assert await b.dead_letter(m, "schema", "bad json")
    assert not await b.dead_letter(m, "schema", "again"), "already dead: lease no longer valid"
    [dl] = await b.list_dead()
    assert (dl.reason, dl.body, dl.key, dl.attempts) == ("schema", "poison", "k", 1)
    assert await b.replay_dead(reason="permanent") == 0
    assert await b.replay_dead(reason="schema") == 1
    assert await b.list_dead() == []  # replayed letters hidden by default
    [m2] = await b.claim(T, 1, 30)
    assert m2.body == "poison" and m2.attempts == 1
    await b.close()


async def test_messages_survive_restart(tmp_path):
    clock = FakeClock()
    b, _ = await _broker(tmp_path, clock)
    await b.publish_many(T, [(None, str(i)) for i in range(5)])
    await b.claim(T, 2, 30)  # in flight when the "process" dies
    await b.close()

    b2, _ = await _broker(tmp_path, clock)
    clock.advance(31)
    msgs = await b2.claim(T, 10, 30)
    assert sorted(m.body for m in msgs) == ["0", "1", "2", "3", "4"]
    await b2.close()
