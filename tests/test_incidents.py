"""Story 3.3: one explainable incident per pipe (AD-6, AD-12, AD-13), against a fake DynamoDB with real
condition and transaction semantics (``tests/detect_fakes.FakeDynamoDB``). Every row of the plan's matrix
except Live."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from detect import handler
from detect.adapters.incidents import Incidents, iso, serialize, to_item
from detect.core import incident_rules as rules
from detect.core.incident_rules import DetectorState, Incident
from detect.core.models import Finding
from facility.model import DEFAULT_PATH, load
from tests.detect_fakes import FakeDynamoDB, FakeSiteWise
from tests.test_detect_handler import END, SCHEDULED, STAGE, leak_store, runtime

TABLE = "incidents-test"
T = {"quiet_runs_to_close": 2, "reopen_grace_s": 600}
PIPES = {"P-007": "train1", "P-008": "train1"}
BASE = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)  # window_end of run 0
ALL = {"leak_mass_balance", "leak_methane_rise", "outlier_peer", "dq_flatline", "dq_gap"}
A = {m: f"/lng1/train1/P-007/{m}" for m in ("flow_in", "flow_out", "methane_ppm", "pressure_in", "pressure_out")}


def end_of(k: int) -> datetime:
    return BASE + timedelta(minutes=k)


def run_id_of(k: int) -> str:
    return iso(end_of(k) + timedelta(seconds=77))  # scheduled_time: window_end + ingest lag + 17 s


def F(det: str, c: float, aliases, k: int, pipe: str = "P-007") -> Finding:
    end = end_of(k)
    return Finding(det, "1", pipe, tuple(aliases), end - timedelta(minutes=15), end, c, f"{det} saw something.")


class Rig:
    """An ``Incidents`` adapter over a ``FakeDynamoDB``, stepped one run (minute ``k``) at a time."""

    def __init__(self, tunables=T):
        self.db = FakeDynamoDB(TABLE, page=2)
        self.logs: list[str] = []
        self.ids = iter(f"01ID{i:022d}" for i in range(100))
        self.inc = Incidents(self.db, TABLE, new_id=lambda now: next(self.ids), log=self.logs.append)
        self.t = tunables

    def run(self, k: int, findings=(), quiet_ok=None, run_id=None):
        return self.inc.apply(
            run_id=run_id or run_id_of(k),
            now=end_of(k),
            pipes=PIPES,
            findings=list(findings),
            quiet_ok={p: ALL for p in PIPES} if quiet_ok is None else quiet_ok,
            tunables=self.t,
            config_hash="hash-1",
        )

    def items(self, pipe="P-007"):
        return {sk: v for (pk, sk), v in self.db.plain().items() if pk == pipe}


def three(k: int):
    return [
        F("leak_mass_balance", 0.8, [A["flow_out"], A["flow_in"]], k),
        F("leak_methane_rise", 1.0, [A["methane_ppm"]], k),
        F("outlier_peer", 0.6, [A["methane_ppm"], A["flow_in"]], k),
    ]


# -- Open -------------------------------------------------------------------------------------------


def test_open_one_incident_with_three_detectors():
    rig = Rig()
    res = rig.run(0, three(0))
    items = rig.items()
    assert set(items) == {"OPEN"} and rig.items("P-008") == {}
    it = items["OPEN"]
    assert it["status"] == "OPEN" and it["pipe_id"] == "P-007" and it["area"] == "train1"
    assert set(it["detectors"]) == {"leak_mass_balance", "leak_methane_rise", "outlier_peer"}
    assert it["confidence"] == Decimal("1.0")
    assert it["evidence_aliases"] == sorted({A["flow_in"], A["flow_out"], A["methane_ppm"]})
    assert it["evidence_from"] == iso(end_of(0) - timedelta(minutes=15)) and it["evidence_to"] == iso(end_of(0))
    assert it["opened_at"] == it["last_seen"] == iso(end_of(0))
    assert it["last_run"] == run_id_of(0) and it["config_hash"] == "hash-1"
    assert "notified_at" not in it and "closed_at" not in it
    d = it["detectors"]["leak_mass_balance"]
    assert d == {
        "confidence": Decimal("0.8"),
        "explanation": "leak_mass_balance saw something.",
        "version": "1",
        "first_seen": iso(end_of(0)),
        "last_seen": iso(end_of(0)),
        "quiet_runs": Decimal(0),
    }
    assert res.ops["opened"] == [{"pipe": "P-007", "incident_id": it["incident_id"]}]
    assert res.ops["updated"] == res.ops["closed"] == res.ops["reopened"] == []
    assert res.open["P-008"] is None
    inc = res.open["P-007"]
    assert rules.alert_state(inc) == 3 and rules.alert_state(None) == 0
    assert json.loads(rules.alert_summary(inc)) == {
        "incident_id": it["incident_id"],
        "confidence": 1.0,
        "detectors": [{"id": "leak_mass_balance", "c": 0.8}, {"id": "leak_methane_rise", "c": 1.0}, {"id": "outlier_peer", "c": 0.6}],
        "evidence": sorted({A["flow_in"], A["flow_out"], A["methane_ppm"]}),
    }
    assert rules.alert_summary(None) == "{}"
    # One write: the conditional put of the slot.
    assert [op for op, _ in rig.db.writes] == ["put_item"]


# -- Grow -------------------------------------------------------------------------------------------


def test_grow_keeps_id_widens_evidence_to_and_fixes_evidence_from():
    rig = Rig(tunables={**T, "quiet_runs_to_close": 5})
    rig.run(0, three(0))
    first = rig.items()["OPEN"]
    res = rig.run(1, [F("leak_mass_balance", 0.9, [A["pressure_out"]], 1), F("dq_flatline", 0.5, [A["pressure_in"]], 1)])
    it = rig.items()["OPEN"]
    assert it["incident_id"] == first["incident_id"] and it["opened_at"] == first["opened_at"]
    assert it["evidence_from"] == first["evidence_from"]
    assert it["evidence_to"] == iso(end_of(1)) > first["evidence_to"]
    assert it["evidence_aliases"] == sorted(set(first["evidence_aliases"]) | {A["pressure_out"], A["pressure_in"]})
    mb = it["detectors"]["leak_mass_balance"]
    assert mb["confidence"] == Decimal("0.9") and mb["first_seen"] == iso(end_of(0)) and mb["last_seen"] == iso(end_of(1))
    assert it["detectors"]["leak_methane_rise"]["quiet_runs"] == 1 and it["detectors"]["dq_flatline"]["quiet_runs"] == 0
    assert it["confidence"] == Decimal("1.0") and it["last_run"] == run_id_of(1)
    assert res.ops["updated"] == [{"pipe": "P-007", "incident_id": it["incident_id"]}]
    assert rules.alert_state(res.open["P-007"]) == 4


# -- Quiet close ------------------------------------------------------------------------------------


def test_quiet_close_after_quiet_runs_to_close():
    rig = Rig()
    rig.run(0, three(0))
    incident_id = rig.items()["OPEN"]["incident_id"]
    res = rig.run(1)
    assert {d["quiet_runs"] for d in rig.items()["OPEN"]["detectors"].values()} == {1}
    assert res.ops["updated"] and rules.alert_state(res.open["P-007"]) == 3
    res = rig.run(2)
    items = rig.items()
    assert set(items) == {f"C#{incident_id}"}
    c = items[f"C#{incident_id}"]
    assert c["status"] == "CLOSED" and c["closed_reason"] == "quiet" and c["closed_at"] == iso(end_of(2))
    assert c["last_run"] == run_id_of(2) and c["incident_id"] == incident_id
    assert res.ops["closed"] == [{"pipe": "P-007", "incident_id": incident_id}]
    assert res.open["P-007"] is None
    assert rules.alert_state(res.open["P-007"]) == 0 and rules.alert_summary(res.open["P-007"]) == "{}"
    op, tx = rig.db.writes[-1]
    assert op == "transact_write_items" and [next(iter(t)) for t in tx] == ["Delete", "Put"]
    # Quiet afterwards: nothing more is written.
    n = len(rig.db.writes)
    rig.run(3)
    assert len(rig.db.writes) == n


def test_close_needs_every_detector_quiet():
    rig = Rig()
    rig.run(0, three(0))
    rig.run(1, [F("outlier_peer", 0.5, [A["methane_ppm"]], 1)])
    rig.run(2)
    assert "OPEN" in rig.items()  # outlier_peer only 1 quiet run
    rig.run(3)
    assert "OPEN" not in rig.items()


# -- Reopen / Reset-closed --------------------------------------------------------------------------


def _open_then_close(rig):
    rig.run(0, three(0))
    rig.db.items[("P-007", "OPEN")]["notified_at"] = {"S": "2026-09-29T01:00:30.000Z"}  # as the notifier would (3.4)
    rig.run(1)
    rig.run(2)
    (sk,) = rig.items()
    return sk[2:]


def test_reopen_within_grace_restores_the_incident():
    rig = Rig()
    incident_id = _open_then_close(rig)
    res = rig.run(10, [F("leak_methane_rise", 0.9, [A["methane_ppm"]], 10)])  # 8 min after the close
    items = rig.items()
    assert set(items) == {"OPEN"}  # the C# item is restored, not copied
    it = items["OPEN"]
    assert it["incident_id"] == incident_id and it["status"] == "OPEN"
    assert it["notified_at"] == "2026-09-29T01:00:30.000Z"
    assert "closed_at" not in it and "closed_reason" not in it
    assert it["opened_at"] == iso(end_of(0)) and it["evidence_from"] == iso(end_of(0) - timedelta(minutes=15))
    assert it["detectors"]["leak_methane_rise"]["quiet_runs"] == 0
    assert it["detectors"]["leak_methane_rise"]["first_seen"] == iso(end_of(0))
    assert res.ops["reopened"] == [{"pipe": "P-007", "incident_id": incident_id}]
    assert rules.alert_state(res.open["P-007"]) == 3
    # And it can close again.
    rig.run(11)
    rig.run(12)
    assert set(rig.items()) == {f"C#{incident_id}"}


def test_after_grace_a_new_incident_opens():
    rig = Rig()
    old = _open_then_close(rig)
    res = rig.run(13, [F("leak_methane_rise", 0.9, [A["methane_ppm"]], 13)])  # 11 min > 600 s
    items = rig.items()
    new = items["OPEN"]["incident_id"]
    assert new != old and f"C#{old}" in items and "notified_at" not in items["OPEN"]
    assert res.ops["opened"] == [{"pipe": "P-007", "incident_id": new}]


def test_reset_closed_never_reopens():
    rig = Rig()
    reset = Incident(
        incident_id="01RESET",
        pipe_id="P-007",
        area="train1",
        status=rules.CLOSED,
        opened_at=end_of(0),
        last_seen=end_of(0),
        evidence_from=end_of(0),
        evidence_to=end_of(0),
        evidence_aliases=(A["methane_ppm"],),
        detectors={"leak_methane_rise": DetectorState(1.0, "x.", "1", end_of(0), end_of(0), 0)},
        confidence=1.0,
        notified_at="2026-09-29T01:00:30.000Z",
        closed_at=end_of(1),
        closed_reason=rules.RESET,
    )
    rig.db.items[("P-007", "C#01RESET")] = serialize(to_item(reset, run_id_of(1), "hash-1"))
    res = rig.run(2, [F("leak_methane_rise", 0.9, [A["methane_ppm"]], 2)])  # within grace of the reset
    items = rig.items()
    assert items["OPEN"]["incident_id"] != "01RESET" and "C#01RESET" in items
    assert res.ops["opened"] and not res.ops["reopened"]


# -- Replay (AD-12) ---------------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["open", "close"])
def test_replayed_run_makes_no_incident_writes(step):
    rig = Rig()
    plan = {
        "open": [(0, three(0))],
        "update": [(0, three(0)), (1, [])],
        "close": [(0, three(0)), (1, []), (2, [])],
        "reopen": [(0, three(0)), (1, []), (2, []), (4, [F("outlier_peer", 0.9, [A["flow_in"]], 4)])],
    }[step]
    for k, fs in plan:
        res = rig.run(k, fs)
    k, fs = plan[-1]
    before, n = rig.db.plain(), len(rig.db.writes)
    again = rig.run(k, fs)
    assert len(rig.db.writes) == n and rig.db.plain() == before
    assert all(v == [] for v in again.ops.values())
    assert rules.alert_state(again.open["P-007"]) == rules.alert_state(res.open["P-007"])
    assert rules.alert_summary(again.open["P-007"]) == rules.alert_summary(res.open["P-007"])
    # An older run arriving late is refused too.
    rig.run(k - 1 if k else 0, three(0), run_id=run_id_of(-1))
    assert len(rig.db.writes) == n


def test_every_write_is_conditional_on_last_run():
    rig = Rig()
    for k, fs in [(0, three(0)), (1, []), (2, []), (4, three(4))]:
        rig.run(k, fs)
    for op, body in rig.db.writes:
        parts = body if op == "transact_write_items" else [{"Put": body}]
        for p in parts:
            ((kind, b),) = p.items()
            if kind == "Put":
                assert b["Item"]["last_run"]["S"].startswith("2026-")
            cond = b.get("ConditionExpression") or ""
            assert "#lr < :run_id" in cond or cond == "attribute_not_exists(pk)"


# -- Healthy ----------------------------------------------------------------------------------------


def test_healthy_run_writes_nothing():
    rig = Rig()
    res = rig.run(0)
    assert rig.db.items == {} and rig.db.writes == []
    assert res.open == {"P-007": None, "P-008": None} and all(v == [] for v in res.ops.values())


# -- through the handler ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def facility():
    return load(DEFAULT_PATH, stage=STAGE)


def alerts(facility, fake):
    t = int(END.timestamp())
    return {
        p.id: (
            fake.stored[(facility.derived_alias(p.id, "alert_state"), t)],
            fake.stored[(facility.derived_alias(p.id, "alert_summary"), t)],
        )
        for p in facility.pipes
    }


def hrun(facility, fake, db, scheduled=SCHEDULED):
    logs = []
    rt = runtime(facility, fake, db=db)
    return handler.run(rt, handler.parse_scheduled_time({"scheduled_time": scheduled}), log=logs.append), logs


def test_leak_opens_one_incident_and_alert_state_through_the_handler(facility):
    fake, db = FakeSiteWise(leak_store(facility), page=20000), FakeDynamoDB("incidents-poc-dj")
    summary, logs = hrun(facility, fake, db)
    ((key, item),) = db.plain().items()
    assert key == ("P-007", "OPEN")
    assert set(item["detectors"]) == {"leak_mass_balance", "leak_methane_rise", "outlier_peer"}
    assert item["config_hash"] == facility.config_hash() and item["last_run"] == summary["run_id"]
    assert item["evidence_to"] == summary["window_end"]
    assert summary["incident_ops"]["opened"] == [{"pipe": "P-007", "incident_id": item["incident_id"]}]
    assert summary["alert_state"] == {"P-007": 3}
    values = alerts(facility, fake)
    state, text = values["P-007"]
    assert state == {"integerValue": 3}
    doc = json.loads(text["stringValue"])
    assert doc["incident_id"] == item["incident_id"] and doc["confidence"] == 1.0
    assert [d["id"] for d in doc["detectors"]] == ["leak_mass_balance", "leak_methane_rise", "outlier_peer"]
    assert doc["evidence"] == item["evidence_aliases"]
    assert all(v == ({"integerValue": 0}, {"stringValue": "{}"}) for p, v in values.items() if p != "P-007")
    assert summary["values_written"] == 288 + 48 and len(logs) == 1

    # Replay: no incident writes, no alert write for the replayed pipe, identical derived values.
    n, stored, calls = len(db.writes), dict(fake.stored), len(fake.calls)
    again, _ = hrun(facility, fake, db)
    assert len(db.writes) == n and fake.stored == stored
    assert all(v == [] for v in again["incident_ops"].values()) and again["alert_state"] == {"P-007": 3}
    sent = {e["propertyAlias"] for c in fake.calls[calls:] for e in c}
    assert facility.derived_alias("P-007", "alert_state") not in sent
    assert facility.derived_alias("P-007", "alert_summary") not in sent
    assert facility.derived_alias("P-008", "alert_state") in sent
    assert again["values_written"] == 288 + 46
