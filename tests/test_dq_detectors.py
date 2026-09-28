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


def test_railed_at_max_and_within_rel_tol():
    c = METAS["P-003"].contracts["pressure_in"]
    tol = T["detectors"]["dq_flatline"]["rel_tol"] * (c.max - c.min)
    values = [c.max - tol * (i % 2) for i in range(121)]  # jitters inside the tolerance
    (f,) = findings(window(overrides={"pressure_in": series([healthy(i, "pressure_in") for i in range(700)] + values)}))
    assert (f.detector_id, f.confidence) == ("dq_flatline", 1.0)
    assert "maximum" in f.explanation


def test_just_outside_rel_tol_is_not_railed():
    c = METAS["P-003"].contracts["flow_in"]
    tol = T["detectors"]["dq_flatline"]["rel_tol"] * (c.max - c.min)
    values = [c.min + 3 * tol + (i % 2) * tol for i in range(121)]  # not railed, not identical
    assert findings(window(overrides={"flow_in": series(values)})) == []


def test_frozen():
    pw = window(overrides={"temperature": tail(800, 251.37, 61)})
    found = findings(pw)
    assert [(f.detector_id, f.confidence) for f in found] == [("dq_stuck", 1.0)]
    assert dq_of(pw, found)["temperature"] == (0.0, "dq_stuck")


def test_short_run_gives_no_finding():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(overrides={"temperature": tail(800, 251.37, 21), "flow_in": tail(800, lo, 21, m="flow_in")})  # 20 s each
    assert findings(pw) == []


@pytest.mark.parametrize("detector", ["dq_stuck", "dq_flatline"])
def test_partial_run_scales_confidence(detector):
    value = METAS["P-003"].contracts["flow_in"].min if detector == "dq_flatline" else 12.5
    pw = window(overrides={"flow_in": tail(800, value, 46, m="flow_in")})  # 45 s
    (f,) = findings(pw)
    assert (f.detector_id, f.confidence) == (detector, 0.75)
    dq, failing = dq_of(pw, [f])["flow_in"]
    assert (dq, failing) == (0.25, detector) and dq < T["dq_min"]


def test_confidence_at_min_run_boundary():
    pw = window(overrides={"temperature": tail(800, 251.37, 31)})  # exactly min_run_s = 30 s
    (f,) = findings(pw)
    assert f.confidence == 0.5
    assert findings(window(overrides={"temperature": tail(800, 251.37, 30)})) == []  # 29 s


def test_recovered_gives_no_finding():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(
        overrides={
            "temperature": series([251.37] * 300 + [healthy(i) for i in range(10)]),
            "flow_in": series([lo] * 300 + [healthy(i, "flow_in") for i in range(10)]),
        }
    )
    assert findings(pw) == []


def test_railed_run_belongs_only_to_flatline():
    """Identical values at a rail would satisfy "stuck" too; only dq_flatline fires."""
    lo = METAS["P-003"].contracts["temperature"].min
    found = findings(window(overrides={"temperature": tail(800, lo, 61)}))
    assert [f.detector_id for f in found] == ["dq_flatline"]


def test_non_good_sample_ends_the_run():
    """A BAD / UNCERTAIN / valueless sample ends the run: the sensor may have changed during it."""
    stuck = tail(800, 251.37, 61)
    for broken in (Sample(stuck[-40].ts, 251.37, "BAD"), Sample(stuck[-40].ts, None, "GOOD"), Sample(stuck[-40].ts, 251.37, "UNCERTAIN")):
        samples = stuck[:-40] + [broken] + stuck[-39:]  # 39 GOOD samples after it: 38 s
        (f,) = findings(window(overrides={"temperature": samples}))
        assert (f.detector_id, f.confidence) == ("dq_stuck", pytest.approx(38 / 60))
    # Identical GOOD values on both sides of a BAD stretch are not one run.
    split = series([healthy(i) for i in range(800)] + [251.37] * 20 + [300.0] * 30 + [251.37] * 20)
    split = [Sample(s.ts, s.value, "BAD" if s.value == 300.0 else "GOOD") for s in split]
    assert findings(window(overrides={"temperature": split})) == []


def test_trailing_non_good_samples_are_skipped_to_the_last_good_one():
    stuck = tail(800, 251.37, 61) + [Sample(END + timedelta(seconds=i), None, "BAD") for i in range(3)]
    (f,) = findings(window(overrides={"temperature": stuck}))
    assert (f.detector_id, f.confidence) == ("dq_stuck", 1.0)
    bad_run = [Sample(s.ts, s.value, "BAD" if s.value == 251.37 else "GOOD") for s in tail(800, 251.37, 61)]
    assert findings(window(overrides={"temperature": bad_run})) == []


def test_gap_longer_than_two_periods_splits_the_run():
    """Two identical GOOD samples far apart are not a long run (contract rate 1 Hz: max gap 2 s)."""
    lo = METAS["P-003"].contracts["flow_in"].min
    far = [Sample(END - timedelta(seconds=300), 251.37, "GOOD"), Sample(END - timedelta(seconds=1), 251.37, "GOOD")]
    assert findings(window(overrides={"temperature": far, "flow_in": [Sample(x.ts, lo, "GOOD") for x in far]})) == []
    # A 2 s gap (one missing sample) keeps the run whole; a 3 s gap splits it.
    run = tail(800, 251.37, 61)
    one_missing = run[:-30] + run[-29:]
    assert findings(window(overrides={"temperature": one_missing}))[0].confidence == 1.0
    two_missing = run[:-30] + run[-28:]
    assert findings(window(overrides={"temperature": two_missing})) == []  # 27 s < min_run_s


def test_gap_limit_follows_the_contract_rate():
    import dataclasses

    pipe = METAS["P-003"]
    slow = dataclasses.replace(pipe.contracts["temperature"], rate_hz=0.1)  # period 10 s: gaps up to 20 s
    none = dataclasses.replace(pipe.contracts["temperature"], rate_hz=0)  # no rate: 2 s
    samples = [Sample(END - timedelta(seconds=10 * (7 - i)), 251.37, "GOOD") for i in range(7)]  # 60 s every 10 s
    for contract, expected in ((slow, 1.0), (none, None)):
        meta = dataclasses.replace(pipe, contracts={**pipe.contracts, "temperature": contract})
        pw = PipeWindow(pipe=meta, window=Window(START, END, {**window().window.series, pipe.aliases["temperature"]: samples}))
        found = STUCK.detect(pw, T)
        assert [f.confidence for f in found] == ([] if expected is None else [expected])


def test_values_beyond_the_contract_limits_are_railed():
    c = METAS["P-003"].contracts["flow_in"]
    for value, rail in ((c.max + 7.0, "maximum"), (c.min - 1.0, "minimum")):
        (f,) = findings(window(overrides={"flow_in": tail(800, value, 61, m="flow_in")}))
        assert (f.detector_id, f.confidence) == ("dq_flatline", 1.0) and rail in f.explanation
    # Pegged above max but noisy: still railed, never "stuck".
    noisy = series([healthy(i, "flow_in") for i in range(800)] + [c.max + 1 + (i % 3) for i in range(61)])
    assert [f.detector_id for f in findings(window(overrides={"flow_in": noisy}))] == ["dq_flatline"]


def test_explanation_omits_an_empty_unit():
    import dataclasses

    pipe = METAS["P-003"]
    for unit in ("", None):
        contract = dataclasses.replace(pipe.contracts["temperature"], unit=unit)
        meta = dataclasses.replace(pipe, contracts={**pipe.contracts, "temperature": contract})
        pw = PipeWindow(pipe=meta, window=Window(START, END, {**window().window.series, pipe.aliases["temperature"]: tail(800, 251.37, 61)}))
        (f,) = STUCK.detect(pw, T)
        assert f.explanation == "temperature has repeated exactly 251.37 for the last 60 s."


def test_empty_or_missing_series_gives_no_finding():
    pw = window(overrides={"temperature": []})
    assert findings(pw) == []


def test_other_sensors_keep_full_trust():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(overrides={"flow_in": tail(800, lo, 61, m="flow_in")})
    scores = dq_of(pw, findings(pw))
    assert {m: s for m, s in scores.items() if m != "flow_in"} == {m: (1.0, "") for m in pw.pipe.aliases if m != "flow_in"}


def test_finding_shape():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(overrides={"flow_in": tail(800, lo, 61, m="flow_in"), "temperature": tail(800, 251.37, 50)})
    found = findings(pw)
    assert {f.detector_id for f in found} == {"dq_flatline", "dq_stuck"}
    for f in found:
        assert len(f.evidence_aliases) == 1 and f.evidence_aliases[0] in pw.pipe.aliases.values()
        assert 0.0 <= f.confidence <= 1.0
        assert f.pipe_id == "P-003" and (f.window_start, f.window_end) == (START, END)
        assert f.explanation.endswith(".") and f.explanation.count(". ") == 0
        dq_score.check_finding(f, pw.pipe.aliases.values())
    by_id = {f.detector_id: f for f in found}
    assert by_id["dq_flatline"].explanation == "flow_in has been railed at its contract minimum of 0 kg/s for the last 60 s."
    assert by_id["dq_stuck"].explanation == "temperature has repeated exactly 251.37 K for the last 49 s."
    assert by_id["dq_stuck"].detector_version == by_id["dq_flatline"].detector_version == "1"


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


@pytest.mark.parametrize("t_end", [int(END.timestamp()), int(END.timestamp()) + 6 * 3600 + 17])
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
NORMAL_ENDS = [(T_END, 0), (T_END + 5 * 3600 + 17, 0), (T_END + 11 * 3600 + 5, 0), (T_END + 17 * 3600 + 41, 0), (T_END, 3)]


@pytest.fixture(scope="module", params=NORMAL_ENDS, ids=lambda p: f"end+{p[0] - T_END}s-seed{p[1]}")
def normal_fw(request):
    t_end, seed = request.param
    return facility_window(t_end, seed=seed)


# -- matrix rows (real simulator physics) ----------------------------------------------------------


def test_normal_simulator_windows_raise_neither_drift_nor_gap(normal_fw):
    assert sum(len(s) == T["window_len_s"] for s in normal_fw.window.series.values()) == 144
    assert pass1(normal_fw) == []


def test_drift():
    """The ops/inject.py example: temperature drifting 0.5 K/min, 5 min of it in the window."""
    fw = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 300, 30, per_min=0.5)])
    found = pass1(fw)
    assert brief(found) == [("dq_drift", "P-010", "temperature")]  # peers unaffected
    (f,) = found
    dq, failing = dq_score.score_facility(fw, found)[METAS["P-010"].aliases["temperature"]]
    assert dq < DQ_MIN and failing == "dq_drift"
    assert f.explanation == "temperature has drifted up from the median of its 5 peer pipes by 0.5 K/min over the last 300 s."


@pytest.mark.parametrize("pipe_id", ["P-003", "P-004", "P-016", "P-020"])
def test_drift_crosses_dq_min_within_five_minutes_on_any_group(pipe_id):
    fw = facility_window(T_END, [inj("dq_drift", pipe_id, "temperature", T_END - 300, 30, per_min=0.5)])
    found = pass1(fw)
    assert brief(found) == [("dq_drift", pipe_id, "temperature")]
    assert 1 - found[0].confidence < DQ_MIN


def test_drift_downwards_and_before_it_is_established():
    fw = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 300, 30, per_min=-0.5)])
    (f,) = DRIFT.detect(fw, T)
    assert "drifted down" in f.explanation and f.confidence == 1.0
    # One minute of drift in a 300 s fit is not yet a line through the residual.
    early = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 60, 30, per_min=0.5)])
    assert DRIFT.detect(early, T) == []


def test_shared_change_is_not_drift():
    """The whole peer group moving together (a process change) leaves every residual flat."""
    group = [p.pipe_id for p in METAS.values() if p.peer_group == METAS["P-010"].peer_group]
    assert len(group) == 6
    fw = facility_window(T_END, [inj("dq_drift", pid, "temperature", T_END - 400, 30, per_min=0.5) for pid in group])
    assert DRIFT.detect(fw, T) == []


def test_a_leak_step_is_not_drift():
    """A leak steps flow_out, pressure_out and methane_ppm: a step is not a line (R² < min_r2)."""
    for start in (T_END - 150, T_END - 60, T_END - 240):
        fw = facility_window(T_END, [inj("leak", "P-003", None, start, 30)])
        assert DRIFT.detect(fw, T) == [], start


def test_too_few_peers_gives_no_drift():
    fw = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 400, 30, per_min=0.5)])
    group = sorted(p.pipe_id for p in METAS.values() if p.peer_group == METAS["P-010"].peer_group)
    peers = [p for p in group if p != "P-010"]

    def only(kept):
        series = dict(fw.window.series)
        for pid in peers:
            if pid not in kept:
                series[METAS[pid].aliases["temperature"]] = []
        return FacilityWindow(pipes=fw.pipes, window=Window(fw.window.start, fw.window.end, series))

    assert DRIFT.detect(only(peers[:1]), T) == []  # one peer with data
    assert DRIFT.detect(only([]), T) == []
    # Two peers is the minimum; with three (AD-14's smallest group of 4) the peers stay clean.
    assert ("dq_drift", "P-010", "temperature") in brief(DRIFT.detect(only(peers[:2]), T))
    assert brief(DRIFT.detect(only(peers[:3]), T)) == [("dq_drift", "P-010", "temperature")]


def test_too_few_points_gives_no_drift():
    fw = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 400, 30, per_min=0.5)])
    alias = METAS["P-010"].aliases["temperature"]
    series = dict(fw.window.series)
    series[alias] = series[alias][-(T["detectors"]["dq_drift"]["min_points"] - 1) :]
    assert DRIFT.detect(FacilityWindow(fw.pipes, Window(fw.window.start, fw.window.end, series)), T) == []


def test_non_good_samples_are_ignored_by_drift():
    fw = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 400, 30, per_min=0.5)])
    alias = METAS["P-010"].aliases["temperature"]
    series = dict(fw.window.series)
    series[alias] = [Sample(s.ts, s.value, "BAD") for s in series[alias]]
    assert DRIFT.detect(FacilityWindow(fw.pipes, Window(fw.window.start, fw.window.end, series)), T) == []


def test_silence():
    """dq_gap on P-020 flow_out for the last 3 min of the window."""
    fw = facility_window(T_END, [inj("dq_gap", "P-020", "flow_out", T_END - 180, 3)])
    found = pass1(fw)
    assert brief(found) == [("dq_gap", "P-020", "flow_out")]
    (f,) = found
    assert f.confidence == 1.0
    assert f.explanation == "flow_out has sent no GOOD samples for the last 181 s of the window."
    assert dq_score.score_facility(fw, found)[METAS["P-020"].aliases["flow_out"]] == (0.0, "dq_gap")


def test_silence_crosses_dq_min_just_past_min_gap():
    # A gap starting ``secs`` before the window end: the last sample is at end - secs - 1.
    for secs, expected in ((28, None), (29, 0.5), (44, 0.75), (59, 1.0)):
        fw = facility_window(T_END, [inj("dq_gap", "P-020", "flow_out", T_END - secs, 3)])
        found = GAP.detect(PipeWindow(METAS["P-020"], Window(fw.window.start, fw.window.end, {a: fw.window.series[a] for a in METAS["P-020"].aliases.values()})), T)
        assert [f.confidence for f in found] == ([] if expected is None else [expected]), secs


def test_old_gap_with_data_since_gives_no_finding():
    fw = facility_window(T_END, [inj("dq_gap", "P-020", "flow_out", T_END - 600, 3)])
    assert pass1(fw) == []


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


def test_all_bad_samples_are_silence():
    pw = window(overrides={"temperature": series([healthy(i) for i in range(900)], quality="BAD")})
    (f,) = GAP.detect(pw, T)
    assert (f.detector_id, f.confidence) == ("dq_gap", 1.0) and "no GOOD samples in the window" in f.explanation


@pytest.mark.parametrize(("latency_s", "flagged"), [(0, False), (20, False), (55, False), (120, True)])
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


def test_new_finding_shapes():
    fw = facility_window(
        T_END,
        [inj("dq_drift", "P-010", "temperature", T_END - 300, 30, per_min=0.5), inj("dq_gap", "P-020", "flow_out", T_END - 180, 3)],
    )
    found = pass1(fw)
    assert brief(found) == [("dq_drift", "P-010", "temperature"), ("dq_gap", "P-020", "flow_out")]
    for f in found:
        assert f.detector_version == "1" and (f.window_start, f.window_end) == (fw.window.start, fw.window.end)
        assert f.explanation.endswith(".") and f.explanation.count(". ") == 0
        dq_score.check_finding(f, fw.pipes[f.pipe_id].aliases.values())


def test_drift_ignores_empty_unit_and_bad_contract():
    import dataclasses

    fw = facility_window(T_END, [inj("dq_drift", "P-010", "temperature", T_END - 300, 30, per_min=0.5)])
    p = METAS["P-010"]
    for contract, expected in (
        (dataclasses.replace(p.contracts["temperature"], unit=""), "by 0.5 per min"),
        (dataclasses.replace(p.contracts["temperature"], max=p.contracts["temperature"].min), None),
    ):
        pipes = {**METAS, "P-010": dataclasses.replace(p, contracts={**p.contracts, "temperature": contract})}
        found = DRIFT.detect(FacilityWindow(pipes, fw.window), T)
        if expected is None:
            assert found == []
        else:
            (f,) = found
            assert expected in f.explanation
