"""Raw writer: alias guard, strictly increasing timestamps, batch limits, retries (AD-1, AD-3, AD-17)."""

from __future__ import annotations

import pytest

from facility.model import load
from sim.writer import AliasError, RawWriter, TimestampError
from tests.sim_fakes import FakeSiteWise

F = load(stage="poc-dj")
RAW = [F.alias(p.id, m) for p in F.pipes for m in p.sensors]
T0 = 1_790_000_000


def writer(fake=None, allowed=RAW, **kw):
    fake = fake or FakeSiteWise()
    return RawWriter(fake, allowed=allowed, sleep=lambda s: None, **kw), fake


@pytest.mark.parametrize(
    "alias",
    [F.derived_alias("P-007", "alert_state")],
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


def test_retries_only_failed_entries_on_retryable_error_entries():
    w, fake = writer(FakeSiteWise(entry_errors=["ThrottlingException"]))
    for alias in RAW[:3]:
        w.add(alias, T0, 1.0)
    w.flush()
    assert [len(c) for c in fake.calls] == [3, 1]
