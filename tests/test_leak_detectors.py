"""``leak_*`` detectors (Story 3.1, pass 2) on synthetic windows and on real simulator physics.

Matrix rows: Leak, Healthy, Untrusted flow, Untrusted corroboration, Isolation (the Live row is a deployed
check), plus the calibration targets: no finding in normal 30-minute runs, and a default leak on P-007
found by both detectors at confidence >= 0.8 within two runs of entering the window.
"""

from __future__ import annotations

import ast
import bisect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from detect import handler
from detect.core import dq_score
from detect.core.detectors import PASS1, PASS2, LeakMassBalance, LeakMethaneRise
from detect.core.models import FacilityDetector, FacilityWindow, PipeDetector, PipeWindow, Sample, Window
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


def pass2(afw):
    """Pass-2 findings on an annotated facility window, through the handler's dispatch and checks."""
    out = []
    for det in PASS2:
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


def test_healthy_synthetic_window_gives_nothing():
    assert pass2(trusted(synthetic())) == []


@pytest.mark.parametrize(("frac", "expected"), [(0.019, None), (0.021, 0.2625), (0.04, 0.5), (0.08, 1.0), (0.3, 1.0)])
def test_mass_balance_confidence(frac, expected):
    found = MB.detect(pipe_window(trusted(leaking(frac=frac)), "P-007"), T)
    if expected is None:
        assert found == []
    else:
        (f,) = found
        assert f.confidence == pytest.approx(expected)


@pytest.mark.parametrize(("ppm", "expected"), [(29.0, None), (30.0, 0.1), (150.0, 0.5), (300.0, 1.0), (5000.0, 1.0)])
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


def test_mass_balance_uses_only_the_trailing_span():
    """A loss that stopped before the trailing ``avg_s`` gives nothing."""
    fw = trusted(synthetic({("P-007", "flow_out"): lambda i: 18.0 if i < N - 60 else 20.0}))
    assert MB.detect(pipe_window(fw, "P-007"), T) == []


def test_too_few_paired_seconds_gives_no_mass_balance():
    fw = leaking()
    a = METAS["P-007"].aliases["flow_out"]
    series = dict(fw.window.series)
    series[a] = series[a][-(int(T["detectors"]["leak_mass_balance"]["min_points"]) - 1) :]
    fw = trusted(FacilityWindow(pipes=fw.pipes, window=Window(fw.window.start, fw.window.end, series)))
    assert MB.detect(pipe_window(fw, "P-007"), T) == []


def test_non_good_samples_are_ignored():
    fw = leaking()
    a = METAS["P-007"].aliases["flow_out"]
    series = dict(fw.window.series)
    series[a] = [Sample(s.ts, s.value, "BAD") for s in series[a]]
    assert MB.detect(pipe_window(trusted(FacilityWindow(fw.pipes, Window(fw.window.start, fw.window.end, series))), "P-007"), T) == []


@pytest.mark.parametrize("m", ["flow_in", "flow_out"])
def test_untrusted_required_flow_gives_no_mass_balance(m):
    fw = trusted(leaking(), {("P-007", m): DQ_MIN - 0.01})
    assert brief(pass2(fw)) == [("leak_methane_rise", "P-007")]
    # dq exactly at dq_min is trusted.
    assert brief(pass2(trusted(leaking(), {("P-007", m): DQ_MIN}))) == [
        ("leak_mass_balance", "P-007"),
        ("leak_methane_rise", "P-007"),
    ]


def test_missing_dq_means_untrusted():
    """Pass 2 needs the dq-annotated window: a sensor without a score is never trusted."""
    assert pass2(leaking()) == []


def test_untrusted_pressure_out_is_named_and_the_finding_still_made():
    fw = trusted(leaking(), {("P-007", "pressure_out"): 0.2})
    (f,) = MB.detect(pipe_window(fw, "P-007"), T)
    a = METAS["P-007"].aliases
    assert f.evidence_aliases == (a["flow_in"], a["flow_out"]) and f.confidence == 1.0
    assert f.explanation == (
        "flow_out is 10.0% below flow_in (2 kg/s missing) on average over the last 60 s; "
        "pressure_out was excluded as untrusted (dq 0.20 < dq_min 0.5)."
    )


def test_untrusted_pressure_in_skips_the_pressure_check_and_says_so():
    fw = trusted(leaking(), {("P-007", "pressure_in"): 0.0})
    (f,) = MB.detect(pipe_window(fw, "P-007"), T)
    assert "pressure_out was not checked because pressure_in was excluded as untrusted (dq 0.00 < dq_min 0.5)" in f.explanation
    assert len(f.evidence_aliases) == 2


def test_no_matching_pressure_drop_is_not_cited():
    (f,) = MB.detect(pipe_window(trusted(leaking(p_drop=0.0)), "P-007"), T)
    assert "pressure" not in f.explanation and len(f.evidence_aliases) == 2


def test_untrusted_methane_gives_no_methane_finding():
    fw = trusted(leaking(), {("P-007", "methane_ppm"): 0.0})
    assert brief(pass2(fw)) == [("leak_mass_balance", "P-007")]


def test_untrusted_peers_are_left_out_and_named():
    group = sorted(pid for pid, p in METAS.items() if p.peer_group == METAS["P-007"].peer_group and pid != "P-007")
    fw = trusted(leaking(), {(group[0], "methane_ppm"): 0.1, (group[1], "methane_ppm"): 0.0})
    (f,) = MR.detect(fw, T)
    assert f.explanation == (
        f"methane_ppm averaged 602 ppmv over the last 60 s, 600 ppmv above the median of its 3 peer pipes "
        f"({group[0]}, {group[1]} excluded as untrusted, dq < dq_min 0.5)."
    )


def test_too_few_trusted_peers_gives_no_methane_finding():
    group = sorted(pid for pid, p in METAS.items() if p.peer_group == METAS["P-007"].peer_group and pid != "P-007")
    fw = trusted(leaking(), {(pid, "methane_ppm"): 0.0 for pid in group[:4]})
    assert MR.detect(fw, T) == []


def test_high_methane_across_the_whole_peer_group_is_not_a_leak():
    """A shared rise (weather, a flare) moves the peer median too."""
    group = [pid for pid, p in METAS.items() if p.peer_group == METAS["P-007"].peer_group]
    fw = trusted(synthetic({(pid, "methane_ppm"): lambda i: 400.0 for pid in group}))
    assert MR.detect(fw, T) == []


def test_window_end_is_exclusive():
    """Samples at or after window_end are not read."""
    fw = synthetic()
    a = METAS["P-007"].aliases
    series = dict(fw.window.series)
    series[a["flow_out"]] = series[a["flow_out"]] + [Sample(END + timedelta(seconds=i), 10.0, "GOOD") for i in range(60)]
    series[a["methane_ppm"]] = series[a["methane_ppm"]] + [Sample(END + timedelta(seconds=i), 900.0, "GOOD") for i in range(60)]
    assert pass2(trusted(FacilityWindow(fw.pipes, Window(fw.window.start, fw.window.end, series)))) == []


def test_detectors_declare_id_version_kind():
    assert [(d.id, d.version, d.kind) for d in PASS2] == [("leak_mass_balance", "1", "pipe"), ("leak_methane_rise", "1", "facility")]
    for d in PASS2:
        assert isinstance(d, FacilityDetector if d.kind == "facility" else PipeDetector)


def test_detectors_import_types_only_from_models():
    root = Path(__file__).resolve().parent.parent / "detect" / "core" / "detectors"
    for name in ("leak_mass_balance.py", "leak_methane_rise.py", "_pass2.py", "_peers.py"):
        for node in ast.walk(ast.parse((root / name).read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("detect."):
                assert node.module == "detect.core.models" or node.module.startswith("detect.core.detectors"), (name, node.module)


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
NORMAL_RUNS = [(T_END, 0), (T_END + 7 * 3600 + 13, 0), (T_END + 15 * 3600 + 29, 1), (T_END + 3 * 3600 + 41, 3)]


@pytest.mark.parametrize(("t_first", "seed"), NORMAL_RUNS, ids=lambda v: str(v))
def test_healthy_thirty_minute_runs_give_no_leak_findings(t_first, seed):
    """30 consecutive one-minute runs of normal simulator output: no leak finding on any pipe."""
    ends = [t_first + 60 * k for k in range(30)]
    data = simulate(ends[0] - N, ends[-1], seed=seed)
    for t_end in ends:
        afw, p1 = annotate(window_at(data, t_end))
        assert p1 == [], t_end
        assert pass2(afw) == [], t_end


@pytest.mark.parametrize("entered_s", [1, 17, 30, 45, 60])
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


def test_leak_row_evidence_and_isolation():
    """The Leak and Isolation rows: 2 minutes of a default leak in the window, on P-007 only."""
    fw = window_at(simulate(T_END - N, T_END, [leak("P-007", T_END - 120)]), T_END)
    afw, _ = annotate(fw)
    found = pass2(afw)
    assert brief(found) == [("leak_mass_balance", "P-007"), ("leak_methane_rise", "P-007")]
    a = METAS["P-007"].aliases
    by_id = {f.detector_id: f for f in found}
    mb, mr = by_id["leak_mass_balance"], by_id["leak_methane_rise"]
    assert mb.evidence_aliases == (a["flow_in"], a["flow_out"], a["pressure_in"], a["pressure_out"])
    assert mr.evidence_aliases == (a["methane_ppm"],)
    assert mb.confidence >= 0.8 and mr.confidence >= 0.8
    assert "below flow_in" in mb.explanation and "pressure_out has dropped" in mb.explanation
    assert "above the median of its 5 peer pipes" in mr.explanation


@pytest.mark.parametrize("pipe_id", ["P-003", "P-010", "P-016", "P-020"])
def test_default_leak_is_found_on_every_peer_group(pipe_id):
    fw = window_at(simulate(T_END - N, T_END, [leak(pipe_id, T_END - 120)]), T_END)
    afw, _ = annotate(fw)
    found = pass2(afw)
    assert brief(found) == [("leak_mass_balance", pipe_id), ("leak_methane_rise", pipe_id)]
    assert min(f.confidence for f in found) >= 0.8


def test_untrusted_flow_during_a_leak():
    """P-007 flow_out railed (dq_flatline, dq 0.0) during a leak: no mass balance, methane still found."""
    injections = [leak("P-007", T_END - 180), fault("dq_flatline", "P-007", "flow_out", T_END - 180)]
    afw, p1 = annotate(window_at(simulate(T_END - N, T_END, injections), T_END))
    assert [(f.detector_id, f.pipe_id) for f in p1] == [("dq_flatline", "P-007")]
    assert afw.pipes["P-007"].dq["flow_out"] < DQ_MIN
    assert brief(pass2(afw)) == [("leak_methane_rise", "P-007")]


def test_untrusted_corroboration_during_a_leak():
    """P-007 pressure_out railed during a leak: the mass-balance finding is made and names the exclusion."""
    injections = [leak("P-007", T_END - 180), fault("dq_flatline", "P-007", "pressure_out", T_END - 180)]
    afw, _ = annotate(window_at(simulate(T_END - N, T_END, injections), T_END))
    assert afw.pipes["P-007"].dq["pressure_out"] < DQ_MIN
    found = pass2(afw)
    assert brief(found) == [("leak_mass_balance", "P-007"), ("leak_methane_rise", "P-007")]
    (mb,) = [f for f in found if f.detector_id == "leak_mass_balance"]
    assert mb.confidence >= 0.8
    assert "pressure_out was excluded as untrusted (dq 0.00 < dq_min 0.5)" in mb.explanation
    assert METAS["P-007"].aliases["pressure_out"] not in mb.evidence_aliases


# -- review fixes ------------------------------------------------------------------------------------


def _pipe_with(pipe_id, **changes):
    """METAS with ``pipe_id``'s PipeMeta changed (aliases / contracts)."""
    return {**METAS, pipe_id: replace(METAS[pipe_id], **changes)}


def test_near_zero_flow_gives_no_mass_balance():
    """A pipe barely flowing: one tiny flow_in reading must not blow up the loss fraction."""
    fw = synthetic({("P-007", "flow_in"): lambda i: 0.01, ("P-007", "flow_out"): lambda i: 0.0})
    assert MB.detect(pipe_window(trusted(fw), "P-007"), T) == []
    # One near-zero flow_in second among normal ones: ratio of sums, no false 100% loss.
    fw = synthetic({("P-007", "flow_in"): lambda i: 1e-9 if i == N - 5 else 20.0, ("P-007", "flow_out"): lambda i: 0.0 if i == N - 5 else 20.0})
    assert MB.detect(pipe_window(trusted(fw), "P-007"), T) == []


def test_negative_flow_gives_no_mass_balance():
    fw = synthetic({("P-007", "flow_in"): lambda i: -20.0, ("P-007", "flow_out"): lambda i: -25.0})
    assert MB.detect(pipe_window(trusted(fw), "P-007"), T) == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_flow_values_are_ignored(bad):
    fw = synthetic({("P-007", "flow_out"): lambda i: bad if i % 2 else 20.0})
    assert MB.detect(pipe_window(trusted(fw), "P-007"), T) == []
    (f,) = MB.detect(pipe_window(trusted(synthetic({("P-007", "flow_out"): lambda i: bad if i % 2 else 18.0})), "P-007"), T)
    assert f.confidence == 1.0 and "nan" not in f.explanation and "inf" not in f.explanation


def test_mismatched_flow_units_give_no_mass_balance():
    p = METAS["P-007"]
    contracts = {**p.contracts, "flow_out": replace(p.contracts["flow_out"], unit="t/h")}
    fw = leaking()
    fw = trusted(FacilityWindow(pipes=_pipe_with("P-007", contracts=MappingProxyType(contracts)), window=fw.window))
    assert MB.detect(pipe_window(fw, "P-007"), T) == []


def test_mismatched_peer_methane_unit_gives_no_methane_finding():
    peer = next(pid for pid, p in METAS.items() if p.peer_group == METAS["P-007"].peer_group and pid != "P-007")
    q = METAS[peer]
    contracts = {**q.contracts, "methane_ppm": replace(q.contracts["methane_ppm"], unit="mg/m3")}
    fw = leaking()
    fw = trusted(FacilityWindow(pipes=_pipe_with(peer, contracts=MappingProxyType(contracts)), window=fw.window))
    assert MR.detect(fw, T) == []


def test_untrusted_pressure_out_is_named_without_pressure_in():
    p = METAS["P-007"]
    aliases = {m: a for m, a in p.aliases.items() if m != "pressure_in"}
    fw = leaking()
    fw = trusted(FacilityWindow(pipes=_pipe_with("P-007", aliases=MappingProxyType(aliases)), window=fw.window), {("P-007", "pressure_out"): 0.1})
    (f,) = MB.detect(pipe_window(fw, "P-007"), T)
    assert "pressure_out was excluded as untrusted (dq 0.10 < dq_min 0.5)" in f.explanation


def test_peers_with_too_little_data_are_named():
    group = sorted(pid for pid, p in METAS.items() if p.peer_group == METAS["P-007"].peer_group and pid != "P-007")
    fw = leaking()
    series = dict(fw.window.series)
    series[METAS[group[2]].aliases["methane_ppm"]] = series[METAS[group[2]].aliases["methane_ppm"]][:-60]
    (f,) = MR.detect(trusted(FacilityWindow(fw.pipes, Window(fw.window.start, fw.window.end, series))), T)
    assert f.explanation.endswith(f"median of its 4 peer pipes ({group[2]} had too little data).")


def test_pressure_baseline_too_short_is_not_cited():
    """Fewer than min_points pressure samples before the trailing span: finding with the two flow aliases only."""
    fw = leaking()
    a = METAS["P-007"].aliases
    series = dict(fw.window.series)
    for m in ("pressure_in", "pressure_out"):
        series[a[m]] = series[a[m]][-(60 + int(T["detectors"]["leak_mass_balance"]["min_points"]) - 1) :]
    (f,) = MB.detect(pipe_window(trusted(FacilityWindow(fw.pipes, Window(fw.window.start, fw.window.end, series))), "P-007"), T)
    assert f.evidence_aliases == (a["flow_in"], a["flow_out"]) and "pressure" not in f.explanation
