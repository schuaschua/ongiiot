"""Full detection runs with fakes: the "Normal run", "No data" and "Replay" rows (Story 2.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from detect import handler
from detect.adapters.derived_writer import DerivedWriter
from detect.adapters.reader import make_reader
from facility.model import DEFAULT_PATH, load
from tests.detect_fakes import FakeSiteWise

STAGE = "poc-dj"
SCHEDULED = "2026-09-28T12:34:17Z"
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)
START = datetime(2026, 9, 28, 12, 18, tzinfo=UTC)


@pytest.fixture(scope="module")
def facility():
    return load(DEFAULT_PATH, stage=STAGE)


def live_store(facility, lo=START.timestamp() - 120, hi=END.timestamp() + 60):
    return {
        facility.alias(p.id, m): [(t, 1.0, "GOOD") for t in range(int(lo), int(hi))] for p in facility.pipes for m in p.sensors
    }


def runtime(facility, fake, strategy="batch_get"):
    targets = handler.dq_aliases(facility)
    return handler.Runtime(
        facility=facility,
        pipes=handler.pipe_metas(facility),
        dq_targets=targets,
        reader=make_reader(strategy, fake),
        writer=DerivedWriter(fake, allowed=[a for pair in targets.values() for a in pair]),
    )


def run(facility, fake, strategy="batch_get", scheduled=SCHEDULED):
    logs = []
    summary = handler.run(runtime(facility, fake, strategy), handler.parse_scheduled_time({"scheduled_time": scheduled}), log=logs.append)
    return summary, logs


def written(facility, fake):
    """(pipe_id, measurement) -> (dq, dq_fail) at END."""
    t = int(END.timestamp())
    out = {}
    for p in facility.pipes:
        for m in p.sensors:
            dq = fake.stored[(facility.derived_alias(p.id, f"{m}_dq"), t)]
            fail = fake.stored[(facility.derived_alias(p.id, f"{m}_dq_fail"), t)]
            out[(p.id, m)] = (dq["doubleValue"], fail["stringValue"])
    return out


def test_facility_has_144_sensors(facility):
    assert sum(len(p.sensors) for p in facility.pipes) == 144


@pytest.mark.parametrize("strategy", ["batch_get", "execute_query"])
def test_normal_run(facility, strategy):
    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, logs = run(facility, fake, strategy)
    values = written(facility, fake)
    assert len(values) == 144 and set(values.values()) == {(1.0, "")}
    # Only derived aliases are written, all at window_end.
    for call in fake.calls:
        for e in call:
            assert "/derived/" in e["propertyAlias"]
            assert e["propertyValues"][0]["timestamp"] == {"timeInSeconds": int(END.timestamp()), "offsetInNanos": 0}
    assert all(len(c) <= 10 for c in fake.calls)
    (line,) = logs
    assert json.loads(line) == summary
    assert summary["run_id"] == "2026-09-28T12:34:17.000Z"
    assert summary["window_start"] == "2026-09-28T12:18:00.000Z"
    assert summary["window_end"] == "2026-09-28T12:33:00.000Z"
    assert summary["stage"] == STAGE and summary["config_hash"] == facility.config_hash()
    assert summary["reader_strategy"] == strategy
    assert summary["aliases_no_data"] == [] and summary["values_written"] == 288
    assert summary["samples"] == 144 * 900
    assert {"read_duration_s", "detectors", "ran_ok"} <= set(summary)


def test_no_data(facility):
    fake = FakeSiteWise({})
    summary, _ = run(facility, fake)
    assert set(written(facility, fake).values()) == {(0.0, "dq_gap")}
    assert len(summary["aliases_no_data"]) == 144 and summary["values_written"] == 288


def test_partial_data_scores_only_silent_sensors_zero(facility):
    store = live_store(facility)
    silent = facility.alias("P-007", "flow_in")
    store[silent] = [(END.timestamp() + 5, 1.0, "GOOD")]  # data only after window_end (ingest lag)
    fake = FakeSiteWise(store, page=20000)
    summary, _ = run(facility, fake)
    values = written(facility, fake)
    assert values[("P-007", "flow_in")] == (0.0, "dq_gap")
    assert sum(v == (1.0, "") for v in values.values()) == 143
    assert summary["aliases_no_data"] == [silent]


def test_replay_writes_identical_values(facility):
    fake = FakeSiteWise(live_store(facility), page=20000)
    run(facility, fake)
    first = [c for c in fake.calls]
    before = dict(fake.stored)
    run(facility, fake)
    assert fake.calls[len(first) :] == first
    assert fake.stored == before


def test_detector_failure_is_logged_and_skipped(facility):
    class Boom:
        id, version = "dq_boom", "1"

        def detect(self, pw, t):
            raise RuntimeError("boom")

    fake = FakeSiteWise(live_store(facility), page=20000)
    logs = []
    rt = runtime(facility, fake)
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), detectors=[Boom()], log=logs.append)
    assert summary["ran_ok"] == [] and summary["detectors"] == {"dq_boom": "1"}
    assert json.loads(logs[0])["detector_id"] == "dq_boom"
    assert set(written(facility, fake).values()) == {(1.0, "")}


def test_adapter_failure_fails_the_invocation(facility):
    fake = FakeSiteWise({})
    fake.fail = "ThrottlingException"
    with pytest.raises(Exception, match="ThrottlingException"):
        run(facility, fake)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-28T12:34:17Z", datetime(2026, 9, 28, 12, 34, 17, tzinfo=UTC)),
        ("2026-09-28T20:34:17+08:00", datetime(2026, 9, 28, 12, 34, 17, tzinfo=UTC)),
    ],
)
def test_parse_scheduled_time(raw, expected):
    assert handler.parse_scheduled_time({"scheduled_time": raw}) == expected


@pytest.mark.parametrize("event", [{}, {"scheduled_time": ""}, {"scheduled_time": "2026-09-28T12:34:17"}, None, {"action": "reset"}])
def test_bad_events_are_refused(event):
    with pytest.raises(ValueError):
        handler.parse_scheduled_time(event)


def test_handler_entry_point(facility, monkeypatch):
    fake = FakeSiteWise(live_store(facility), page=20000)
    monkeypatch.setattr(handler, "_RUNTIME", None)
    rt = handler.runtime({"STAGE": STAGE}, client=fake)
    assert rt.reader.strategy == handler.DEFAULT_READER_STRATEGY == "batch_get"
    assert handler.runtime({"STAGE": "poc"}) is rt  # cached per cold start
    summary = handler.handler({"scheduled_time": SCHEDULED})
    assert summary["values_written"] == 288
    monkeypatch.setattr(handler, "_RUNTIME", None)
    assert handler.runtime({"STAGE": STAGE, "READER_STRATEGY": "execute_query"}, client=fake).reader.strategy == "execute_query"
    monkeypatch.setattr(handler, "_RUNTIME", None)
    with pytest.raises(ValueError, match="STAGE"):
        handler.runtime({}, client=fake)
    monkeypatch.setattr(handler, "_RUNTIME", None)


def test_pipe_metas_match_the_model(facility):
    metas = handler.pipe_metas(facility)
    p = facility.pipe("P-007")
    m = metas["P-007"]
    assert (m.area, m.service, m.material, m.age_years, m.peer_group) == (p.area, p.service, p.material, p.age_years, p.peer_group)
    assert dict(m.aliases) == {s: facility.alias("P-007", s) for s in p.sensors}
    assert m.contracts["flow_in"].max == p.contracts["flow_in"].max
