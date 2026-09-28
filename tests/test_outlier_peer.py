"""``outlier_peer`` (Story 3.2, pass 2) on synthetic windows and on real simulator physics.

Matrix rows: Healthy, Leak outlier, Peers only, Small group, Untrusted sensor, Shared shift (the Live row
is a deployed check), plus the calibration targets: no finding in healthy 30-minute runs (several seeds
and times of day), and a default leak on P-007 makes P-007 an outlier within two runs of entering the
window, with no other pipe flagged.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

from detect import handler
from detect.core.detectors import OutlierPeer
from detect.core.detectors.outlier_peer import FEATURES
from detect.core.models import FacilityWindow, Window
from tests.test_leak_detectors import (
    DQ_MIN,
    METAS,
    N,
    T,
    T_END,
    annotate,
    fault,
    leak,
    leaking,
    simulate,
    synthetic,
    trusted,
    window_at,
)

OP = OutlierPeer()
GROUP = sorted(pid for pid, p in METAS.items() if p.peer_group == METAS["P-007"].peer_group)
PEERS = [pid for pid in GROUP if pid != "P-007"]  # P-008, P-009, P-019, P-020, P-021
A = METAS["P-007"].aliases


def outliers(fw):
    """``outlier_peer`` findings through the handler's dispatch and pass-2 checks."""
    found = handler._run_detector(OP, fw, T, check=handler.check_pass2_finding)
    handler.check_pass2_set(OP, found)
    return found


def pipes(found):
    return sorted(f.pipe_id for f in found)


# -- synthetic windows -----------------------------------------------------------------------------


def test_healthy_synthetic_window_gives_nothing():
    assert outliers(trusted(synthetic())) == []


def test_leak_outlier_names_features_z_and_peers():
    (f,) = outliers(trusted(leaking()))
    assert f.pipe_id == "P-007" and f.confidence == 1.0 and f.detector_version == "1"
    # The flagged features' raw aliases, in the pipe's sensor order; pressure_ratio (z -3.3) is not flagged.
    assert f.evidence_aliases == (A["flow_in"], A["flow_out"], A["methane_ppm"])
    assert f.explanation == (
        "Unlike its peers in fg-cs-10to15 over the last 60 s: "
        "methane_ppm 602 vs median 2 of peers P-008, P-009, P-019, P-020, P-021, z +600.0; "
        "flow_ratio (flow_out/flow_in) 0.9 vs median 1 of peers P-008, P-009, P-019, P-020, P-021, z -20.0."
    )


@pytest.mark.parametrize(("excess", "expected"), [(3.9, None), (4.0, None), (4.4, 0.1), (6.0, 0.5), (8.0, 1.0), (50.0, 1.0)])
def test_confidence_ramps_from_min_z_to_full_z(excess, expected):
    """Flat synthetic peers: MAD 0, so z = excess / floor_methane_ppm (1.0 ppmv). Flagged only above min_z,
    so no zero-confidence finding at exactly min_z."""
    found = outliers(trusted(synthetic({("P-007", "methane_ppm"): lambda i: 2.0 + excess})))
    if expected is None:
        assert found == []
    else:
        (f,) = found
        assert f.confidence == pytest.approx(expected) and f.evidence_aliases == (A["methane_ppm"],)


def test_pressure_ratio_outlier_cites_both_pressures():
    """pressure_out 3% of pressure_in lower than the peers': ratio 0.9367 vs 0.9667, z = -0.03 / 0.003."""
    (f,) = outliers(trusted(synthetic({("P-007", "pressure_out"): lambda i: 2.9e6 - 0.03 * 3.0e6})))
    assert f.evidence_aliases == (A["pressure_in"], A["pressure_out"]) and f.confidence == 1.0
    assert f"pressure_ratio (pressure_out/pressure_in) 0.9367 vs median 0.9667 of peers {', '.join(PEERS)}, z -10.0" in f.explanation


def test_a_low_value_is_an_outlier_too():
    (f,) = outliers(trusted(synthetic({("P-007", "temperature"): lambda i: 280.0})))
    assert f.evidence_aliases == (A["temperature"],) and "z -7.5" in f.explanation


def test_peers_only_the_leaking_pipe_is_flagged():
    """The Peers only row: the leak pulls neither the median nor the MAD far enough to flag a peer."""
    assert pipes(outliers(trusted(leaking()))) == ["P-007"]


def test_shared_shift_gives_nothing():
    """The whole group shifts together (weather, a process change): the median moves too."""
    shift = {}
    for pid in GROUP:
        shift[(pid, "temperature")] = lambda i: 320.0
        shift[(pid, "methane_ppm")] = lambda i: 400.0
        shift[(pid, "flow_out")] = lambda i: 18.0
        shift[(pid, "pressure_out")] = lambda i: 2.7e6
    assert outliers(trusted(synthetic(shift))) == []


def test_small_group_gives_nothing():
    """Only 3 pipes with trusted data (the pipe itself included): below the AD-14 minimum of 4."""
    dq = {(pid, m): 0.0 for pid in PEERS[:3] for m in A}
    assert outliers(trusted(leaking(), dq)) == []


def test_four_pipes_are_enough():
    dq = {(pid, m): 0.0 for pid in PEERS[:2] for m in A}
    (f,) = outliers(trusted(leaking(), dq))
    assert f.pipe_id == "P-007"
    # Per flagged feature: the peers compared and those left out, with every untrusted sensor named.
    assert (
        f"methane_ppm 602 vs median 2 of peers {', '.join(PEERS[2:])} "
        f"({PEERS[0]} left out: methane_ppm untrusted; {PEERS[1]} left out: methane_ppm untrusted), z +600.0"
    ) in f.explanation
    assert f"flow_ratio (flow_out/flow_in) 0.9 vs median 1 of peers {', '.join(PEERS[2:])} ({PEERS[0]} left out: flow_out, flow_in untrusted;" in f.explanation


def test_untrusted_sensor_skips_only_its_feature_and_says_so():
    """The Untrusted sensor row: P-007 pressure_out untrusted, so no pressure_ratio; the rest is scored."""
    fw = trusted(synthetic({("P-007", "pressure_out"): lambda i: 1.0e6, ("P-007", "methane_ppm"): lambda i: 20.0}), {("P-007", "pressure_out"): 0.2})
    (f,) = outliers(fw)
    assert f.evidence_aliases == (A["methane_ppm"],)
    assert f.explanation.endswith(
        "; pressure_ratio was not scored because pressure_out was excluded as untrusted (dq 0.20 < dq_min 0.5)."
    )
    # Every untrusted sensor of a skipped feature is named, in the explanation and in suppressed.
    fw = trusted(synthetic({("P-007", "methane_ppm"): lambda i: 20.0}), {("P-007", "pressure_out"): 0.2, ("P-007", "pressure_in"): 0.1})
    (f,) = outliers(fw)
    assert f.explanation.endswith(
        "; pressure_ratio was not scored because pressure_out was excluded as untrusted (dq 0.20 < dq_min 0.5) "
        "and pressure_in was excluded as untrusted (dq 0.10 < dq_min 0.5)."
    )
    assert handler._suppressed(OP, fw, T) == [
        {
            "detector": "outlier_peer",
            "pipe": "P-007",
            "reason": "pressure_ratio skipped: pressure_out untrusted (dq 0.20 < dq_min 0.5), pressure_in untrusted (dq 0.10 < dq_min 0.5)",
            "scope": "feature",
        }
    ]
    # dq exactly at dq_min is trusted: the railed pressure_out is then scored and flagged.
    (f,) = outliers(trusted(synthetic({("P-007", "pressure_out"): lambda i: 1.0e6}), {("P-007", "pressure_out"): DQ_MIN}))
    assert f.evidence_aliases == (A["pressure_in"], A["pressure_out"])


def test_untrusted_sensor_alone_gives_no_finding_but_is_reported_suppressed():
    """No outlier on the other features: no finding, and the handler's summary says the feature was skipped."""
    fw = trusted(synthetic({("P-007", "pressure_out"): lambda i: 1.0e6}), {("P-007", "pressure_out"): 0.0})
    assert outliers(fw) == []
    assert handler._suppressed(OP, fw, T) == [
        {"detector": "outlier_peer", "pipe": "P-007", "reason": "pressure_ratio skipped: pressure_out untrusted (dq 0.00 < dq_min 0.5)", "scope": "feature"}
    ]


def test_untrusted_peer_is_left_out_and_named():
    fw = trusted(leaking(), {(PEERS[0], "methane_ppm"): 0.1})
    (f,) = outliers(fw)
    assert f"methane_ppm 602 vs median 2 of peers {', '.join(PEERS[1:])} ({PEERS[0]} left out: methane_ppm untrusted), z" in f.explanation
    assert f"flow_ratio (flow_out/flow_in) 0.9 vs median 1 of peers {', '.join(PEERS)}, z" in f.explanation


def test_an_outlying_untrusted_peer_is_never_flagged():
    fw = trusted(synthetic({(PEERS[0], "temperature"): lambda i: 350.0}), {(PEERS[0], "temperature"): 0.0})
    assert outliers(fw) == []


def test_peer_with_too_little_data_is_named():
    fw = leaking()
    series = dict(fw.window.series)
    a = METAS[PEERS[2]].aliases["methane_ppm"]
    series[a] = series[a][:-40]
    (f,) = outliers(trusted(FacilityWindow(fw.pipes, Window(fw.window.start, fw.window.end, series))))
    assert f"({PEERS[2]} left out: too little data)" in f.explanation


def test_near_zero_inlet_gives_no_ratio():
    """A pipe not flowing: flow_ratio has no value (never a division blow-up), the rest is still scored."""
    fw = trusted(synthetic({("P-007", "flow_in"): lambda i: 0.0, ("P-007", "flow_out"): lambda i: 0.0}))
    assert outliers(fw) == []


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        # Idling below min_den_frac (1%) of the inlet contract span (P-007 flow 0-80 kg/s: 0.8 kg/s).
        ({("P-007", "flow_in"): lambda i: 0.5, ("P-007", "flow_out"): lambda i: 0.05}, "flow_ratio skipped: flow_in too low"),
        # Shut in: pressure_in below 1% of its 0-10 MPa span.
        ({("P-007", "pressure_in"): lambda i: 5.0e4, ("P-007", "pressure_out"): lambda i: 5.0e3}, "pressure_ratio skipped: pressure_in too low"),
        ({("P-007", "flow_out"): lambda i: -5.0}, "flow_ratio skipped: flow_out negative"),
    ],
)
def test_idle_inlet_or_negative_outlet_gives_no_ratio(values, reason):
    """A tiny positive inlet mean would blow the ratio up into a false outlier; a negative outlet mean is no ratio."""
    fw = trusted(synthetic(values))
    assert outliers(fw) == []
    assert OP.skipped(fw, T) == {"P-007": [reason]}


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_values_are_ignored(bad):
    fw = trusted(synthetic({("P-007", "temperature"): lambda i: bad if i % 2 else 295.0}))
    assert outliers(fw) == []


def with_units(fw, changes):
    """``fw`` with contract units changed: ``{(pipe_id, measurement): unit or None}`` (None drops the contract)."""
    metas = dict(fw.pipes)
    for (pid, m), u in changes.items():
        contracts = dict(metas[pid].contracts)
        if u is None:
            contracts.pop(m)
        else:
            contracts[m] = replace(contracts[m], unit=u)
        metas[pid] = replace(metas[pid], contracts=MappingProxyType(contracts))
    return FacilityWindow(pipes=metas, window=fw.window)


def test_mismatched_ratio_units_give_no_ratio():
    fw = trusted(with_units(synthetic({("P-007", "flow_out"): lambda i: 10.0}), {("P-007", "flow_out"): "t/h"}))
    assert outliers(fw) == []
    assert OP.skipped(fw, T) == {"P-007": ["flow_ratio skipped: unit mismatch"]}


def test_ratio_units_are_not_compared_between_peers():
    """A ratio is dimensionless once a pipe's own in/out units match: a peer metering in t/h still counts, and a
    peer whose in/out units differ is left out as a unit mismatch (not "too little data")."""
    fw = trusted(with_units(leaking(), {(PEERS[0], "flow_in"): "t/h", (PEERS[0], "flow_out"): "t/h", (PEERS[1], "flow_out"): "t/h"}))
    (f,) = outliers(fw)
    peers = ", ".join(p for p in PEERS if p != PEERS[1])
    assert f"flow_ratio (flow_out/flow_in) 0.9 vs median 1 of peers {peers} ({PEERS[1]} left out: unit mismatch), z" in f.explanation


def test_peer_with_a_different_unit_is_left_out_and_named():
    fw = trusted(with_units(leaking(), {(PEERS[0], "methane_ppm"): "mg/m3"}))
    (f,) = outliers(fw)
    assert f"methane_ppm 602 vs median 2 of peers {', '.join(PEERS[1:])} ({PEERS[0]} left out: unit mismatch), z" in f.explanation


def test_a_missing_unit_is_not_comparable():
    """P-007 without a methane_ppm contract: methane is compared with no one; flow_ratio still flags the leak."""
    fw = trusted(with_units(leaking(), {("P-007", "methane_ppm"): None}))
    (f,) = outliers(fw)
    assert "methane_ppm" not in f.explanation and f.evidence_aliases == (A["flow_in"], A["flow_out"])


def test_too_few_pipes_is_reported_as_skipped():
    fw = trusted(synthetic(), {(pid, "methane_ppm"): 0.0 for pid in PEERS[:3]})
    assert OP.skipped(fw, T)["P-007"] == ["methane_ppm skipped: only 3 pipes with data"]


@pytest.mark.parametrize("group", ["", None])
def test_a_pipe_without_a_peer_group_is_not_scored(group):
    """No peer_group means no peers: the pipe is skipped, never pooled with other ungrouped pipes."""
    fw = leaking()
    metas = {**fw.pipes, **{pid: replace(fw.pipes[pid], peer_group=group) for pid in GROUP}}
    fw = trusted(FacilityWindow(pipes=metas, window=fw.window))
    assert outliers(fw) == [] and OP.skipped(fw, T) == {}


def test_three_of_six_changed_are_flagged():
    """With 3 of 6 pipes changed, each changed pipe's 5 peers are 3 normal and 2 changed: a normal median, so
    it is flagged. The group is split evenly, so the 3 unchanged pipes are flagged too."""
    changed = GROUP[:3]
    fw = trusted(synthetic({(pid, "temperature"): lambda i: 315.0 for pid in changed}))
    assert pipes(outliers(fw)) == GROUP


def test_four_of_six_changed_mask_each_other():
    """With 4 of 6 changed, each changed pipe's median is a changed one: they hide each other (and the 2
    unchanged pipes now look like the outliers)."""
    changed = GROUP[:4]
    fw = trusted(synthetic({(pid, "temperature"): lambda i: 315.0 for pid in changed}))
    assert pipes(outliers(fw)) == GROUP[4:]


def test_missing_dq_means_untrusted():
    assert outliers(leaking()) == []


def test_features_and_detector_shape():
    assert dict(FEATURES) == {
        "pressure_ratio": ("pressure_out", "pressure_in"),
        "flow_ratio": ("flow_out", "flow_in"),
        "temperature": ("temperature",),
        "methane_ppm": ("methane_ppm",),
    }
    assert (OP.id, OP.version, OP.kind, OP.requires) == ("outlier_peer", "1", "facility", ())


def test_non_finite_dq_is_untrusted_in_the_summary_too():
    """The handler's suppressed list uses the detector's trust rule: a NaN dq is untrusted in both."""
    fw = trusted(synthetic(), {("P-007", "temperature"): float("nan")})
    assert handler._suppressed(OP, fw, T) == [
        {"detector": "outlier_peer", "pipe": "P-007", "reason": "temperature skipped: temperature untrusted (dq none < dq_min 0.5)", "scope": "feature"}
    ]


# -- real simulator physics -------------------------------------------------------------------------

# Several times of day (the ambient cycle, each pipe's temperature phase and AR state differ) and seeds
# (each seed gives every pipe a different steady-state offset from its peers).
HEALTHY_RUNS = [
    (T_END, 0),
    (T_END + 7 * 3600 + 13, 0),
    (T_END + 15 * 3600 + 29, 1),
    (T_END + 11 * 3600, 2),
    (T_END + 3 * 3600 + 41, 3),
    (T_END + 20 * 3600 + 7, 5),
]


@pytest.mark.parametrize(("t_first", "seed"), HEALTHY_RUNS, ids=lambda v: str(v))
def test_healthy_thirty_minute_runs_give_no_outlier(t_first, seed):
    """The Healthy row: 30 consecutive one-minute runs of normal simulator output, no finding on any pipe."""
    ends = [t_first + 60 * k for k in range(30)]
    data = simulate(ends[0] - N, ends[-1], seed=seed)
    for t_end in ends:
        afw, p1 = annotate(window_at(data, t_end))
        assert p1 == [], t_end
        assert outliers(afw) == [], t_end
        assert OP.skipped(afw, T) == {}, t_end  # every feature scored: no pipe idles below min_den_frac


@pytest.mark.parametrize("entered_s", [1, 17, 30, 45, 60])
def test_default_leak_on_p007_is_an_outlier_within_two_runs(entered_s):
    """A default leak in the window ``entered_s`` seconds at the first run: P-007 is an outlier at confidence
    1.0 by the second run, through methane (and flow ratio); no other pipe is flagged in either run."""
    data = simulate(T_END - N, T_END + 60, [leak("P-007", T_END - entered_s)])
    found = []
    for t_end in (T_END, T_END + 60):
        afw, p1 = annotate(window_at(data, t_end))
        assert p1 == []
        found.append(outliers(afw))
        assert pipes(found[-1]) in ([], ["P-007"])
    (f,) = found[1]
    assert f.confidence == 1.0 and A["methane_ppm"] in f.evidence_aliases
    assert "methane_ppm" in f.explanation and ", ".join(PEERS) in f.explanation


@pytest.mark.parametrize("pipe_id", ["P-003", "P-010", "P-016", "P-020"])
def test_default_leak_is_an_outlier_on_every_peer_group(pipe_id):
    afw, _ = annotate(window_at(simulate(T_END - N, T_END, [leak(pipe_id, T_END - 120)]), T_END))
    (f,) = outliers(afw)
    assert f.pipe_id == pipe_id and f.confidence == 1.0


def test_untrusted_pressure_out_during_a_leak():
    """P-007 pressure_out railed (dq_flatline) during a leak: pressure_ratio skipped and named, P-007 still an
    outlier on its other features, no other pipe flagged."""
    injections = [leak("P-007", T_END - 180), fault("dq_flatline", "P-007", "pressure_out", T_END - 180)]
    afw, _ = annotate(window_at(simulate(T_END - N, T_END, injections), T_END))
    assert afw.pipes["P-007"].dq["pressure_out"] < DQ_MIN
    (f,) = outliers(afw)
    assert f.pipe_id == "P-007" and f.confidence == 1.0
    assert "pressure_ratio was not scored because pressure_out was excluded as untrusted (dq 0.00 < dq_min 0.5)" in f.explanation
    assert A["pressure_out"] not in f.evidence_aliases
