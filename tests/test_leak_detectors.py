"""``leak_*`` detectors (Story 3.1, pass 2) on synthetic windows and on real simulator physics.

Matrix rows: Leak, Healthy, Untrusted flow, Untrusted corroboration, Isolation (the Live row is a deployed
check), plus the calibration targets: no finding in normal 30-minute runs, and a default leak on P-007
found by both detectors at confidence >= 0.8 within two runs of entering the window.
"""

from __future__ import annotations

import bisect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from detect import handler
from detect.core import dq_score
from detect.core.detectors import PASS1, PASS2, LeakMassBalance, LeakMethaneRise
from detect.core.models import FacilityWindow, PipeWindow, Sample, Window
from facility.control import new_injection
from facility.model import load
from sim import physics

F = load(stage="poc-dj")
T = F.tunables
METAS = handler.pipe_metas(F)
DQ_MIN = T["dq_min"]
MB, MR = LeakMassBalance(), LeakMethaneRise()
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)
T_END = int(END.timestamp())
START = END - timedelta(seconds=T["window_len_s"])
N = int(T["window_len_s"])


# -- helpers ---------------------------------------------------------------------------------------


def trusted(fw, dq=None):
    """``fw`` with every sensor's dq set to 1.0, except ``{(pipe_id, measurement): dq}`` overrides."""
    dq = dq or {}
    pipes = {
        pid: replace(p, dq=MappingProxyType({m: dq.get((pid, m), 1.0) for m in p.aliases}))
        for pid, p in fw.pipes.items()
    }
    return FacilityWindow(pipes=pipes, window=fw.window)


def annotate(fw):
    """What ``handler.run`` does before pass 2: pass 1, ``dq_score``, then ``PipeMeta.dq`` filled."""
    found = []
    for det in PASS1:
        found += handler._run_detector(det, fw, T)
    scores = dq_score.score_facility(fw, found)
    pipes = {
        pid: replace(p, dq=MappingProxyType({m: scores[a][0] for m, a in p.aliases.items()}))
        for pid, p in fw.pipes.items()
    }
    return FacilityWindow(pipes=pipes, window=fw.window), found


LEAKS = tuple(d for d in PASS2 if d.id.startswith("leak_"))  # outlier_peer has its own tests (test_outlier_peer.py)


def pass2(afw):
    """Leak findings on an annotated facility window, through the handler's dispatch and checks."""
    out = []
    for det in LEAKS:
        out += handler._run_detector(det, afw, T, check=handler.check_pass2_finding)
    return out


def brief(found):
    return sorted((f.detector_id, f.pipe_id) for f in found)


def pipe_window(afw, pipe_id):
    p = afw.pipes[pipe_id]
    w = afw.window
    return PipeWindow(pipe=p, window=Window(w.start, w.end, {a: w.series[a] for a in p.aliases.values()}))


# -- synthetic windows -----------------------------------------------------------------------------


def synthetic(values=None, n=N):
    """A facility window where every sensor of every pipe reads ``values[(pipe_id, m)](i)`` or a flat base.

    Defaults: ``flow_in`` = ``flow_out`` = 20, ``pressure_in`` 3e6, ``pressure_out`` 2.9e6, methane 2 ppmv,
    temperature 295 K, all GOOD at 1 Hz, the last sample 1 s before ``END``.
    """
    base = {"flow_in": 20.0, "flow_out": 20.0, "pressure_in": 3.0e6, "pressure_out": 2.9e6, "methane_ppm": 2.0, "temperature": 295.0}
    values = values or {}
    series = {}
    for pid, p in METAS.items():
        for m, a in p.aliases.items():
            fn = values.get((pid, m), lambda i, m=m: base[m])
            series[a] = [Sample(END - timedelta(seconds=n - i), fn(i), "GOOD") for i in range(n)]
    return FacilityWindow(pipes=METAS, window=Window(start=START, end=END, series=series))


def leaking(pipe_id="P-007", frac=0.10, tail_s=60, methane=600.0, p_drop=0.01):
    """A synthetic window where ``pipe_id`` loses ``frac`` of its flow for the last ``tail_s`` seconds,
    methane rises to ``methane`` ppmv and pressure_out drops by ``p_drop`` of pressure_in."""
    on = lambda i: i >= N - tail_s  # noqa: E731
    return synthetic(
        {
            (pipe_id, "flow_out"): lambda i: 20.0 * (1 - frac) if on(i) else 20.0,
            (pipe_id, "methane_ppm"): lambda i: 2.0 + methane if on(i) else 2.0,
            (pipe_id, "pressure_out"): lambda i: 2.9e6 - p_drop * 3.0e6 if on(i) else 2.9e6,
        }
    )


def test_synthetic_leak_gives_both_findings_with_evidence_and_explanations():
    fw = trusted(leaking())
    found = pass2(fw)
    assert brief(found) == [("leak_mass_balance", "P-007"), ("leak_methane_rise", "P-007")]
    mb, mr = sorted(found, key=lambda f: f.detector_id)
    a = METAS["P-007"].aliases
    assert mb.evidence_aliases == (a["flow_in"], a["flow_out"], a["pressure_in"], a["pressure_out"])
    assert mb.confidence == 1.0
    assert mb.explanation == (
        "flow_out is 10.0% below flow_in (2 kg/s missing) on average over the last 60 s, "
        "and pressure_out has dropped 1.03% relative to pressure_in from earlier in the window."
    )
    assert mr.evidence_aliases == (a["methane_ppm"],) and mr.confidence == 1.0
    assert mr.explanation == "methane_ppm averaged 602 ppmv over the last 60 s, 600 ppmv above the median of its 5 peer pipes."
    for f in found:
        assert (f.window_start, f.window_end, f.detector_version) == (START, END, "1")
        assert f.explanation.count(".") <= 3 and f.explanation.endswith(".")


@pytest.mark.parametrize(("frac", "expected"), [(0.021, 0.2625)])
def test_mass_balance_confidence(frac, expected):
    found = MB.detect(pipe_window(trusted(leaking(frac=frac)), "P-007"), T)
    if expected is None:
        assert found == []
    else:
        (f,) = found
        assert f.confidence == pytest.approx(expected)


@pytest.mark.parametrize(("ppm", "expected"), [(30.0, 0.1)])
def test_methane_confidence(ppm, expected):
    found = MR.detect(trusted(leaking(methane=ppm)), T)
    if expected is None:
        assert found == []
    else:
        (f,) = found
        assert f.pipe_id == "P-007" and f.confidence == pytest.approx(expected)


def test_flow_out_above_flow_in_is_not_a_leak():
    fw = trusted(synthetic({("P-007", "flow_out"): lambda i: 25.0}))
    assert MB.detect(pipe_window(fw, "P-007"), T) == []


def test_high_methane_across_the_whole_peer_group_is_not_a_leak():
    """A shared rise (weather, a flare) moves the peer median too."""
    group = [pid for pid, p in METAS.items() if p.peer_group == METAS["P-007"].peer_group]
    fw = trusted(synthetic({(pid, "methane_ppm"): lambda i: 400.0 for pid in group}))
    assert MR.detect(fw, T) == []


# -- real simulator physics -------------------------------------------------------------------------


def simulate(t_lo, t_hi, injections=(), seed=0):
    """alias -> ascending samples of real simulator output for all 24 pipes, ``t_lo <= t < t_hi`` (2 min warm-up)."""
    data = {a: [] for p in METAS.values() for a in p.aliases.values()}
    for pipe in F.pipes:
        state = physics.initial_state(pipe, seed)
        mine = [i for i in injections if i.pipe_id == pipe.id]
        acts = [physics.activate(state, i) for i in mine]
        for t in range(t_lo - 120, t_hi):
            now = datetime.fromtimestamp(t, UTC)
            state, samples = physics.step(state, t, [a for a, i in zip(acts, mine) if i.active_at(now)])
            if t < t_lo:
                continue
            for s in samples:
                data[METAS[pipe.id].aliases[s.measurement]].append(Sample(now, s.value, s.quality))
    return data


def window_at(data, t_end):
    """The detection window ending at ``t_end`` (epoch s), sliced from ``simulate`` output."""
    end = datetime.fromtimestamp(t_end, UTC)
    start = end - timedelta(seconds=N)
    series = {}
    for a, samples in data.items():
        ts = [s.ts for s in samples]
        series[a] = samples[bisect.bisect_left(ts, start) : bisect.bisect_left(ts, end)]
    return FacilityWindow(pipes=METAS, window=Window(start=start, end=end, series=series))


def leak(pipe_id, start, minutes=30, **params):
    s = datetime.fromtimestamp(start, UTC)
    return new_injection(f"leak-{pipe_id}", "leak", pipe_id, s, s + timedelta(minutes=minutes), params=params, facility=F)


def fault(type_, pipe_id, m, start, minutes=30):
    s = datetime.fromtimestamp(start, UTC)
    return new_injection(f"{type_}-{pipe_id}-{m}", type_, pipe_id, s, s + timedelta(minutes=minutes), measurement=m, params={}, facility=F)


# Several times of day (the ambient cycle and each pipe's AR state differ) and seeds.


@pytest.mark.parametrize("entered_s", [1])
def test_default_leak_on_p007_is_found_within_two_runs(entered_s):
    """A default leak (10% of mean flow) that has been in the window ``entered_s`` seconds at the first run:
    both findings on P-007 at confidence >= 0.8 by the second run, and nothing on any other pipe."""
    start = T_END - entered_s
    data = simulate(T_END - N, T_END + 60, [leak("P-007", start)])
    confidences = []
    for t_end in (T_END, T_END + 60):
        afw, p1 = annotate(window_at(data, t_end))
        assert p1 == []  # the leak does not make its own evidence untrusted
        found = pass2(afw)
        assert {f.pipe_id for f in found} <= {"P-007"}
        confidences.append({f.detector_id: f.confidence for f in found})
    second = confidences[1]
    assert set(second) == {"leak_mass_balance", "leak_methane_rise"}
    assert min(second.values()) >= 0.8


def test_untrusted_flow_during_a_leak():
    """P-007 flow_out railed (dq_flatline, dq 0.0) during a leak: no mass balance, methane still found."""
    injections = [leak("P-007", T_END - 180), fault("dq_flatline", "P-007", "flow_out", T_END - 180)]
    afw, p1 = annotate(window_at(simulate(T_END - N, T_END, injections), T_END))
    assert [(f.detector_id, f.pipe_id) for f in p1] == [("dq_flatline", "P-007")]
    assert afw.pipes["P-007"].dq["flow_out"] < DQ_MIN
    assert brief(pass2(afw)) == [("leak_methane_rise", "P-007")]
