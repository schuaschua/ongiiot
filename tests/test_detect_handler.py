"""Full detection runs with fakes: the "Normal run", "No data" and "Replay" rows (Story 2.1),
railed and stuck sensors end to end (Story 2.2), and drifting and silent sensors end to end (Story 2.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from detect import handler
from detect.adapters.derived_writer import DerivedWriter
from detect.adapters.reader import make_reader
from detect.core.models import Finding
from facility.model import DEFAULT_PATH, load
from tests.detect_fakes import FakeSiteWise

STAGE = "poc-dj"
SCHEDULED = "2026-09-28T12:34:17Z"
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)
START = datetime(2026, 9, 28, 12, 18, tzinfo=UTC)


@pytest.fixture(scope="module")
def facility():
    return load(DEFAULT_PATH, stage=STAGE)


def live_value(contract, t):
    """Healthy synthetic reading: varies every second, mid-range of the contract (off both rails)."""
    return (contract.min + contract.max) / 2 + (t % 10) * (contract.max - contract.min) * 1e-4


def live_store(facility, lo=START.timestamp() - 120, hi=END.timestamp() + 60):
    return {
        facility.alias(p.id, m): [(t, live_value(p.contracts[m], t), "GOOD") for t in range(int(lo), int(hi))]
        for p in facility.pipes
        for m in p.sensors
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
    assert {"read_duration_s", "detectors", "ran_ok", "run_s"} <= set(summary)


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
        id, version, kind = "dq_boom", "1", "pipe"

        def detect(self, pw, t):
            raise RuntimeError("boom")

    fake = FakeSiteWise(live_store(facility), page=20000)
    logs = []
    rt = runtime(facility, fake)
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), detectors=[Boom()], log=logs.append)
    assert summary["ran_ok"] == [] and summary["detectors"] == {"dq_boom": "1"}
    assert json.loads(logs[0])["detector_id"] == "dq_boom"
    assert set(written(facility, fake).values()) == {(1.0, "")}


class DqOn:
    """Fake pass-1 detector: a dq_* finding with confidence ``c`` on one (pipe, measurement)."""

    id, version, kind = "dq_fake", "1", "pipe"

    def __init__(self, facility, pipe_id, measurement, c):
        self.alias = facility.alias(pipe_id, measurement)
        self.pipe_id, self.c = pipe_id, c
        self.seen: dict[str, set[str]] = {}

    def detect(self, pw, t):
        self.seen[pw.pipe.pipe_id] = set(pw.window.series)
        if pw.pipe.pipe_id != self.pipe_id:
            return []
        return [Finding(self.id, self.version, self.pipe_id, (self.alias,), pw.window.start, pw.window.end, self.c, "x.")]


def run_with(facility, fake, detectors):
    logs = []
    rt = runtime(facility, fake)
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), detectors=detectors, log=logs.append)
    return summary, logs


def test_detector_findings_reach_written_scores(facility):
    det = DqOn(facility, "P-007", "flow_in", 0.3)
    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, _ = run_with(facility, fake, [det])
    values = written(facility, fake)
    assert values[("P-007", "flow_in")] == (pytest.approx(0.7), "dq_fake")
    assert sum(v == (1.0, "") for v in values.values()) == 143
    assert summary["ran_ok"] == ["dq_fake"]
    # Each pipe sees only its own aliases.
    assert det.seen == {p.id: {facility.alias(p.id, m) for m in p.sensors} for p in facility.pipes}


def test_detector_failing_on_a_later_pipe_contributes_nothing(facility):
    last = facility.pipes[-1].id

    class LateBoom(DqOn):
        def detect(self, pw, t):
            if pw.pipe.pipe_id == last:
                raise RuntimeError("boom")
            return super().detect(pw, t)

    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, logs = run_with(facility, fake, [LateBoom(facility, facility.pipes[0].id, facility.pipes[0].sensors[0], 0.9)])
    assert set(written(facility, fake).values()) == {(1.0, "")}
    assert summary["ran_ok"] == [] and json.loads(logs[0])["detector_id"] == "dq_fake"


def test_malformed_dq_finding_skips_only_that_detector(facility):
    p0, p1 = facility.pipes[0], facility.pipes[1]

    class Foreign(DqOn):
        def detect(self, pw, t):  # names another pipe's alias
            return [Finding(self.id, "1", pw.pipe.pipe_id, (facility.alias(p1.id, p1.sensors[0]),), pw.window.start, pw.window.end, 0.5, "x.")] if pw.pipe.pipe_id == p0.id else []

    good = DqOn(facility, p0.id, p0.sensors[0], 0.25)
    good.id = "dq_good"
    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, logs = run_with(facility, fake, [Foreign(facility, p0.id, p0.sensors[0], 0.5), good])
    assert summary["ran_ok"] == ["dq_good"]
    values = written(facility, fake)
    assert values[(p0.id, p0.sensors[0])] == (pytest.approx(0.75), "dq_good")
    assert values[(p1.id, p1.sensors[0])] == (1.0, "")
    assert "not an expected alias" in json.loads(logs[0])["error"]


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
    assert rt.reader.strategy == handler.DEFAULT_STRATEGY == "batch_get"
    assert handler.runtime({"STAGE": "poc"}) is rt  # cached per cold start
    summary = handler.handler({"scheduled_time": SCHEDULED})
    assert summary["values_written"] == 288
    monkeypatch.setattr(handler, "_RUNTIME", None)
    assert handler.runtime({"STAGE": STAGE, "READER_STRATEGY": "execute_query"}, client=fake).reader.strategy == "execute_query"
    monkeypatch.setattr(handler, "_RUNTIME", None)
    with pytest.raises(ValueError, match="STAGE"):
        handler.runtime({}, client=fake)
    monkeypatch.setattr(handler, "_RUNTIME", None)


def test_write_client_has_no_sdk_retries(monkeypatch):
    import boto3

    made = []
    monkeypatch.setattr(boto3, "client", lambda service, config=None, **kw: made.append(config) or object())
    monkeypatch.setattr(handler, "_RUNTIME", None)
    rt = handler.runtime({"STAGE": STAGE})
    monkeypatch.setattr(handler, "_RUNTIME", None)
    attempts = [c.retries["max_attempts"] for c in made]
    assert sorted(attempts) == [1, 3]
    write_config = made[attempts.index(1)]
    assert rt.writer._client is not rt.reader._client and write_config.retries["mode"] == "standard"


def test_pipe_metas_match_the_model(facility):
    metas = handler.pipe_metas(facility)
    p = facility.pipe("P-007")
    m = metas["P-007"]
    assert (m.area, m.service, m.material, m.age_years, m.peer_group) == (p.area, p.service, p.material, p.age_years, p.peer_group)
    assert dict(m.aliases) == {s: facility.alias("P-007", s) for s in p.sensors}
    assert m.contracts["flow_in"].max == p.contracts["flow_in"].max


# -- Story 2.2: dq_flatline and dq_stuck registered in pass 1 ------------------------------------


def test_pass1_registers_the_dq_detectors():
    assert [(d.id, d.kind) for d in handler.PASS1_DETECTORS] == [
        ("dq_flatline", "pipe"),
        ("dq_stuck", "pipe"),
        ("dq_drift", "facility"),
        ("dq_gap", "pipe"),
    ]
    from detect.core.models import FacilityDetector, PipeDetector

    for d in handler.PASS1_DETECTORS:  # structural: id, version, kind, detect
        assert isinstance(d, FacilityDetector if d.kind == "facility" else PipeDetector)


def test_railed_and_stuck_sensors_end_to_end(facility):
    """P-003 flow_in railed at its contract min and P-015 temperature frozen mid-range, both for the
    last 120 s of the window: `_dq` 0.0 with the named check; the other 142 sensors stay 1.0 / ""."""
    store = live_store(facility)
    fault_from = int(END.timestamp()) - 120
    railed, stuck = facility.alias("P-003", "flow_in"), facility.alias("P-015", "temperature")
    store[railed] = [(t, facility.pipe("P-003").contracts["flow_in"].min if t >= fault_from else v, q) for t, v, q in store[railed]]
    store[stuck] = [(t, 251.37 if t >= fault_from else v, q) for t, v, q in store[stuck]]
    fake = FakeSiteWise(store, page=20000)
    summary, logs = run(facility, fake)
    values = written(facility, fake)
    assert values[("P-003", "flow_in")] == (0.0, "dq_flatline")
    assert values[("P-015", "temperature")] == (0.0, "dq_stuck")
    others = {k: v for k, v in values.items() if k not in {("P-003", "flow_in"), ("P-015", "temperature")}}
    assert len(others) == 142 and set(others.values()) == {(1.0, "")}
    assert summary["ran_ok"] == ["dq_flatline", "dq_stuck", "dq_drift", "dq_gap"]
    assert summary["detectors"] == {"dq_flatline": "1", "dq_stuck": "1", "dq_drift": "1", "dq_gap": "1"}
    assert len(logs) == 1  # no detector errors


def test_facility_detectors_run_once_per_run(facility):
    p = facility.pipes[0]

    class Whole:
        id, version, kind = "dq_whole", "1", "facility"
        calls = 0

        def detect(self, fw, t):
            Whole.calls += 1
            assert set(fw.pipes) == {q.id for q in facility.pipes}
            alias = fw.pipes[p.id].aliases[p.sensors[0]]
            return [Finding(self.id, self.version, p.id, (alias,), fw.window.start, fw.window.end, 0.4, "x.")]

    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, _ = run_with(facility, fake, [Whole()])
    assert Whole.calls == 1 and summary["ran_ok"] == ["dq_whole"]
    assert written(facility, fake)[(p.id, p.sensors[0])] == (pytest.approx(0.6), "dq_whole")


def test_facility_detector_naming_another_pipes_alias_is_skipped(facility):
    p0, p1 = facility.pipes[0], facility.pipes[1]

    class Crossed:
        id, version, kind = "dq_crossed", "1", "facility"

        def detect(self, fw, t):
            alias = fw.pipes[p1.id].aliases[p1.sensors[0]]
            return [Finding(self.id, self.version, p0.id, (alias,), fw.window.start, fw.window.end, 0.4, "x.")]

    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, logs = run_with(facility, fake, [Crossed()])
    assert summary["ran_ok"] == [] and "not an expected alias" in json.loads(logs[0])["error"]
    assert set(written(facility, fake).values()) == {(1.0, "")}


def test_unknown_kind_is_logged_and_skipped(facility):
    class Odd(DqOn):
        kind = "sensor"

    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, logs = run_with(facility, fake, [Odd(facility, "P-007", "flow_in", 0.9)])
    assert summary["ran_ok"] == [] and "unknown detector kind" in json.loads(logs[0])["error"]
    with pytest.raises(ValueError, match="kind"):
        handler.check_detectors([Odd(facility, "P-007", "flow_in", 0.9)], facility.tunables)


def _without(tunables, detector_id, key):
    detectors = {k: dict(v) for k, v in tunables["detectors"].items()}
    if key is None:
        del detectors[detector_id]
    else:
        del detectors[detector_id][key]
    return {**tunables, "detectors": detectors}


@pytest.mark.parametrize(
    ("detector_id", "key"),
    [
        ("dq_flatline", "min_run_s"),
        ("dq_flatline", "full_s"),
        ("dq_flatline", "rel_tol"),
        ("dq_stuck", "min_run_s"),
        ("dq_stuck", "full_s"),
        ("dq_stuck", None),
        ("dq_drift", "fit_s"),
        ("dq_drift", "min_points"),
        ("dq_drift", "min_rate"),
        ("dq_drift", "full_rate"),
        ("dq_drift", "min_r2"),
        ("dq_drift", None),
        ("dq_gap", "min_gap_s"),
        ("dq_gap", "full_gap_s"),
        ("dq_gap", None),
    ],
)
def test_missing_detector_tunables_fail_the_cold_start(facility, monkeypatch, detector_id, key):
    import dataclasses

    broken = dataclasses.replace(facility, tunables=_without(facility.tunables, detector_id, key))
    monkeypatch.setattr(handler, "load", lambda path, stage: broken)
    monkeypatch.setattr(handler, "_RUNTIME", None)
    with pytest.raises(ValueError, match=detector_id):
        handler.runtime({"STAGE": STAGE}, client=FakeSiteWise({}))
    assert handler._RUNTIME is None


@pytest.mark.parametrize(
    ("detector_id", "patch"),
    [
        ("dq_flatline", {"rel_tol": -1.0}),
        ("dq_flatline", {"full_s": 0}),
        ("dq_stuck", {"min_run_s": 90}),  # above full_s
        ("dq_stuck", {"full_s": "60"}),
        ("dq_stuck", {"min_run_s": True}),
        ("dq_flatline", {"rel_tol": 0.5}),  # would rail every in-range value
        ("dq_stuck", {"full_s": 900}),  # must be < window_len_s
        ("dq_flatline", {"min_run_s": 900, "full_s": 1000}),
        ("dq_drift", {"fit_s": 0}),
        ("dq_drift", {"fit_s": 901}),  # longer than the window
        ("dq_drift", {"min_points": 2}),
        ("dq_drift", {"min_rate": 2e-3}),  # above full_rate
        ("dq_drift", {"min_rate": -1e-4}),
        ("dq_drift", {"min_rate": 0, "full_rate": 0}),
        ("dq_drift", {"min_r2": 1.5}),
        ("dq_drift", {"min_r2": "0.9"}),
        ("dq_gap", {"min_gap_s": 90}),  # above full_gap_s
        ("dq_gap", {"min_gap_s": -1}),
        ("dq_gap", {"full_gap_s": 0, "min_gap_s": 0}),
        ("dq_gap", {"full_gap_s": 901}),
        ("dq_gap", {"full_gap_s": float("nan")}),
    ],
)
def test_invalid_detector_tunables_are_refused(facility, detector_id, patch):
    detectors = {k: dict(v) for k, v in facility.tunables["detectors"].items()}
    detectors[detector_id].update(patch)
    with pytest.raises(ValueError, match=detector_id):
        handler.check_detectors(handler.PASS1_DETECTORS, {**facility.tunables, "detectors": detectors})


def test_bundled_tunables_pass_the_cold_start_check(facility):
    handler.check_detectors(handler.PASS1_DETECTORS, facility.tunables)
    assert dict(facility.tunables["detectors"]["dq_flatline"]) == {"min_run_s": 30, "full_s": 60, "rel_tol": 1.0e-6}
    assert dict(facility.tunables["detectors"]["dq_stuck"]) == {"min_run_s": 30, "full_s": 60}
    assert dict(facility.tunables["detectors"]["dq_drift"]) == {
        "fit_s": 300,
        "min_points": 150,
        "min_rate": 5.0e-4,
        "full_rate": 1.0e-3,
        "min_r2": 0.97,
    }
    assert dict(facility.tunables["detectors"]["dq_gap"]) == {"min_gap_s": 30, "full_gap_s": 60}


def test_dq_stuck_cold_start_needs_dq_flatline_rel_tol(facility):
    """dq_stuck reads dq_flatline's rel_tol (one definition of railed), so it validates it too."""
    from detect.core.detectors import DqStuck

    with pytest.raises(ValueError, match="dq_flatline"):
        handler.check_detectors([DqStuck()], _without(facility.tunables, "dq_flatline", "rel_tol"))


def test_duplicate_detector_ids_fail_the_cold_start(facility):
    from detect.core.detectors import DqStuck

    with pytest.raises(ValueError, match="duplicate detector ids.*dq_stuck"):
        handler.check_detectors([*handler.PASS1_DETECTORS, DqStuck()], facility.tunables)


def test_pipe_detector_finding_for_another_pipe_is_skipped(facility):
    p0, p1 = facility.pipes[0], facility.pipes[1]

    class Misfiled(DqOn):
        def detect(self, pw, t):  # this pipe's alias, another pipe's id
            if pw.pipe.pipe_id != p0.id:
                return []
            return [Finding(self.id, "1", p1.id, (pw.pipe.aliases[p0.sensors[0]],), pw.window.start, pw.window.end, 0.9, "x.")]

    fake = FakeSiteWise(live_store(facility), page=20000)
    summary, logs = run_with(facility, fake, [Misfiled(facility, p0.id, p0.sensors[0], 0.9)])
    assert summary["ran_ok"] == []
    err = json.loads(logs[0])
    assert err["detector_id"] == "dq_fake" and p1.id in err["error"]
    assert set(written(facility, fake).values()) == {(1.0, "")}


# -- Story 2.3: dq_drift and dq_gap ----------------------------------------------------------------


def test_drifting_and_silent_sensors_end_to_end(facility):
    """P-010 temperature drifts 0.5 K/min for the last 6 min of the window and P-020 flow_out is silent
    for the last 3 min: both score `_dq` 0.0 with the named check; the other 142 sensors stay 1.0 / ""."""
    store = live_store(facility)
    end = int(END.timestamp())
    drift_from, gap_from = end - 360, end - 180
    drifting, silent = facility.alias("P-010", "temperature"), facility.alias("P-020", "flow_out")
    store[drifting] = [(t, v + 0.5 * max(0, t - drift_from) / 60, q) for t, v, q in store[drifting]]
    store[silent] = [(t, v, q) for t, v, q in store[silent] if not gap_from <= t < end + 60]
    fake = FakeSiteWise(store, page=20000)
    summary, logs = run(facility, fake)
    values = written(facility, fake)
    assert values[("P-010", "temperature")] == (0.0, "dq_drift")
    assert values[("P-020", "flow_out")] == (0.0, "dq_gap")
    others = {k: v for k, v in values.items() if k not in {("P-010", "temperature"), ("P-020", "flow_out")}}
    assert len(others) == 142 and set(others.values()) == {(1.0, "")}
    assert summary["ran_ok"] == ["dq_flatline", "dq_stuck", "dq_drift", "dq_gap"] and len(logs) == 1


def test_empty_alias_gets_one_dq_gap_even_if_the_detector_fails(facility):
    """The no-sample rule stays the fallback: a failed dq_gap detector still leaves 0.0 / dq_gap."""

    class GapBoom:
        id, version, kind = "dq_gap", "1", "pipe"

        def detect(self, pw, t):
            raise RuntimeError("boom")

    store = live_store(facility)
    silent = facility.alias("P-007", "flow_in")
    store[silent] = []
    fake = FakeSiteWise(store, page=20000)
    summary, _ = run_with(facility, fake, [GapBoom()])
    assert summary["ran_ok"] == []
    values = written(facility, fake)
    assert values[("P-007", "flow_in")] == (0.0, "dq_gap")
    assert sum(v == (1.0, "") for v in values.values()) == 143
