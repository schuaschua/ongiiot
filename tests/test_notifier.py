"""Story 3.4: one email per incident through the ``notified_at`` outbox (AD-7; Email v1), against the fake
DynamoDB (real condition semantics) and a fake SNS. The main rows of the plan's matrix (not Live)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from detect.adapters.notifier import BANNER, Notifier
from tests.detect_fakes import FakeSNS
from tests.test_incidents import A, F, Rig, _open_then_close, run_id_of, three

TOPIC = "arn:aws:sns:ap-southeast-1:123456789012:operators-test"
URL = "https://g-abc.grafana-workspace.ap-southeast-1.amazonaws.com"
WHEN = datetime(2026, 9, 29, 1, 2, 3, tzinfo=UTC)


class NRig(Rig):
    def __init__(self, sns=None):
        super().__init__()
        self.sns = sns or FakeSNS()
        self.nlogs: list[str] = []
        self.notifier = Notifier(self.sns, TOPIC, self.inc, URL, "pipe-detail-dj", clock=lambda: WHEN, log=self.nlogs.append)

    def step(self, k, findings=()):
        applied = self.run(k, findings)
        return applied, self.notifier.notify(applied.open.values(), run_id=f"run-{k}")


def test_new_incident_one_email_then_notified_at():
    rig = NRig()
    applied, res = rig.step(0, three(0))
    inc = applied.open["P-007"]
    (msg,) = rig.sns.published
    assert msg["TopicArn"] == TOPIC
    assert msg["Subject"] == f"[POC] Pipe P-007 incident {inc.incident_id}"
    assert res.notified == [{"pipe": "P-007", "incident_id": inc.incident_id, "message_id": "msg-1"}]
    assert res.failures == []
    assert rig.items()["OPEN"]["notified_at"] == "2026-09-29T01:02:03.000Z"
    # the outbox mark does not touch last_run: the next run still updates the slot
    assert rig.items()["OPEN"]["last_run"] == run_id_of(0)


def test_email_v1_body():
    rig = NRig()
    applied, _ = rig.step(0, three(0))
    inc = applied.open["P-007"]
    lines = rig.sns.published[0]["Message"].split("\n")
    assert lines[0] == BANNER == "POC - simulated data - evidence only, not an operational alert or official record"
    assert lines[1] == "Pipe: P-007 (area train1)"
    assert lines[2] == "Incident confidence: 1.00"
    assert lines[3] == "Detectors:"
    assert lines[4:7] == [
        "- leak_mass_balance: confidence 0.80 - leak_mass_balance saw something.",
        "- leak_methane_rise: confidence 1.00 - leak_methane_rise saw something.",
        "- outlier_peer: confidence 0.60 - outlier_peer saw something.",
    ]
    assert lines[7] == "Contributing sensors: " + ", ".join(sorted({A["flow_in"], A["flow_out"], A["methane_ppm"]}))
    assert lines[8] == f"Evidence in Grafana: {rig.notifier.link(inc)}"
    assert lines[8].split(": ", 1)[1].startswith(f"{URL}/d/pipe-detail-dj/pipe-detail?orgId=1&var-pipe_id=P-007&")
    assert len(lines) == 9


def test_publish_failure_is_logged_and_retried_next_run():
    rig = NRig(FakeSNS(fail="InternalErrorException"))
    applied, res = rig.step(0, three(0))
    inc_id = applied.open["P-007"].incident_id
    assert res.notified == [] and "notified_at" not in rig.items()["OPEN"]
    (fail,) = res.failures
    assert fail["pipe"] == "P-007" and fail["incident_id"] == inc_id and fail["step"] == "publish"
    assert "InternalErrorException" in fail["error"]
    assert json.loads(rig.nlogs[0])["level"] == "WARNING"
    rig.sns.fail = None
    _, res = rig.step(1, three(1))
    assert [n["incident_id"] for n in res.notified] == [inc_id] and len(rig.sns.published) == 1
    assert rig.items()["OPEN"]["notified_at"]


def test_race_mark_fails_and_next_run_does_not_publish():
    rig = NRig()
    applied = rig.run(0, three(0))

    def other_writer(db, op):
        assert op == "update_item"
        db.items[("P-007", "OPEN")]["notified_at"] = {"S": "2026-09-29T01:00:59.000Z"}

    rig.db.before_write = other_writer
    res = rig.notifier.notify(applied.open.values())
    assert len(rig.sns.published) == 1 and res.notified == []
    (fail,) = res.failures
    assert fail["step"] == "mark" and fail["message_id"] == "msg-1"
    assert rig.items()["OPEN"]["notified_at"] == "2026-09-29T01:00:59.000Z"
    _, res = rig.step(1, three(1))
    assert res.notified == [] and res.failures == [] and len(rig.sns.published) == 1


def test_reopened_incident_sends_no_new_email():
    rig = NRig()
    incident_id = _open_then_close(rig)  # notified_at set while open
    applied, res = rig.step(10, [F("leak_methane_rise", 0.9, [A["methane_ppm"]], 10)])
    assert applied.ops["reopened"] == [{"pipe": "P-007", "incident_id": incident_id}]
    assert res.notified == [] and rig.sns.published == []
