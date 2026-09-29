"""Grafana provisioning handler against a fake Grafana API and a fake AWS client (no network)."""

from __future__ import annotations

import json

import pytest

from facility.model import DEFAULT_PATH, load
from infra.lambdas.grafana_provision import handler
from infra.stacks.grafana_provision import provision_properties

WS = "g-abc123"


class FakeGrafana:
    """In-memory Grafana HTTP API: folders, data sources, dashboards and plugins by path."""

    def __init__(self, plugin_version: str = "2.6.3", install_status: int | None = None) -> None:
        self.folders: dict[str, dict] = {}
        self.datasources: dict[str, dict] = {}
        self.dashboards: dict[str, dict] = {}
        self.plugin_version = plugin_version
        self.install_status = install_status
        self.calls: list[tuple[str, str]] = []

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        parts = path.strip("/").split("/")
        if parts[:2] == ["api", "plugins"]:
            if method == "GET":
                return {"info": {"version": self.plugin_version}}
            if self.install_status:
                raise handler.GrafanaError(method, path, self.install_status, "plugin management disabled")
            self.plugin_version = body["version"]
            return {}
        if parts[:2] == ["api", "folders"]:
            if method == "POST":
                assert body["uid"] not in self.folders, "duplicate folder"
                self.folders[body["uid"]] = body
                return body
            uid = parts[2]
            if uid not in self.folders:
                raise handler.GrafanaError(method, path, 404, "not found")
            if method == "DELETE":
                del self.folders[uid]
                self.dashboards = {k: v for k, v in self.dashboards.items() if v["folderUid"] != uid}
                return {}
            if method == "PUT":
                self.folders[uid] = {"uid": uid, "title": body["title"]}
            return self.folders[uid]
        if parts[:2] == ["api", "datasources"]:
            if method == "POST":
                assert body["uid"] not in self.datasources, "duplicate data source"
                self.datasources[body["uid"]] = body
                return body
            uid = parts[3]
            if uid not in self.datasources:
                raise handler.GrafanaError(method, path, 404, "not found")
            if method == "DELETE":
                del self.datasources[uid]
                return {}
            if method == "PUT":
                self.datasources[uid] = body
            return self.datasources[uid]
        if path == "/api/dashboards/db":
            assert body["overwrite"] is True
            assert body["folderUid"] in self.folders
            self.dashboards[body["dashboard"]["uid"]] = {"dashboard": body["dashboard"], "folderUid": body["folderUid"]}
            return {"status": "success"}
        if parts[:3] == ["api", "dashboards", "uid"]:
            if parts[3] not in self.dashboards:
                raise handler.GrafanaError(method, path, 404, "not found")
            del self.dashboards[parts[3]]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


class FakeAws:
    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.ttls: list[int] = []
        self.deleted_tokens: list[str] = []

    def list_workspace_service_accounts(self, workspaceId, nextToken=None):
        return {"serviceAccounts": [{"id": i, "name": a["name"]} for i, a in self.accounts.items()]}

    def create_workspace_service_account(self, workspaceId, name, grafanaRole):
        assert workspaceId == WS and grafanaRole == "ADMIN"
        sa_id = str(len(self.accounts) + 1)
        self.accounts[sa_id] = {"name": name}
        return {"id": sa_id, "name": name}

    def create_workspace_service_account_token(self, workspaceId, serviceAccountId, name, secondsToLive):
        self.ttls.append(secondsToLive)
        tid = f"t{len(self.ttls)}"
        self.tokens[tid] = {"sa": serviceAccountId}
        return {"serviceAccountToken": {"id": tid, "name": name, "key": f"key-{tid}"}}

    def delete_workspace_service_account_token(self, workspaceId, serviceAccountId, tokenId):
        del self.tokens[tokenId]
        self.deleted_tokens.append(tokenId)

    def delete_workspace_service_account(self, workspaceId, serviceAccountId):
        del self.accounts[serviceAccountId]


def props_for(stage: str) -> dict:
    return provision_properties(load(DEFAULT_PATH, stage=stage), WS, "https://g-abc123.grafana-workspace.example")


def run(request: str, props: dict, aws: FakeAws, grafana: FakeGrafana, physical_id: str | None = None) -> dict:
    event = {"RequestType": request, "ResourceProperties": props}
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    seen = {}

    def factory(url, token):
        seen["url"], seen["token"] = url, token
        return grafana

    result = handler.on_event(event, None, client=aws, api_factory=factory)
    assert seen["url"] == props["GrafanaUrl"]
    assert seen["token"].startswith("key-")
    return result


def test_create_provisions_folder_datasource_and_dashboards():
    aws, grafana = FakeAws(), FakeGrafana()
    result = run("Create", props_for("poc-dj"), aws, grafana)
    assert set(grafana.folders) == {"ongiiot-poc-dj"}
    ds = grafana.datasources["sitewise-poc-dj"]
    assert ds["name"] == "sitewise-poc-dj"
    assert ds["type"] == "grafana-iot-sitewise-datasource"
    assert ds["jsonData"] == {"authType": "ec2_iam_role", "defaultRegion": "ap-southeast-1"}
    assert set(grafana.dashboards) == {"pipe-detail-dj", "facility-overview-dj"}
    assert all(d["folderUid"] == "ongiiot-poc-dj" for d in grafana.dashboards.values())
    assert result["PhysicalResourceId"] == "grafana-provision-poc-dj"
    assert result["Data"]["DashboardUids"] == "facility-overview-dj,pipe-detail-dj"
    assert result["Data"]["PluginVersion"] == "2.6.3" and result["Data"]["PluginPinned"] == "true"


def test_placeholders_filled_per_stage():
    aws, grafana = FakeAws(), FakeGrafana()
    run("Create", props_for("poc-dj"), aws, grafana)
    for entry in grafana.dashboards.values():
        text = json.dumps(entry["dashboard"])
        assert "__" not in text.replace("__url_time_range", "")
        assert "sitewise-poc-dj" in text
    detail = grafana.dashboards["pipe-detail-dj"]["dashboard"]
    (alias_root,) = [v for v in detail["templating"]["list"] if v["name"] == "alias_root"]
    assert alias_root["query"] == "/poc-dj/lng1"
    assert detail["id"] is None


def overview_for(props: dict) -> dict:
    aws, grafana = FakeAws(), FakeGrafana()
    run("Create", props, aws, grafana)
    (uid,) = [u for u in grafana.dashboards if u.startswith("facility-overview")]
    return grafana.dashboards[uid]["dashboard"]


def trust_threshold(overview: dict) -> list:
    values = set()
    for p in overview["panels"]:
        if p["title"].endswith(" trust"):
            steps = p["fieldConfig"]["defaults"]["thresholds"]["steps"]
            assert steps[0] == {"color": "red", "value": None} and steps[1]["color"] == "green"
            values.add(steps[1]["value"])
    return sorted(values)


def test_dq_min_threshold_from_facility_yaml():
    facility = load(DEFAULT_PATH, stage="poc-dj")
    props = props_for("poc-dj")
    assert props["DqMin"] == str(float(facility.tunables["dq_min"]))
    assert trust_threshold(overview_for(props)) == [facility.tunables["dq_min"]]


@pytest.mark.parametrize(("stage", "root", "suffix"), [("poc", "/lng1", "")])
def test_overview_queries_only_own_alias_root(stage, root, suffix):
    overview = overview_for(props_for(stage))
    (alias_root,) = overview["templating"]["list"]
    assert alias_root["name"] == "alias_root" and alias_root["query"] == root
    aliases = [t["propertyAlias"] for p in overview["panels"] for t in p.get("targets", [])]
    assert len(aliases) == 24 * 18 and all(a.startswith("${alias_root}/") for a in aliases)
    assert all(t["datasource"]["uid"] == f"sitewise-{stage}" for p in overview["panels"] for t in p.get("targets", []))
    urls = [p["links"][0]["url"] for p in overview["panels"] if p.get("links")]
    assert len(urls) == 24 * 3 and all(u.startswith(f"/d/pipe-detail{suffix}/pipe-detail?") for u in urls)


def test_rerun_is_idempotent():
    aws, grafana = FakeAws(), FakeGrafana()
    props = props_for("poc-dj")
    run("Create", props, aws, grafana)
    snapshot = json.dumps([grafana.folders, grafana.datasources, grafana.dashboards], sort_keys=True)
    run("Update", props, aws, grafana, physical_id="grafana-provision-poc-dj")
    run("Update", props, aws, grafana, physical_id="grafana-provision-poc-dj")
    assert json.dumps([grafana.folders, grafana.datasources, grafana.dashboards], sort_keys=True) == snapshot
    assert len(aws.accounts) == 1  # the service account is reused, not duplicated
    assert ("PUT", "/api/folders/ongiiot-poc-dj") in grafana.calls
    assert ("PUT", "/api/datasources/uid/sitewise-poc-dj") in grafana.calls


def test_delete_removes_stage_state_and_service_account():
    aws, grafana = FakeAws(), FakeGrafana()
    props = props_for("poc-dj")
    run("Create", props, aws, grafana)
    result = run("Delete", props, aws, grafana, physical_id="grafana-provision-poc-dj")
    assert grafana.folders == {} and grafana.datasources == {} and grafana.dashboards == {}
    assert aws.accounts == {}
    assert result["PhysicalResourceId"] == "grafana-provision-poc-dj"


def test_token_short_lived_and_always_deleted():
    aws, grafana = FakeAws(), FakeGrafana()
    run("Create", props_for("poc-dj"), aws, grafana)
    assert aws.ttls == [900] and handler.TOKEN_TTL_S <= 900
    assert aws.tokens == {} and aws.deleted_tokens == ["t1"]


# -- best-effort paths -------------------------------------------------------------------------


class PluginErrors(FakeGrafana):
    def __init__(self, fail_on: str, status: int = 500, **kw):
        super().__init__(**kw)
        self.fail_on = fail_on
        self.status = status

    def request(self, method, path, body=None):
        if path.startswith("/api/plugins") and method == self.fail_on:
            raise handler.GrafanaError(method, path, self.status, "boom")
        return super().request(method, path, body)


@pytest.mark.parametrize(("fail_on", "status"), [("GET", 500)])
def test_plugin_errors_never_fail_the_deploy(fail_on, status):
    aws, grafana = FakeAws(), PluginErrors(fail_on, status, plugin_version="2.5.0")
    result = run("Create", props_for("poc-dj"), aws, grafana)
    assert result["Data"]["PluginPinned"] == "false"
    assert result["Data"]["PluginVersion"] == ("unknown" if fail_on == "GET" else "2.5.0")
    assert set(grafana.dashboards) == {"pipe-detail-dj", "facility-overview-dj"}
