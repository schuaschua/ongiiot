"""``dq_flatline`` and ``dq_stuck`` on synthetic windows and on real simulator physics (Story 2.2 matrix).

Rows: Railed, Frozen, Short run, Partial, Recovered, Normal, Others. The Live row is a deployed check.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from detect import handler
from detect.core import dq_score
from detect.core.detectors import PASS1, DqFlatline, DqStuck
from detect.core.models import PipeWindow, Sample, Window
from facility.control import new_injection
from facility.model import load
from sim import physics

F = load(stage="poc-dj")
T = F.tunables
METAS = handler.pipe_metas(F)
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)
START = END - timedelta(seconds=T["window_len_s"])
FLAT, STUCK = DqFlatline(), DqStuck()


def healthy(i: int) -> float:
    """Varies every second, far from every contract rail."""
    return 100.0 + (i % 10) * 0.1


def series(values, quality="GOOD", end=END):
    """Ascending 1 Hz samples whose last one is at ``end - 1 s``."""
    n = len(values)
    return [Sample(end - timedelta(seconds=n - i), v, quality) for i, v in enumerate(values)]


def window(pipe_id="P-003", overrides=None):
    """A pipe window of healthy data, with ``{measurement: [Sample]}`` overrides."""
    pipe = METAS[pipe_id]
    data = {pipe.aliases[m]: series([healthy(i) for i in range(900)]) for m in pipe.aliases}
    for m, s in (overrides or {}).items():
        data[pipe.aliases[m]] = s
    return PipeWindow(pipe=pipe, window=Window(start=START, end=END, series=data))


def tail(values_before, value, run_n):
    """Healthy history, then ``run_n`` samples of ``value`` at the end."""
    return series([healthy(i) for i in range(values_before)] + [value] * run_n)


def findings(pw):
    return FLAT.detect(pw, T) + STUCK.detect(pw, T)


def dq_of(pw, found):
    by_alias = {a: [f for f in found if f.evidence_aliases == (a,)] for a in pw.pipe.aliases.values()}
    return {m: dq_score.score(by_alias[a]) for m, a in pw.pipe.aliases.items()}


# -- matrix rows --------------------------------------------------------------------------------


def test_railed():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(overrides={"flow_in": tail(800, lo, 61)})  # 61 samples span 60 s
    found = findings(pw)
    assert [(f.detector_id, f.evidence_aliases, f.confidence) for f in found] == [
        ("dq_flatline", (pw.pipe.aliases["flow_in"],), 1.0)
    ]
    assert dq_of(pw, found)["flow_in"] == (0.0, "dq_flatline")


def test_railed_at_max_and_within_rel_tol():
    c = METAS["P-003"].contracts["pressure_in"]
    tol = T["detectors"]["dq_flatline"]["rel_tol"] * (c.max - c.min)
    values = [c.max - tol * (i % 2) for i in range(121)]  # jitters inside the tolerance
    (f,) = findings(window(overrides={"pressure_in": series([healthy(i) for i in range(700)] + values)}))
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
    pw = window(overrides={"temperature": tail(800, 251.37, 21), "flow_in": tail(800, lo, 21)})  # 20 s each
    assert findings(pw) == []


@pytest.mark.parametrize("detector", ["dq_stuck", "dq_flatline"])
def test_partial_run_scales_confidence(detector):
    value = METAS["P-003"].contracts["flow_in"].min if detector == "dq_flatline" else 12.5
    pw = window(overrides={"flow_in": tail(800, value, 46)})  # 45 s
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
    recovered = [healthy(i) for i in range(10)]
    pw = window(
        overrides={
            "temperature": series([251.37] * 300 + recovered),
            "flow_in": series([lo] * 300 + recovered),
        }
    )
    assert findings(pw) == []


def test_railed_run_belongs_only_to_flatline():
    """Identical values at a rail would satisfy "stuck" too; only dq_flatline fires."""
    lo = METAS["P-003"].contracts["temperature"].min
    found = findings(window(overrides={"temperature": tail(800, lo, 61)}))
    assert [f.detector_id for f in found] == ["dq_flatline"]


def test_only_good_samples_with_values_count():
    """BAD / UNCERTAIN / valueless samples are ignored: they neither start, extend nor break a run."""
    stuck = [Sample(s.ts, s.value, "GOOD") for s in tail(800, 251.37, 61)]
    noisy = [Sample(s.ts, healthy(i), "BAD") if i % 7 == 0 and i > 800 else s for i, s in enumerate(stuck)]
    (f,) = findings(window(overrides={"temperature": noisy}))
    assert f.detector_id == "dq_stuck" and f.confidence == 1.0
    # A trailing run made only of non-GOOD samples is not a finding.
    bad = [Sample(s.ts, s.value, "BAD" if s.value == 251.37 else "GOOD") for s in stuck]
    assert findings(window(overrides={"temperature": bad})) == []
    none_tail = series([healthy(i) for i in range(800)] + [None] * 61)
    assert findings(window(overrides={"temperature": none_tail})) == []


def test_empty_or_missing_series_gives_no_finding():
    pw = window(overrides={"temperature": []})
    assert findings(pw) == []


def test_other_sensors_keep_full_trust():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(overrides={"flow_in": tail(800, lo, 61)})
    scores = dq_of(pw, findings(pw))
    assert {m: s for m, s in scores.items() if m != "flow_in"} == {m: (1.0, "") for m in pw.pipe.aliases if m != "flow_in"}


def test_finding_shape():
    lo = METAS["P-003"].contracts["flow_in"].min
    pw = window(overrides={"flow_in": tail(800, lo, 61), "temperature": tail(800, 251.37, 50)})
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
    assert [(d.id, d.version, d.kind) for d in PASS1] == [("dq_flatline", "1", "pipe"), ("dq_stuck", "1", "pipe")]


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
