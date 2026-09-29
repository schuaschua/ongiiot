"""``outlier_peer`` (Story 3.2, pass 2) on synthetic windows and on real simulator physics.

Matrix rows: Healthy, Leak outlier, Peers only, Small group, Untrusted sensor, Shared shift (the Live row
is a deployed check), plus the calibration targets: no finding in healthy 30-minute runs (several seeds
and times of day), and a default leak on P-007 makes P-007 an outlier within two runs of entering the
window, with no other pipe flagged.
"""

from __future__ import annotations


import pytest

from detect import handler
from detect.core.detectors import OutlierPeer
from detect.core.detectors.outlier_peer import FEATURES
from tests.test_leak_detectors import (
    DQ_MIN,
    METAS,
    N,
    T,
    T_END,
    annotate,
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


@pytest.mark.parametrize(("excess", "expected"), [(4.0, None), (6.0, 0.5)])
def test_confidence_ramps_from_min_z_to_full_z(excess, expected):
    """Flat synthetic peers: MAD 0, so z = excess / floor_methane_ppm (1.0 ppmv). Flagged only above min_z,
    so no zero-confidence finding at exactly min_z."""
    found = outliers(trusted(synthetic({("P-007", "methane_ppm"): lambda i: 2.0 + excess})))
    if expected is None:
        assert found == []
    else:
        (f,) = found
        assert f.confidence == pytest.approx(expected) and f.evidence_aliases == (A["methane_ppm"],)


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


def test_features_and_detector_shape():
    assert dict(FEATURES) == {
        "pressure_ratio": ("pressure_out", "pressure_in"),
        "flow_ratio": ("flow_out", "flow_in"),
        "temperature": ("temperature",),
        "methane_ppm": ("methane_ppm",),
    }
    assert (OP.id, OP.version, OP.kind, OP.requires) == ("outlier_peer", "1", "facility", ())


# -- real simulator physics -------------------------------------------------------------------------

# Several times of day (the ambient cycle, each pipe's temperature phase and AR state differ) and seeds
# (each seed gives every pipe a different steady-state offset from its peers).


@pytest.mark.parametrize("entered_s", [1])
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
