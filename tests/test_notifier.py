"""Story 3.4: one email per incident through the ``notified_at`` outbox (AD-7; Email v1), against the fake
DynamoDB (real condition semantics) and a fake SNS. Every row of the plan's matrix except Live."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from detect import handler
from detect.adapters.notifier import BANNER, Notifier, body, subject
from facility.model import DEFAULT_PATH, load
from tests.detect_fakes import FakeDynamoDB, FakeSiteWise, FakeSNS
from tests.test_detect_handler import SCHEDULED, STAGE, leak_store, runtime
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


def test_already_notified_and_joining_detector_send_nothing():
    rig = NRig()
    rig.step(0, three(0))
    _, res = rig.step(1, three(1))  # same incident, later run
    assert res.notified == [] and len(rig.sns.published) == 1
    _, res = rig.step(2, [*three(2), F("dq_flatline", 1.0, [A["pressure_in"]], 2)])  # a new detector joins
    assert res.notified == [] and len(rig.sns.published) == 1
    assert "dq_flatline" in rig.items()["OPEN"]["detectors"]
    assert rig.items()["OPEN"]["notified_at"] == "2026-09-29T01:02:03.000Z"  # kept through the updates


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


def test_mark_needs_the_same_incident_in_the_slot():
    rig = NRig()
    applied = rig.run(0, three(0))
    stale = replace(applied.open["P-007"], incident_id="01OTHER")
    assert rig.inc.mark_notified("P-007", "01OTHER", WHEN) is False
    res = rig.notifier.notify([stale])
    assert res.failures[0]["step"] == "mark" and "notified_at" not in rig.items()["OPEN"]
    assert rig.inc.mark_notified("P-009", "01OTHER", WHEN) is False  # no slot at all
    assert ("P-009", "OPEN") not in rig.db.items


def test_mark_other_errors_are_recorded_not_raised():
    rig = NRig()
    applied = rig.run(0, three(0))
    rig.db.fail_writes = "ProvisionedThroughputExceededException"
    res = rig.notifier.notify(applied.open.values())
    assert res.failures[0]["step"] == "mark" and "ProvisionedThroughputExceededException" in res.failures[0]["error"]
    with pytest.raises(Exception, match="ProvisionedThroughput"):
        rig.inc.mark_notified("P-007", applied.open["P-007"].incident_id, WHEN)


def test_reopened_incident_sends_no_new_email():
    rig = NRig()
    incident_id = _open_then_close(rig)  # notified_at set while open
    applied, res = rig.step(10, [F("leak_methane_rise", 0.9, [A["methane_ppm"]], 10)])
    assert applied.ops["reopened"] == [{"pipe": "P-007", "incident_id": incident_id}]
    assert res.notified == [] and rig.sns.published == []


def test_closed_and_absent_incidents_are_skipped():
    rig = NRig()
    applied = rig.run(0, three(0))
    inc = applied.open["P-007"]
    res = rig.notifier.notify([None, replace(inc, status="CLOSED")])
    assert res.notified == res.failures == [] and rig.sns.published == []


def test_two_pipes_two_emails_in_pipe_order():
    rig = NRig()
    _, res = rig.step(0, [*three(0), F("leak_methane_rise", 0.7, ["/lng1/train1/P-008/methane_ppm"], 0, pipe="P-008")])
    assert [n["pipe"] for n in res.notified] == ["P-007", "P-008"]
    assert [m["Subject"].split()[2] for m in rig.sns.published] == ["P-007", "P-008"]


def test_notifier_refuses_empty_config():
    rig = Rig()
    for args in (("", URL, "u"), (TOPIC, "", "u"), (TOPIC, URL, "")):
        with pytest.raises(ValueError):
            Notifier(FakeSNS(), args[0], rig.inc, args[1], args[2])


def test_subject_and_body_helpers():
    rig = NRig()
    applied, _ = rig.step(0, three(0))
    inc = applied.open["P-007"]
    assert subject(inc) == rig.sns.published[0]["Subject"] and len(subject(inc)) <= 100  # SNS subject limit
    assert body(inc, "L") == rig.sns.published[0]["Message"].rsplit(": ", 1)[0] + ": L"


# -- through the handler ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def facility():
    return load(DEFAULT_PATH, stage=STAGE)


def test_leak_emails_once_through_the_handler(facility):
    fake, db, sns = FakeSiteWise(leak_store(facility), page=20000), FakeDynamoDB("incidents-poc-dj"), FakeSNS()
    rt = runtime(facility, fake, db=db)
    rt.notifier = Notifier(sns, TOPIC, rt.incidents, URL, "pipe-detail-dj", clock=lambda: WHEN)
    logs: list[str] = []
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), log=logs.append)
    (opened,) = summary["incident_ops"]["opened"]
    assert summary["notified"] == [{**opened, "message_id": "msg-1"}]
    assert summary["notify_failures"] == []
    assert "notify_duration_s" in summary
    assert db.plain()[("P-007", "OPEN")]["notified_at"] == "2026-09-29T01:02:03.000Z"
    # a replay of the same run and the next run send nothing more
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), log=logs.append)
    assert summary["notified"] == [] and len(sns.published) == 1
    assert json.loads(logs[-1])["notified"] == []


def test_handler_without_notifier_reports_empty_lists(facility):
    rt = runtime(facility, FakeSiteWise(leak_store(facility), page=20000), db=FakeDynamoDB("incidents-poc-dj"))
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), log=lambda _: None)
    assert summary["notified"] == [] and summary["notify_failures"] == []


# -- review fixes: render, limits, skip, deadline ----------------------------------------------------


def test_render_error_is_recorded_not_published():
    rig = NRig()
    applied = rig.run(0, three(0))
    bad = replace(applied.open["P-007"], evidence_from=datetime(2026, 9, 29))  # naive: build_link refuses it
    res = rig.notifier.notify([bad])
    (fail,) = res.failures
    assert fail["step"] == "render" and rig.sns.attempts == 0 and "notified_at" not in rig.items()["OPEN"]


def test_subject_is_ascii_one_line_and_at_most_100_chars():
    rig = NRig()
    inc = rig.run(0, three(0)).open["P-007"]
    s = subject(replace(inc, incident_id="é\n" + "X" * 200))
    assert len(s) <= 100 and s.isascii() and "\n" not in s and s.startswith("[POC] Pipe P-007 incident ?")


def test_explanation_newlines_become_spaces():
    rig = NRig()
    inc = rig.run(0, three(0)).open["P-007"]
    det = replace(inc.detectors["outlier_peer"], explanation="line one\nline two\r\nthree")
    text = body(replace(inc, detectors={**inc.detectors, "outlier_peer": det}), "L")
    assert "- outlier_peer: confidence 0.60 - line one line two three" in text.split("\n")


def test_non_dict_publish_response_is_tolerated():
    class OddSNS(FakeSNS):
        def publish(self, **kw):
            super().publish(**kw)
            return None

    rig = NRig(OddSNS())
    _, res = rig.step(0, three(0))
    assert res.notified[0]["message_id"] == "" and rig.items()["OPEN"]["notified_at"]


def test_clock_must_be_tz_aware():
    rig = Rig()
    with pytest.raises(ValueError, match="tz-aware"):
        Notifier(FakeSNS(), TOPIC, rig.inc, URL, "u", clock=lambda: datetime(2026, 9, 29))


def test_skipped_pipes_are_not_notified():
    rig = NRig()
    applied = rig.run(0, three(0))
    res = rig.notifier.notify(applied.open.values(), skip={"P-007"})
    assert res.notified == res.failures == [] and rig.sns.published == []


def test_deadline_stops_publishing():
    rig = NRig()
    applied = rig.run(0, [*three(0), F("leak_methane_rise", 0.7, ["/lng1/train1/P-008/methane_ppm"], 0, pipe="P-008")])
    left = iter([20_000, 9_000])
    res = rig.notifier.notify(applied.open.values(), remaining_ms=lambda: next(left))
    assert [n["pipe"] for n in res.notified] == ["P-007"]
    assert [(f["pipe"], f["step"]) for f in res.failures] == [("P-008", "deadline")]
    assert len(rig.sns.published) == 1 and "notified_at" not in rig.items("P-008")["OPEN"]


def test_handler_publish_failure_run_succeeds(facility):
    db, sns = FakeDynamoDB("incidents-poc-dj"), FakeSNS(fail="InternalErrorException")
    rt = runtime(facility, FakeSiteWise(leak_store(facility), page=20000), db=db)
    rt.notifier = Notifier(sns, TOPIC, rt.incidents, URL, "pipe-detail-dj", clock=lambda: WHEN, log=lambda _: None)
    summary = handler.run(rt, handler.parse_scheduled_time({"scheduled_time": SCHEDULED}), log=lambda _: None)
    assert summary["notified"] == [] and summary["notify_failures"][0]["step"] == "publish"
    assert "notified_at" not in db.plain()[("P-007", "OPEN")]


def test_handler_skips_replayed_pipes(facility):
    db, sns = FakeDynamoDB("incidents-poc-dj"), FakeSNS(fail="InternalErrorException")
    rt = runtime(facility, FakeSiteWise(leak_store(facility), page=20000), db=db)
    rt.notifier = Notifier(sns, TOPIC, rt.incidents, URL, "pipe-detail-dj", clock=lambda: WHEN, log=lambda _: None)
    scheduled = handler.parse_scheduled_time({"scheduled_time": SCHEDULED})
    handler.run(rt, scheduled, log=lambda _: None)
    sns.fail = None
    summary = handler.run(rt, scheduled, log=lambda _: None)  # a replay: P-007 is left untouched, so not notified
    assert summary["notified"] == [] and sns.published == []
