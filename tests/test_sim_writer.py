"""Raw writer: alias guard, strictly increasing timestamps, batch limits, retries (AD-1, AD-3, AD-17)."""

from __future__ import annotations

import pytest

from facility.model import load
from sim.writer import AliasError, RawWriter, TimestampError, WriteError
from tests.sim_fakes import FakeClientError, FakeSiteWise

F = load(stage="poc-dj")
RAW = [F.alias(p.id, m) for p in F.pipes for m in p.sensors]
T0 = 1_790_000_000


def writer(fake=None, allowed=RAW, **kw):
    fake = fake or FakeSiteWise()
    return RawWriter(fake, allowed=allowed, sleep=lambda s: None, **kw), fake


@pytest.mark.parametrize(
    "alias",
    [F.derived_alias("P-007", "alert_state"), F.derived_alias("P-007", "flow_in_dq"), F.attr_alias("P-007", "flow_in_max")],
)
def test_refuses_derived_and_attr_before_any_api_call(alias):
    w, fake = writer()
    with pytest.raises(AliasError):
        w.add(alias, T0, 1.0)
    w2, fake2 = writer(allowed=None)
    with pytest.raises(AliasError):
        w2.add(alias, T0, 1.0)
    w.flush()
    w2.flush()
    assert fake.calls == [] and fake2.calls == []


def test_refuses_derived_in_allowed_set():
    with pytest.raises(AliasError):
        RawWriter(FakeSiteWise(), allowed=[*RAW, F.derived_alias("P-007", "alert_state")])


def test_refuses_alias_outside_model():
    w, fake = writer()
    with pytest.raises(AliasError):
        w.add("/poc-dj/lng1/train1/P-999/flow_in", T0, 1.0)
    assert fake.calls == []


def test_timestamps_strictly_increase_per_alias_across_flushes():
    w, _ = writer()
    a, b = RAW[0], RAW[1]
    w.add(a, T0, 1.0)
    w.add(b, T0, 1.0)  # other aliases are independent
    with pytest.raises(TimestampError):
        w.add(a, T0, 2.0)
    w.flush()
    with pytest.raises(TimestampError):
        w.add(a, T0 - 1, 2.0)
    w.add(a, T0 + 1, 2.0)


def test_batch_limits_and_payload():
    w, fake = writer()
    for t in range(T0, T0 + 15):
        for alias in RAW[:13]:
            w.add(alias, t, 1.5)
    assert w.flush() == 15 * 13
    assert all(1 <= len(call) <= 10 for call in fake.calls)
    assert all(1 <= len(e["propertyValues"]) <= 10 for call in fake.calls for e in call)
    ids = [e["entryId"] for call in fake.calls for e in call]
    assert len(ids) == len(set(ids))
    sent = fake.values()
    assert set(sent) == set(RAW[:13])
    for vals in sent.values():
        assert [v["timestamp"]["timeInSeconds"] for v in vals] == list(range(T0, T0 + 15))
        assert all(v["value"] == {"doubleValue": 1.5} and v["quality"] == "GOOD" for v in vals)
        assert all(v["timestamp"]["offsetInNanos"] == 0 for v in vals)
    assert w.pending == 0 and w.flush() == 0


def test_values_are_not_clamped():
    w, fake = writer()
    w.add(F.alias("P-008", "flow_in"), T0, 1e9)  # far above the contract max
    w.flush()
    assert fake.values()[F.alias("P-008", "flow_in")][0]["value"] == {"doubleValue": 1e9}


def test_retries_throttling_exception():
    w, fake = writer(FakeSiteWise(throttle_first=2))
    w.add(RAW[0], T0, 1.0)
    w.flush()
    assert len(fake.calls) == 3


def test_retries_only_failed_entries_on_retryable_error_entries():
    w, fake = writer(FakeSiteWise(entry_errors=["ThrottlingException"]))
    for alias in RAW[:3]:
        w.add(alias, T0, 1.0)
    w.flush()
    assert [len(c) for c in fake.calls] == [3, 1]


def test_non_retryable_error_raises():
    w, fake = writer(FakeSiteWise(entry_errors=["InvalidRequestException"]))
    w.add(RAW[0], T0, 1.0)
    with pytest.raises(WriteError):
        w.flush()
    assert len(fake.calls) == 1

    class Denied(FakeSiteWise):
        def batch_put_asset_property_value(self, entries):
            raise FakeClientError("AccessDeniedException")

    w, _ = writer(Denied())
    w.add(RAW[0], T0, 1.0)
    with pytest.raises(WriteError):
        w.flush()


def test_gives_up_after_max_attempts():
    w, fake = writer(FakeSiteWise(throttle_first=100), max_attempts=3)
    w.add(RAW[0], T0, 1.0)
    with pytest.raises(WriteError):
        w.flush()
    assert len(fake.calls) == 3 and w.pending == 0


def test_rejects_non_finite_values():
    w, _ = writer()
    with pytest.raises(ValueError):
        w.add(RAW[0], T0, float("nan"))


def test_failed_middle_batch_does_not_drop_later_batches():
    class FailSecond(FakeSiteWise):
        def batch_put_asset_property_value(self, entries):
            self.calls.append(entries)
            if len(self.calls) == 2:
                raise FakeClientError("AccessDeniedException")
            return {"errorEntries": []}

    w, fake = writer(FailSecond())
    for alias in RAW[:30]:  # 30 entries -> 3 requests
        w.add(alias, T0, 1.0)
    with pytest.raises(WriteError) as exc:
        w.flush()
    assert len(fake.calls) == 3
    failed = [e["propertyAlias"] for e in fake.calls[1]]
    assert "10 of 30 values" in str(exc.value) and failed[0] in str(exc.value)
    sent_ok = {e["propertyAlias"] for c in (fake.calls[0], fake.calls[2]) for e in c}
    assert sent_ok == set(RAW[:10]) | set(RAW[20:30])
    assert w.pending == 0


def test_throttled_flush_sleeps_at_most_one_second_in_total():
    slept = []
    fake = FakeSiteWise(throttle_first=1000)
    w = RawWriter(fake, allowed=RAW, sleep=slept.append, max_attempts=100)
    for alias in RAW[:50]:  # 5 requests, all throttled
        w.add(alias, T0, 1.0)
    with pytest.raises(WriteError):
        w.flush()
    assert sum(slept) <= 1.0 + 1e-9
    assert {e["propertyAlias"] for c in fake.calls for e in c} == set(RAW[:50])  # every batch attempted
    # the budget resets on the next flush
    slept.clear()
    fake.throttle_first = 1
    w.add(RAW[0], T0 + 1, 1.0)
    w.flush()
    assert 0 < sum(slept) <= 1.0
