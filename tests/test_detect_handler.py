"""Full detection runs with fakes: the "Normal run" and "No data" rows (Story 2.1; replay is checked
through the handler in test_incidents.py), railed and stuck sensors end to end (Story 2.2), and pass 2 (Story 3.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from detect import handler
from detect.adapters.derived_writer import DerivedWriter
from detect.adapters.incidents import Incidents
from detect.adapters.reader import make_reader
from detect.core.models import Finding
from facility.model import DEFAULT_PATH, load
from tests.detect_fakes import FakeDynamoDB, FakeSiteWise, FakeSNS

STAGE = "poc-dj"
ALL_IDS = ["dq_flatline", "dq_stuck", "dq_drift", "dq_gap", "leak_mass_balance", "leak_methane_rise", "outlier_peer"]
SCHEDULED = "2026-09-28T12:34:17Z"
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)
START = datetime(2026, 9, 28, 12, 18, tzinfo=UTC)


@pytest.fixture(scope="module")
def facility():
    return load(DEFAULT_PATH, stage=STAGE)


def live_value(contract, t):
    """Healthy synthetic reading: varies every second, mid-range of the contract (off both rails)."""
    return (contract.min + contract.max) / 2 + (t % 10) * (contract.max - contract.min) * 1e-4


def live_methane(t):
    """Healthy background methane, the same on every pipe (contract mid-ranges differ between pipes)."""
    return 2.0 + (t % 10) * 1e-2


def live_temperature(t):
    """Healthy temperature, the same on every pipe (P-004 and P-016 have their own contract, so a
    mid-range value would make them peer outliers)."""
    return 225.0 + (t % 10) * 0.035


LIVE_SAME = {"methane_ppm": live_methane, "temperature": live_temperature}


def live_store(facility, lo=START.timestamp() - 120, hi=END.timestamp() + 60):
    return {
        facility.alias(p.id, m): [
            (t, LIVE_SAME[m](t) if m in LIVE_SAME else live_value(p.contracts[m], t), "GOOD") for t in range(int(lo), int(hi))
        ]
        for p in facility.pipes
        for m in p.sensors
    }


TABLE = f"incidents-{STAGE}"
TOPIC = "arn:aws:sns:ap-southeast-1:123456789012:operators-poc-dj"
NOTIFY_ENV = {"OPERATOR_TOPIC_ARN": TOPIC, "GRAFANA_URL": "https://g-1.grafana-workspace.ap-southeast-1.amazonaws.com", "DASHBOARD_UID": "pipe-detail-dj"}
ENV = {"STAGE": STAGE, "INCIDENTS_TABLE": TABLE, **NOTIFY_ENV}


def runtime(facility, fake, strategy="batch_get", db=None):
    return handler.Runtime(
        facility=facility,
        pipes=handler.pipe_metas(facility),
        dq_targets=handler.dq_aliases(facility),
        reader=make_reader(strategy, fake),
        writer=DerivedWriter(fake, allowed=handler.derived_targets(facility), types=handler.derived_types(facility)),
        incidents=Incidents(db if db is not None else FakeDynamoDB(TABLE), TABLE),
        alert_targets=handler.alert_aliases(facility),
    )


def run(facility, fake, strategy="batch_get", scheduled=SCHEDULED, db=None):
    logs = []
    summary = handler.run(
        runtime(facility, fake, strategy, db=db), handler.parse_scheduled_time({"scheduled_time": scheduled}), log=logs.append
    )
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


@pytest.mark.parametrize("strategy", ["batch_get"])
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
    assert summary["aliases_no_data"] == [] and summary["values_written"] == 288 + 48
    assert summary["samples"] == 144 * 900
    assert {"read_duration_s", "detectors", "ran_ok", "run_s"} <= set(summary)
    assert summary["ran_ok"] == ALL_IDS and summary["findings"] == []


def test_no_data(facility):
    fake = FakeSiteWise({})
    summary, _ = run(facility, fake)
    assert set(written(facility, fake).values()) == {(0.0, "dq_gap")}
    assert len(summary["aliases_no_data"]) == 144 and summary["values_written"] == 288 + 48


def run_with(facility, fake, detectors, pass2=()):
    """A run with fake pass-1 ``detectors``; pass 2 is off unless ``pass2`` is given."""
    logs = []
    rt = runtime(facility, fake)
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), detectors=detectors, pass2=pass2, log=logs.append)
    return summary, logs


@pytest.mark.parametrize("event", [{"scheduled_time": ""}])
def test_bad_events_are_refused(event):
    with pytest.raises(ValueError):
        handler.parse_scheduled_time(event)


def test_entry_point_emails_once_with_the_stage_link(facility, monkeypatch):
    fake, sns = FakeSiteWise(leak_store(facility), page=20000), FakeSNS()
    monkeypatch.setattr(handler, "_RUNTIME", None)
    handler.runtime(ENV, client=fake, dynamodb=FakeDynamoDB(TABLE), sns=sns)

    class Ctx:
        def get_remaining_time_in_millis(self):
            return 40_000

    summary = handler.handler({"scheduled_time": SCHEDULED}, Ctx())
    monkeypatch.setattr(handler, "_RUNTIME", None)
    (msg,) = sns.published
    assert msg["TopicArn"] == TOPIC and len(summary["notified"]) == 1
    link = msg["Message"].split("\n")[-1].split(": ", 1)[1]
    assert link.startswith(f"{NOTIFY_ENV['GRAFANA_URL']}/d/{NOTIFY_ENV['DASHBOARD_UID']}/")


def test_reset_event_closes_incidents_without_a_detection_run(facility, monkeypatch):
    """Story 4.2: ``{"action": "reset"}`` closes the OPEN incidents (no reads, no SiteWise writes, no email);
    any other action is refused."""
    fake, db, sns = FakeSiteWise(leak_store(facility), page=20000), FakeDynamoDB(TABLE), FakeSNS()
    monkeypatch.setattr(handler, "_RUNTIME", None)
    handler.runtime(ENV, client=fake, dynamodb=db, sns=sns)
    handler.handler({"scheduled_time": SCHEDULED})
    iid = db.plain()[("P-007", "OPEN")]["incident_id"]
    stored, published = dict(fake.stored), list(sns.published)
    summary = handler.handler({"action": "reset"})
    with pytest.raises(ValueError):
        handler.handler({"action": "wipe"})
    monkeypatch.setattr(handler, "_RUNTIME", None)
    assert summary["action"] == "reset" and summary["closed"] == [{"pipe": "P-007", "incident_id": iid}] and summary["conflicts"] == []
    ((key, item),) = db.plain().items()
    assert key == ("P-007", f"C#{iid}") and item["closed_reason"] == "reset" and item["last_run"] == summary["run_id"]
    assert fake.stored == stored and sns.published == published


# -- Story 2.2: dq_flatline and dq_stuck registered in pass 1 ------------------------------------


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
    assert summary["ran_ok"] == ALL_IDS
    assert summary["detectors"] == {d: "1" for d in ALL_IDS}
    assert summary["findings"] == []  # a railed flow_in reads 0: not a leak, because it is untrusted
    assert len(logs) == 1  # no detector errors


def _without(tunables, detector_id, key):
    detectors = {k: dict(v) for k, v in tunables["detectors"].items()}
    if key is None:
        del detectors[detector_id]
    else:
        del detectors[detector_id][key]
    return {**tunables, "detectors": detectors}


@pytest.mark.parametrize(
    ("detector_id", "key"),
    [("dq_flatline", "min_run_s")],
)
def test_missing_detector_tunables_fail_the_cold_start(facility, monkeypatch, detector_id, key):
    import dataclasses

    broken = dataclasses.replace(facility, tunables=_without(facility.tunables, detector_id, key))
    monkeypatch.setattr(handler, "load", lambda path, stage: broken)
    monkeypatch.setattr(handler, "_RUNTIME", None)
    with pytest.raises(ValueError, match=detector_id):
        handler.runtime(ENV, client=FakeSiteWise({}), dynamodb=FakeDynamoDB(TABLE), sns=FakeSNS())
    assert handler._RUNTIME is None


@pytest.mark.parametrize(
    ("detector_id", "patch"),
    [("dq_flatline", {"rel_tol": -1.0})],
)
def test_invalid_detector_tunables_are_refused(facility, detector_id, patch):
    detectors = {k: dict(v) for k, v in facility.tunables["detectors"].items()}
    detectors[detector_id].update(patch)
    with pytest.raises(ValueError, match=detector_id):
        handler.check_detectors((*handler.PASS1_DETECTORS, *handler.PASS2_DETECTORS), {**facility.tunables, "detectors": detectors})


# -- Story 2.3: dq_drift and dq_gap ----------------------------------------------------------------


# -- Story 3.1: pass 2, leak_mass_balance and leak_methane_rise --------------------------------------


def leak_store(facility, pipe_id="P-007", from_s=120, frac=0.10, ppm=600.0, p_drop=0.01):
    """``live_store`` with a leak on ``pipe_id`` for the last ``from_s`` seconds of the window (and after):
    flow_out ``frac`` below flow_in, methane up ``ppm``, pressure_out down ``p_drop`` of pressure_in."""
    store = live_store(facility)
    t0 = int(END.timestamp()) - from_s
    p_in = dict((t, v) for t, v, _ in store[facility.alias(pipe_id, "pressure_in")])
    for m, f in {
        "flow_out": lambda t, v: v * (1 - frac),
        "methane_ppm": lambda t, v: v + ppm,
        "pressure_out": lambda t, v: v - p_drop * p_in[t],
    }.items():
        a = facility.alias(pipe_id, m)
        store[a] = [(t, f(t, v) if t >= t0 else v, q) for t, v, q in store[a]]
    return store


def test_leak_findings_reach_the_summary(facility):
    fake = FakeSiteWise(leak_store(facility), page=20000)
    summary, logs = run(facility, fake)
    assert [(f["detector"], f["pipe"], f["confidence"]) for f in summary["findings"]] == [
        ("leak_mass_balance", "P-007", 1.0),
        ("leak_methane_rise", "P-007", 1.0),
        ("outlier_peer", "P-007", 1.0),
    ]
    mb, mr, op = summary["findings"]
    a = lambda m: facility.alias("P-007", m)  # noqa: E731
    assert mb["evidence_aliases"] == [a("flow_in"), a("flow_out"), a("pressure_in"), a("pressure_out")]
    assert mr["evidence_aliases"] == [a("methane_ppm")]
    assert "below flow_in" in mb["explanation"] and "above the median" in mr["explanation"]
    # The Leak outlier row through the handler: flow ratio and methane, no other pipe.
    assert op["evidence_aliases"] == [a("flow_in"), a("flow_out"), a("methane_ppm")]
    assert op["explanation"].startswith("Unlike its peers in fg-cs-10to15 over the last 60 s: methane_ppm ")
    assert "of peers P-008, P-009, P-019, P-020, P-021" in op["explanation"]
    assert summary["suppressed"] == [] and summary["pass2_skipped"] is False
    assert summary["ran_ok"] == ALL_IDS and len(logs) == 1
    assert json.loads(logs[0])["findings"] == summary["findings"]
    # Pass 2 writes nothing: every sensor keeps full trust.
    assert set(written(facility, fake).values()) == {(1.0, "")}


class SeesDq:
    """Fake pass-2 detector recording the dq it sees, and returning ``findings(fw)``."""

    id, version, kind = "leak_fake", "1", "facility"

    def __init__(self, findings=lambda fw: []):
        self.findings = findings
        self.dq = None

    def detect(self, fw, t):
        self.dq = {pid: dict(p.dq) for pid, p in fw.pipes.items()}
        return self.findings(fw)


def _finding(facility, pipe_id, aliases, c=0.9, explanation="x.", detector_id="leak_fake", n=1):
    return lambda fw: [Finding(detector_id, "1", pipe_id, tuple(aliases), fw.window.start, fw.window.end, c, explanation)] * n


class PipeSeesDq(SeesDq):
    """``SeesDq`` as a pipe detector: returns ``findings(pw)`` only in P-007's window."""

    kind = "pipe"

    def detect(self, pw, t):
        return self.findings(pw) if pw.pipe.pipe_id == "P-007" else []


@pytest.mark.parametrize(
    ("case", "match"),
    [("foreign_alias", "not an expected alias")],
)
@pytest.mark.parametrize("kind", ["pipe"])
def test_malformed_pass2_finding_skips_only_that_detector(facility, case, match, kind):
    p7, p8 = facility.alias("P-007", "flow_in"), facility.alias("P-008", "flow_in")
    bad = {
        "foreign_alias": _finding(facility, "P-007", [p7, p8]),
        "no_evidence": _finding(facility, "P-007", []),
        "confidence_high": _finding(facility, "P-007", [p7], c=1.5),
        "confidence_nan": _finding(facility, "P-007", [p7], c=float("nan")),
        "empty_explanation": _finding(facility, "P-007", [p7], explanation=" "),
        "unknown_pipe": _finding(facility, "P-999", [p7]),
        "dq_id": _finding(facility, "P-007", [p7], detector_id="dq_sneaky"),
        "other_id": _finding(facility, "P-007", [p7], detector_id="leak_mass_balance"),
        "duplicate": _finding(facility, "P-007", [p7], n=2),
    }[case]
    if kind == "pipe" and case in ("unknown_pipe",):
        match = "from the window of P-007"  # a pipe detector naming another pipe
    fake = FakeSiteWise(leak_store(facility), page=20000)
    det = PipeSeesDq(bad) if kind == "pipe" else SeesDq(bad)
    summary, logs = run_with(facility, fake, handler.PASS1_DETECTORS, pass2=[det, *handler.PASS2_DETECTORS])
    assert summary["ran_ok"] == ALL_IDS
    assert [f["pipe"] for f in summary["findings"]] == ["P-007", "P-007", "P-007"]
    err = json.loads(logs[0])
    assert err["detector_id"] == "leak_fake" and match in err["error"]


def test_pass1_failure_skips_pass2(facility):
    """A failed dq_* detector leaves its sensors at 1.0, so pass 2 must not trust them: skipped, with an ERROR line."""

    class Boom:
        id, version, kind = "dq_boom", "1", "pipe"

        def detect(self, pw, t):
            raise RuntimeError("boom")

    fake = FakeSiteWise(leak_store(facility), page=20000)
    summary, logs = run_with(facility, fake, [*handler.PASS1_DETECTORS, Boom()], pass2=handler.PASS2_DETECTORS)
    assert summary["pass2_skipped"] is True and summary["findings"] == [] and summary["suppressed"] == []
    assert summary["ran_ok"] == ALL_IDS[:4]
    err = json.loads(logs[1])
    assert err["level"] == "ERROR" and err["error"] == "pass 2 skipped: pass-1 detector(s) failed" and err["failed"] == ["dq_boom"]
    assert json.loads(logs[-1]) == summary and len(logs) == 3
