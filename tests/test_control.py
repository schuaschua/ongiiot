"""sim_control item v1: validation, DynamoDB conversion, windows and supersedes (AD-15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from facility.control import (
    ControlError,
    cancel_of,
    effective,
    from_item,
    new_injection,
    windows,
    to_item,
)
from facility.model import load

F = load()
T0 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)


def leak(iid="A", start=T0, minutes=10, **params):
    return new_injection(iid, "leak", "P-007", start, start + timedelta(minutes=minutes), params=params, facility=F)


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


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"type_": "burst"}, "unknown injection type"), ({"type_": "dq_stuck"}, "requires a measurement")],
)
def test_invalid_injections(kwargs, match):
    base = {"injection_id": "X", "type_": "leak", "pipe_id": "P-007", "start_at": T0, "end_at": T0 + timedelta(minutes=1)}
    with pytest.raises(ControlError, match=match):
        new_injection(**(base | kwargs), facility=F)


def test_window_is_half_open():
    inj = leak(minutes=1)
    assert effective([inj], T0 - timedelta(seconds=1)) == []
    assert effective([inj], T0) == [inj]
    assert effective([inj], T0 + timedelta(seconds=59)) == [inj]
    assert effective([inj], T0 + timedelta(minutes=1)) == []


def test_cancel_of_a_cancel_is_refused():
    c = cancel_of(leak(), "C", T0)
    with pytest.raises(ControlError, match="itself a cancel"):
        cancel_of(c, "D", T0)


def test_windows_fold_cancels_and_keep_planned_end():
    inj = leak(minutes=10)
    other = leak(iid="B", minutes=5)
    c = cancel_of(inj, "C", T0 + timedelta(minutes=2))
    got = {i.injection_id: (i.end_at, planned) for i, planned in windows([inj, c, other])}
    assert got == {
        "A": (T0 + timedelta(minutes=2), T0 + timedelta(minutes=10)),
        "B": (T0 + timedelta(minutes=5), T0 + timedelta(minutes=5)),
    }
