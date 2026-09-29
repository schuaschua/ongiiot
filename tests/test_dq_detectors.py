"""``dq_*`` detectors on synthetic windows and on real simulator physics.

Story 2.2 (``dq_flatline``, ``dq_stuck``) rows: Railed, Frozen, Short run, Partial, Recovered, Normal, Others.
Story 2.3 (``dq_drift``, ``dq_gap``) rows: Drift, Shared change, Too few peers, Silence, Old gap, Empty
window, Ingest lag, Normal, plus the calibration targets. The Live rows are deployed checks.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from detect import handler
from detect.core import dq_score
from detect.core.detectors import PASS1, DqDrift, DqFlatline, DqGap, DqStuck
from detect.core.models import FacilityWindow, PipeWindow, Sample, Window
from facility.control import new_injection
from facility.model import load
from sim import physics

F = load(stage="poc-dj")
T = F.tunables
METAS = handler.pipe_metas(F)
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)
START = END - timedelta(seconds=T["window_len_s"])
FLAT, STUCK = DqFlatline(), DqStuck()


def healthy(i: int, m: str = "temperature") -> float:
    """Varies every second, mid-range of the measurement's contract (far from both rails)."""
    c = METAS["P-003"].contracts[m]
    return (c.min + c.max) / 2 + (i % 10) * (c.max - c.min) * 1e-4


def series(values, quality="GOOD", end=END):
    """Ascending 1 Hz samples whose last one is at ``end - 1 s``."""
    n = len(values)
    return [Sample(end - timedelta(seconds=n - i), v, quality) for i, v in enumerate(values)]


def window(pipe_id="P-003", overrides=None):
    """A pipe window of healthy data, with ``{measurement: [Sample]}`` overrides."""
    pipe = METAS[pipe_id]
    data = {pipe.aliases[m]: series([healthy(i, m) for i in range(900)]) for m in pipe.aliases}
    for m, s in (overrides or {}).items():
        data[pipe.aliases[m]] = s
    return PipeWindow(pipe=pipe, window=Window(start=START, end=END, series=data))


def tail(values_before, value, run_n, m="temperature"):
    """Healthy history of ``m``, then ``run_n`` samples of ``value`` at the end."""
    return series([healthy(i, m) for i in range(values_before)] + [value] * run_n)


def findings(pw):
    return FLAT.detect(pw, T) + STUCK.detect(pw, T)


def dq_of(pw, found):
    by_alias = {a: [f for f in found if f.evidence_aliases == (a,)] for a in pw.pipe.aliases.values()}
    return {m: dq_score.score(by_alias[a]) for m, a in pw.pipe.aliases.items()}


# -- matrix rows --------------------------------------------------------------------------------


def test_railed():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(overrides={"flow_in": tail(800, lo, 61, m="flow_in")})  # 61 samples span 60 s
    found = findings(pw)
    assert [(f.detector_id, f.evidence_aliases, f.confidence) for f in found] == [
        ("dq_flatline", (pw.pipe.aliases["flow_in"],), 1.0)
    ]
    assert dq_of(pw, found)["flow_in"] == (0.0, "dq_flatline")


def test_detectors_declare_id_version_kind():
    assert [(d.id, d.version, d.kind) for d in PASS1] == [
        ("dq_flatline", "1", "pipe"),
        ("dq_stuck", "1", "pipe"),
        ("dq_drift", "1", "facility"),
        ("dq_gap", "1", "pipe"),
    ]


def test_detectors_import_types_only_from_models():
    """AD-5: no I/O modules; types come from detect/core/models.py (helpers from the detectors package)."""
    root = Path(__file__).resolve().parent.parent / "detect" / "core" / "detectors"
    allowed_std = {"__future__", "math", "collections", "typing"}
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                if name.startswith("detect."):
                    assert name == "detect.core.models" or name.startswith("detect.core.detectors"), (path.name, name)
                else:
                    assert name.split(".")[0] in allowed_std, (path.name, name)


# -- real simulator physics ---------------------------------------------------------------------


def physics_window(pipe_id, t_end, injections=(), seed=0):
    """A 15-minute PipeWindow of real simulator output ending just before ``t_end`` (epoch s)."""
    pipe = F.pipe(pipe_id)
    state = physics.initial_state(pipe, seed)
    acts = [physics.activate(state, i) for i in injections]
    t0 = t_end - int(T["window_len_s"]) - 120  # warm up the AR state a little before the window
    meta = METAS[pipe_id]
    data = {a: [] for a in meta.aliases.values()}
    for t in range(t0, t_end):
        now = datetime.fromtimestamp(t, UTC)
        live = [a for a, i in zip(acts, injections) if i.active_at(now)]
        state, samples = physics.step(state, t, live)
        if t < t_end - T["window_len_s"]:
            continue
        for s in samples:
            data[meta.aliases[s.measurement]].append(Sample(now, s.value, s.quality))
    end = datetime.fromtimestamp(t_end, UTC)
    return PipeWindow(pipe=meta, window=Window(start=end - timedelta(seconds=T["window_len_s"]), end=end, series=data))


@pytest.mark.parametrize("t_end", [int(END.timestamp())])
def test_normal_simulator_output_has_no_findings(t_end):
    windows = [physics_window(p.id, t_end) for p in F.pipes]
    assert sum(len(w.pipe.aliases) for w in windows) == 144
    for pw in windows:
        assert all(len(s) == T["window_len_s"] for s in pw.window.series.values())
        assert findings(pw) == [], pw.pipe.pipe_id


def _inj(type_, pipe_id, measurement, start):
    s = datetime.fromtimestamp(start, UTC)
    return new_injection(f"{type_}-{pipe_id}", type_, pipe_id, s, s + timedelta(minutes=10), measurement=measurement, params={}, facility=F)


def test_simulator_faults_are_caught_after_one_minute():
    """The simulator's own dq_flatline / dq_stuck effects, 70 s before the window end."""
    t_end = int(END.timestamp())
    start = t_end - 70
    railed = physics_window("P-003", t_end, [_inj("dq_flatline", "P-003", "flow_in", start)])
    frozen = physics_window("P-015", t_end, [_inj("dq_stuck", "P-015", "temperature", start)])
    (f,) = findings(railed)
    assert (f.detector_id, f.evidence_aliases, f.confidence) == ("dq_flatline", (railed.pipe.aliases["flow_in"],), 1.0)
    (s,) = findings(frozen)
    assert (s.detector_id, s.evidence_aliases, s.confidence) == ("dq_stuck", (frozen.pipe.aliases["temperature"],), 1.0)


# == Story 2.3: dq_drift and dq_gap ==============================================================

DRIFT, GAP = DqDrift(), DqGap()
DQ_MIN = T["dq_min"]


def facility_window(t_end, injections=(), seed=0, arrived_by=None):
    """A 15-minute FacilityWindow of real simulator output for all 24 pipes, ending at ``t_end`` (epoch s).

    ``arrived_by`` (epoch s) drops samples stamped after it: what SiteWise holds at that moment.
    """
    data = {a: [] for p in METAS.values() for a in p.aliases.values()}
    for pipe in F.pipes:
        state = physics.initial_state(pipe, seed)
        mine = [i for i in injections if i.pipe_id == pipe.id]
        acts = [physics.activate(state, i) for i in mine]
        for t in range(t_end - int(T["window_len_s"]) - 120, t_end):
            now = datetime.fromtimestamp(t, UTC)
            state, samples = physics.step(state, t, [a for a, i in zip(acts, mine) if i.active_at(now)])
            if t < t_end - T["window_len_s"] or (arrived_by is not None and t > arrived_by):
                continue
            for s in samples:
                data[METAS[pipe.id].aliases[s.measurement]].append(Sample(now, s.value, s.quality))
    end = datetime.fromtimestamp(t_end, UTC)
    return FacilityWindow(pipes=METAS, window=Window(start=end - timedelta(seconds=T["window_len_s"]), end=end, series=data))


def pass1(fw):
    """Findings of dq_drift (once) and dq_gap (per pipe) on a facility window."""
    found = DRIFT.detect(fw, T)
    for pipe in fw.pipes.values():
        pw = PipeWindow(pipe=pipe, window=Window(fw.window.start, fw.window.end, {a: fw.window.series[a] for a in pipe.aliases.values()}))
        found += GAP.detect(pw, T)
    return found


def inj(type_, pipe_id, measurement, start, minutes, **params):
    s = datetime.fromtimestamp(start, UTC)
    return new_injection(f"{type_}-{pipe_id}-{measurement}", type_, pipe_id, s, s + timedelta(minutes=minutes), measurement=measurement, params=params, facility=F)


def brief(found):
    return sorted((f.detector_id, f.pipe_id, f.evidence_aliases[0].rsplit("/", 1)[1]) for f in found)


T_END = int(END.timestamp())
# Several times of day (the ambient cycle and each pipe's AR state differ), plus a second seed.


# -- matrix rows (real simulator physics) ----------------------------------------------------------


def test_drift():
    """The ops/inject.py example: temperature drifting 0.5 K/min, 5 min of it in the window."""
    fw = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 300, 30, per_min=0.5)])
    found = pass1(fw)
    assert brief(found) == [("dq_drift", "P-010", "temperature")]  # peers unaffected
    (f,) = found
    dq, failing = dq_score.score_facility(fw, found)[METAS["P-010"].aliases["temperature"]]
    assert dq < DQ_MIN and failing == "dq_drift"
    assert f.explanation == "temperature has drifted up from the median of its 5 peer pipes by 0.5 K/min over the last 300 s."


def test_shared_change_is_not_drift():
    """The whole peer group moving together (a process change) leaves every residual flat."""
    group = [p.pipe_id for p in METAS.values() if p.peer_group == METAS["P-010"].peer_group]
    assert len(group) == 6
    fw = facility_window(T_END, [inj("dq_drift", pid, "temperature", T_END - 400, 30, per_min=0.5) for pid in group])
    assert DRIFT.detect(fw, T) == []


def test_silence():
    """dq_gap on P-020 flow_out for the last 3 min of the window."""
    fw = facility_window(T_END, [inj("dq_gap", "P-020", "flow_out", T_END - 180, 3)])
    found = pass1(fw)
    assert brief(found) == [("dq_gap", "P-020", "flow_out")]
    (f,) = found
    assert f.confidence == 1.0
    assert f.explanation == "flow_out has sent no GOOD samples for the last 181 s of the window."
    assert dq_score.score_facility(fw, found)[METAS["P-020"].aliases["flow_out"]] == (0.0, "dq_gap")


def test_empty_window_scores_exactly_one_dq_gap():
    empty = FacilityWindow(pipes=METAS, window=Window(START, END, {a: [] for p in METAS.values() for a in p.aliases.values()}))
    found = pass1(empty)
    assert len(found) == 144 and {f.detector_id for f in found} == {"dq_gap"} and {f.confidence for f in found} == {1.0}
    assert found[0].explanation.endswith("sent no GOOD samples in the window.")
    combined = dq_score.with_gap_rule(empty, found)
    assert sorted(f.evidence_aliases[0] for f in combined) == sorted(a for p in METAS.values() for a in p.aliases.values())
    assert set(dq_score.score_facility(empty, found).values()) == {(0.0, "dq_gap")}
    # Without the detector's findings the AD-13 rule is still the single source.
    assert len(dq_score.with_gap_rule(empty, [])) == 144


@pytest.mark.parametrize(("latency_s", "flagged"), [(55, False), (120, True)])
def test_ingest_lag(latency_s, flagged):
    """The simulator flushes every 5 s and SiteWise adds latency; the window ends 60 s behind the run
    (scheduled 17 s past the minute). Normal lag never raises dq_gap; data 2 minutes late does."""
    from detect.core.window import window_bounds

    scheduled = END + timedelta(seconds=T["ingest_lag_s"] + 17)
    start, end = window_bounds(scheduled, T)
    assert end == END
    arrived_by = int(scheduled.timestamp()) - 5 - latency_s
    fw = facility_window(T_END, arrived_by=arrived_by)
    found = pass1(fw)
    if flagged:
        assert {f.detector_id for f in found} == {"dq_gap"} and len(found) == 144
    else:
        assert found == []
