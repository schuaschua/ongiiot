"""ops/smoke.py: inject a leak, wait for incident, email and link, resolve the link (Story 4.3, AD-15).

Fakes for the tables, CloudWatch Logs, the Grafana client and API, and a fake clock (no real sleeps)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

from infra.lambdas.grafana_provision.handler import GrafanaError
from ops import smoke
from tests.sim_fakes import FakeTable

ENV = {"STAGE": "poc-dj", "SIM_CONTROL_TABLE": "sim-control-poc-dj"}
T0 = datetime(2026, 9, 29, 2, 0, 0, tzinfo=UTC)
URL = "https://g-abc.grafana-workspace.ap-southeast-1.amazonaws.com"
UID = "pipe-detail-dj"
INC = "01INCIDENT"
LINK = f"{URL}/d/{UID}/pipe-detail?orgId=1&var-pipe_id=P-007&var-area=train1&from=1&to=2"
OUTPUTS = {"SharedGrafanaStack": {"WorkspaceId": "g-abc", "GrafanaUrl": URL}, "GrafanaStack-poc-dj": {"PipeDetailUid": UID}}


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return T0 + timedelta(seconds=self.t)


def ts(clock_s: float) -> str:
    return (T0 + timedelta(seconds=clock_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Incidents:
    """The OPEN slot as a function of fake time: ``script(t) -> item | None``."""

    def __init__(self, clock, script):
        self.clock, self.script = clock, script

    def get_item(self, Key, ConsistentRead):
        assert Key == {"pk": "P-007", "sk": "OPEN"} and ConsistentRead
        item = self.script(self.clock.t)
        return {"Item": item} if item else {}


def chain(opened_at=10.0, open_s=120.0, notified_s=130.0, pk="P-007"):
    def script(t):
        if t < open_s:
            return None
        item = {"pk": pk, "sk": "OPEN", "incident_id": INC, "opened_at": ts(opened_at)}
        if notified_s is not None and t >= notified_s:
            item["notified_at"] = ts(notified_s)
        return item

    return script


class Logs:
    def __init__(self, clock, at_s=140.0, link=LINK):
        self.clock, self.at_s, self.link = clock, at_s, link
        self.calls = []

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        if self.clock.t < self.at_s:
            return {"events": []}
        entry = {"pipe": "P-007", "incident_id": INC, "message_id": "m"} | ({"link": self.link} if self.link else {})
        summary = {"run_id": "r", "notified": [entry]}
        if "nextToken" not in kw:  # first page: an unrelated match, then the summary on page two
            return {"events": [{"message": "START RequestId: x"}], "nextToken": "p2"}
        return {"events": [{"message": json.dumps(summary, separators=(",", ":"))}]}


class GrafanaClient:
    def __init__(self):
        self.tokens: list[str] = []
        self.deleted: list[str] = []

    def list_workspace_service_accounts(self, workspaceId):
        return {"serviceAccounts": [{"name": "ongiiot-provision-poc-dj", "id": "sa-1"}]}

    def create_workspace_service_account_token(self, workspaceId, serviceAccountId, name, secondsToLive):
        assert serviceAccountId == "sa-1" and secondsToLive <= 900
        self.tokens.append("tok-1")
        return {"serviceAccountToken": {"id": "tok-1", "key": "secret"}}

    def delete_workspace_service_account_token(self, workspaceId, serviceAccountId, tokenId):
        self.deleted.append(tokenId)


class Api:
    def __init__(self, status=200, variables=("pipe_id", "area", "alias_root", "evidence")):
        self.status, self.variables = status, variables
        self.calls = []

    def __call__(self, base_url, token):
        assert base_url == URL and token == "secret"
        return self

    def request(self, method, path):
        self.calls.append((method, path))
        if self.status != 200:
            raise GrafanaError(method, path, self.status, "not found")
        return {"dashboard": {"uid": UID, "templating": {"list": [{"name": v} for v in self.variables]}}}


def run(script, logs=None, api=None, clock=None, sleep=None, table=None):
    clock = clock or Clock()
    table = table if table is not None else FakeTable()
    rig = {"clock": clock, "table": table, "logs": logs or Logs(clock), "grafana": GrafanaClient(), "api": api or Api()}
    ids = iter(f"01ID{i}" for i in range(10))
    code = smoke.main(
        ["--stage", "poc-dj"],
        env=ENV,
        table=table,
        incidents=Incidents(clock, script),
        logs=rig["logs"],
        grafana=rig["grafana"],
        api_factory=rig["api"],
        outputs=OUTPUTS.__getitem__,
        clock=clock,
        sleep=sleep or clock.sleep,
        now=clock.now,
        new_id=lambda: next(ids),
    )
    return code, rig


def cancelled(table):
    (inj,) = [i for i in table.items.values() if "supersedes" not in i]
    (cancel,) = [i for i in table.items.values() if "supersedes" in i]
    assert cancel["supersedes"] == inj["pk"] and cancel["start_at"] == cancel["end_at"]
    return inj


def test_pass_prints_each_step_and_cancels(capsys):
    code, rig = run(chain())
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    assert [line.split("] ")[1].split(":")[0] for line in out[:-1]] == ["precheck", "inject", "incident", "email", "link", "link", "cancel"]
    assert out[-1].startswith("PASS") and "ops.reset" in out[-1]
    inj = cancelled(rig["table"])
    assert inj["type"] == "leak" and inj["pipe_id"] == "P-007" and inj["params"] == {}
    assert inj["start_at"] == ts(0) and inj["end_at"] == ts(420)  # budget + 2 minutes
    assert all(s <= smoke.POLL_S for s in rig["clock"].sleeps)
    call = rig["logs"].calls[0]
    assert call["logGroupName"] == "/aws/lambda/detection-poc-dj" and call["filterPattern"] == f'"{INC}"'
    assert rig["api"].calls == [("GET", f"/api/dashboards/uid/{UID}")]
    assert rig["grafana"].deleted == rig["grafana"].tokens == ["tok-1"]


def test_dirty_pipe_fails_precheck_and_injects_nothing(capsys):
    code, rig = run(chain(opened_at=-600, open_s=0))
    assert code == 1 and rig["table"].items == {}
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("FAIL: precheck: P-007 has OPEN incident 01INCIDENT") and "run ops.reset first" in last


@pytest.mark.parametrize(
    ("script", "link", "last"),
    [
        (lambda t: None, LINK, "FAIL: incident: none after 300 s"),
        (chain(opened_at=-5, open_s=30), LINK, "FAIL: incident: none after 300 s"),  # opened before the injection
        (chain(), None, f"FAIL: link: no emailed link for {INC} in /aws/lambda/detection-poc-dj after 300 s"),
    ],
    ids=["no-incident", "stale-incident", "no-link"],
)
def test_wait_times_out(capsys, script, link, last):
    clock = Clock()
    code, rig = run(script, logs=Logs(clock, link=link), clock=clock)
    assert code == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == last
    assert rig["clock"].t == 300
    cancelled(rig["table"])


def test_no_email_times_out(capsys):
    code, rig = run(chain(notified_s=None))
    assert code == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == f"FAIL: email: incident {INC} has no notified_at after 300 s"
    cancelled(rig["table"])


@pytest.mark.parametrize(
    ("api", "link", "reason"),
    [
        (Api(status=404), LINK, f"GET /api/dashboards/uid/{UID} returned 404"),
        (Api(variables=("pipe_id", "alias_root")), LINK, f"dashboard {UID} lacks variables area, evidence"),
        (Api(), LINK.replace(UID, "pipe-detail-poc"), f"link uid 'pipe-detail-poc' is not the stage's pipe-detail uid '{UID}'"),
    ],
    ids=["404", "vars-missing", "wrong-uid"],
)
def test_bad_link_fails_and_deletes_token(capsys, api, link, reason):
    clock = Clock()
    code, rig = run(chain(), logs=Logs(clock, link=link), api=api, clock=clock)
    assert code == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == f"FAIL: link: {reason}"
    assert rig["grafana"].deleted == rig["grafana"].tokens  # every minted token is deleted
    if link != LINK:
        assert api.calls == [] and rig["grafana"].tokens == []  # no Grafana call for a wrong uid
    cancelled(rig["table"])


def test_failed_cancel_fails_and_names_the_command(capsys):
    class CancelFails(FakeTable):
        def put_item(self, Item, ConditionExpression=None):
            if "supersedes" in Item:
                raise ClientError({"Error": {"Code": "InternalServerError", "Message": "boom"}}, "PutItem")
            super().put_item(Item, ConditionExpression)

    code, rig = run(chain(), table=CancelFails())
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.strip().splitlines()[-1].startswith("FAIL: cancel:")
    (inj,) = rig["table"].items.values()
    assert f"ops.inject cancel {inj['pk']}" in captured.err


def test_ctrl_c_still_cancels(capsys):
    clock = Clock()

    def interrupt(_s):
        raise KeyboardInterrupt

    code, rig = run(lambda t: None, clock=clock, sleep=interrupt)
    assert code == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "FAIL: incident: interrupted"
    cancelled(rig["table"])
