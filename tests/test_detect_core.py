"""Pure detection core: window maths (AD-9), dq_score and the gap rule (AD-13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from detect.core import dq_score
from detect.core.models import FacilityWindow, Finding, PipeMeta, Sample, Window
from detect.core.window import floor_minute, window_bounds

T = {"window_len_s": 900, "ingest_lag_s": 60}
START = datetime(2026, 9, 28, 12, 18, tzinfo=UTC)
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)


def at(h, m, s=0, us=0):
    return datetime(2026, 9, 28, h, m, s, us, tzinfo=UTC)


# -- window (AD-9) -------------------------------------------------------------------------------


def test_normal_run_window():
    assert window_bounds(at(12, 34, 17), T) == (START, END)


@pytest.mark.parametrize("t", [at(12, 34), at(12, 34, 0, 1), at(12, 34, 59, 999999)])
def test_any_time_in_the_minute_gives_the_same_window(t):
    assert window_bounds(t, T) == (START, END)


def test_replay_is_identical():
    assert window_bounds(at(12, 34, 17), T) == window_bounds(at(12, 34, 17), T)


def test_window_uses_tunables():
    assert window_bounds(at(12, 34, 17), {"window_len_s": 60, "ingest_lag_s": 0}) == (at(12, 33), at(12, 34))


def test_non_utc_offsets_are_refused():
    with pytest.raises(ValueError):
        floor_minute(datetime(2026, 9, 28, 12, 34))
    with pytest.raises(ValueError):
        floor_minute(datetime(2026, 9, 28, 12, 34, tzinfo=timezone(timedelta(hours=8))))


# -- dq_score (AD-13) ----------------------------------------------------------------------------


def finding(detector_id, confidence, alias="/a/train1/P-001/flow_in", pipe_id="P-001"):
    return Finding(detector_id, "1", pipe_id, (alias,), START, END, confidence, "x.")


def test_no_findings_scores_one():
    assert dq_score.score([]) == (1.0, "")


def test_one_minus_max_confidence_and_sorted_ids():
    dq, failing = dq_score.score([finding("dq_stuck", 0.3), finding("dq_drift", 0.75), finding("dq_stuck", 0.1)])
    assert dq == pytest.approx(0.25)
    assert failing == "dq_drift,dq_stuck"


def test_non_dq_findings_are_ignored():
    assert dq_score.score([finding("leak_mass_balance", 0.9)]) == (1.0, "")


def test_confidence_is_clamped():
    assert dq_score.score([finding("dq_flatline", 1.5)]) == (0.0, "dq_flatline")
    assert dq_score.score([finding("dq_flatline", -0.2)]) == (1.0, "dq_flatline")


def fw(series):
    pipes = {
        "P-001": PipeMeta("P-001", "train1", "s", "m", 1, "g", {"flow_in": "/a/P-001/flow_in", "flow_out": "/a/P-001/flow_out"}, {}),
        "P-002": PipeMeta("P-002", "train1", "s", "m", 1, "g", {"flow_in": "/a/P-002/flow_in"}, {}),
    }
    full = {a: series.get(a, []) for p in pipes.values() for a in p.aliases.values()}
    return FacilityWindow(pipes=pipes, window=Window(START, END, full))


def test_gap_rule_scores_zero_with_dq_gap():
    sample = [Sample(START, 1.0, "GOOD")]
    scores = dq_score.score_facility(fw({"/a/P-001/flow_in": sample, "/a/P-002/flow_in": sample}), [])
    assert scores == {"/a/P-001/flow_in": (1.0, ""), "/a/P-001/flow_out": (0.0, "dq_gap"), "/a/P-002/flow_in": (1.0, "")}


def test_gap_findings_name_one_alias_each():
    gaps = dq_score.gap_findings(fw({}))
    assert [g.evidence_aliases for g in gaps] == [("/a/P-001/flow_in",), ("/a/P-001/flow_out",), ("/a/P-002/flow_in",)]
    assert all(g.detector_id == "dq_gap" and g.confidence == 1.0 and (g.window_start, g.window_end) == (START, END) for g in gaps)


def test_gap_rule_and_a_real_dq_gap_finding_do_not_duplicate():
    w = fw({"/a/P-001/flow_in": [Sample(START, 1.0, "GOOD")], "/a/P-002/flow_in": [Sample(START, 1.0, "GOOD")]})
    scores = dq_score.score_facility(w, [finding("dq_gap", 1.0, "/a/P-001/flow_out"), finding("dq_stuck", 0.4, "/a/P-001/flow_out")])
    assert scores["/a/P-001/flow_out"] == (0.0, "dq_gap,dq_stuck")


def test_findings_route_to_their_alias():
    w = fw({a: [Sample(START, 1.0, "GOOD")] for a in ("/a/P-001/flow_in", "/a/P-001/flow_out", "/a/P-002/flow_in")})
    scores = dq_score.score_facility(w, [finding("dq_drift", 0.2, "/a/P-002/flow_in", "P-002")])
    assert scores["/a/P-002/flow_in"] == (pytest.approx(0.8), "dq_drift")
    assert scores["/a/P-001/flow_in"] == (1.0, "")


def test_dq_finding_must_name_exactly_one_expected_alias():
    w = fw({})
    bad = Finding("dq_drift", "1", "P-001", ("/a/P-001/flow_in", "/a/P-001/flow_out"), START, END, 0.5, "x.")
    with pytest.raises(ValueError, match="exactly one alias"):
        dq_score.score_facility(w, [bad])
    with pytest.raises(ValueError, match="not an expected alias"):
        dq_score.score_facility(w, [finding("dq_drift", 0.5, "/other")])
