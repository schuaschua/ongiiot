"""sim_control item v1: validation, DynamoDB conversion, windows and supersedes (AD-15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from facility.control import (
    ControlError,
    cancel_of,
    effective,
    format_ts,
    from_item,
    new_injection,
    parse_ts,
    resolve,
    windows,
    to_item,
)
from facility.model import load

F = load()
T0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)


def leak(iid="A", start=T0, minutes=10, **params):
    return new_injection(iid, "leak", "P-007", start, start + timedelta(minutes=minutes), params=params, facility=F)


def test_timestamp_format_round_trip():
    s = format_ts(datetime(2026, 9, 28, 10, 0, 1, 234567, tzinfo=UTC))
    assert s == "2026-09-28T10:00:01.234Z"
    assert parse_ts(s) == datetime(2026, 9, 28, 10, 0, 1, 234000, tzinfo=UTC)
    with pytest.raises(ControlError):
        parse_ts("2026-09-28T10:00:01+00:00")


def test_item_round_trip():
    inj = new_injection(
        "01J", "dq_drift", "P-003", T0, T0 + timedelta(minutes=5), measurement="temperature", params={"per_min": 0.5}, facility=F
    )
    item = to_item(inj)
    assert item == {
        "pk": "01J",
        "type": "dq_drift",
        "pipe_id": "P-003",
        "measurement": "temperature",
        "params": {"per_min": Decimal("0.5")},
        "start_at": "2026-09-28T10:00:00.000Z",
        "end_at": "2026-09-28T10:05:00.000Z",
    }
    assert from_item(item, F) == inj


def test_leak_item_has_no_measurement_and_optional_rate():
    item = to_item(leak())
    assert "measurement" not in item and item["params"] == {}
    assert from_item(to_item(leak(rate_kg_s=2)), F).params == {"rate_kg_s": 2.0}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"type_": "burst"}, "unknown injection type"),
        ({"type_": "dq_stuck"}, "requires a measurement"),
        ({"pipe_id": "P-999"}, "unknown pipe"),
        ({"measurement": "flow_in"}, "takes no measurement"),
        ({"params": {"per_min": 1}}, "unknown param"),
        ({"params": {"rate_kg_s": -1}}, "rate_kg_s must be > 0"),
        ({"params": {"rate_kg_s": float("nan")}}, "finite"),
        ({"type_": "dq_gap", "measurement": "viscosity"}, "no sensor"),
        ({"type_": "dq_drift", "measurement": "flow_in", "params": {"per_min": 0}}, "non-zero"),
        ({"end_at": T0}, "start_at must be before end_at"),
    ],
)
def test_invalid_injections(kwargs, match):
    base = {"injection_id": "X", "type_": "leak", "pipe_id": "P-007", "start_at": T0, "end_at": T0 + timedelta(minutes=1)}
    with pytest.raises(ControlError, match=match):
        new_injection(**(base | kwargs), facility=F)


def test_from_item_rejects_unknown_and_missing_fields():
    item = to_item(leak())
    with pytest.raises(ControlError, match="unknown item fields"):
        from_item(item | {"extra": 1}, F)
    del item["start_at"]
    with pytest.raises(ControlError, match="missing"):
        from_item(item, F)


def test_window_is_half_open():
    inj = leak(minutes=1)
    assert effective([inj], T0 - timedelta(seconds=1)) == []
    assert effective([inj], T0) == [inj]
    assert effective([inj], T0 + timedelta(seconds=59)) == [inj]
    assert effective([inj], T0 + timedelta(minutes=1)) == []


def test_cancel_ends_target_at_cancellers_end_at():
    inj = leak(minutes=10)
    stop = T0 + timedelta(minutes=2)
    c = cancel_of(inj, "C", stop)
    assert c.supersedes == "A" and c.start_at == c.end_at == stop
    assert effective([inj, c], T0 + timedelta(minutes=1)) == [resolve([inj, c])[0]]
    assert effective([inj, c], stop) == []
    assert effective([inj, c], T0 + timedelta(minutes=5)) == []
    assert {i.injection_id: i.end_at for i in resolve([inj, c])}["A"] == stop


def test_cancel_before_start_prevents_apply_and_earliest_canceller_wins():
    inj = leak(start=T0 + timedelta(minutes=5))
    c1 = cancel_of(inj, "C1", T0 + timedelta(minutes=8))
    c2 = cancel_of(inj, "C2", T0)
    assert all(effective([inj, c1, c2], T0 + timedelta(minutes=m)) == [] for m in range(20))


def test_cancel_of_a_cancel_is_refused():
    c = cancel_of(leak(), "C", T0)
    with pytest.raises(ControlError, match="itself a cancel"):
        cancel_of(c, "D", T0)


def test_late_cancel_never_extends_the_window():
    inj = leak(minutes=10)
    late = cancel_of(inj, "C", T0 + timedelta(minutes=30))
    assert {i.injection_id: i.end_at for i in resolve([inj, late])}["A"] == T0 + timedelta(minutes=10)
    assert effective([inj, late], T0 + timedelta(minutes=15)) == []


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("type", ["leak"], "unknown injection type"),
        ("type", {"S": "leak"}, "unknown injection type"),
        ("supersedes", ["A"], "supersedes"),
        ("supersedes", {"S": "A"}, "supersedes"),
        ("supersedes", "", "supersedes"),
        ("measurement", ["flow_in"], "measurement"),
    ],
)
def test_malformed_dynamodb_fields_raise_control_error(field, value, match):
    item = to_item(leak()) | {field: value}
    with pytest.raises(ControlError, match=match):
        from_item(item, F)
    with pytest.raises(ControlError, match=match):
        from_item(item)  # also without the facility model


def test_windows_fold_cancels_and_keep_planned_end():
    inj = leak(minutes=10)
    other = leak(iid="B", minutes=5)
    c = cancel_of(inj, "C", T0 + timedelta(minutes=2))
    got = {i.injection_id: (i.end_at, planned) for i, planned in windows([inj, c, other])}
    assert got == {
        "A": (T0 + timedelta(minutes=2), T0 + timedelta(minutes=10)),
        "B": (T0 + timedelta(minutes=5), T0 + timedelta(minutes=5)),
    }
