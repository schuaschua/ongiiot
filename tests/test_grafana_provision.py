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


MEASUREMENTS = ("pressure_in", "pressure_out", "flow_in", "flow_out", "temperature", "methane_ppm")


def overview_for(props: dict) -> dict:
    aws, grafana = FakeAws(), FakeGrafana()
    run("Create", props, aws, grafana)
    (uid,) = [u for u in grafana.dashboards if u.startswith("facility-overview")]
    return grafana.dashboards[uid]["dashboard"]


def test_overview_has_every_pipe_linking_to_stage_pipe_detail():
    props = props_for("poc-dj")
    overview = overview_for(props)
    pipes = json.loads(props["Pipes"])
    raw = [p for p in overview["panels"] if p["type"] == "stat" and p["title"].endswith(")")]
    trust = [p for p in overview["panels"] if p["title"].endswith(" trust")]
    failing = [p for p in overview["panels"] if p["title"].endswith(" failing checks")]
    assert len(raw) == len(trust) == len(failing) == len(pipes) == 24
    assert len([p for p in overview["panels"] if p["type"] == "stat"]) == 24 * 3
    for pipe, r, t, f in zip(pipes, raw, trust, failing):
        pid, area = pipe["id"], pipe["area"]
        assert (r["title"], t["title"], f["title"]) == (f"{pid} ({area})", f"{pid} trust", f"{pid} failing checks")
        for panel in (r, t, f):
            for url in (panel["links"][0]["url"], panel["fieldConfig"]["defaults"]["links"][0]["url"]):
                assert url.startswith(f"/d/pipe-detail-dj/pipe-detail?var-pipe_id={pid}&var-area={area}&")
        root = f"${{alias_root}}/{area}/{pid}"
        assert [x["propertyAlias"] for x in r["targets"]] == [f"{root}/{m}" for m in MEASUREMENTS]
        assert [x["propertyAlias"] for x in t["targets"]] == [f"{root}/derived/{m}_dq" for m in MEASUREMENTS]
        assert [x["propertyAlias"] for x in f["targets"]] == [f"{root}/derived/{m}_dq_fail" for m in MEASUREMENTS]
        # Grouped per pipe: the three tiles stack in one column block.
        assert r["gridPos"]["x"] == t["gridPos"]["x"] == f["gridPos"]["x"]
        assert r["gridPos"]["y"] + r["gridPos"]["h"] == t["gridPos"]["y"]
        assert t["gridPos"]["y"] + t["gridPos"]["h"] == f["gridPos"]["y"]
    rows = [p for p in overview["panels"] if p["type"] == "row"]
    assert [p["title"] for p in rows] == ["train1", "train2"]
    # Every pipe of an area sits below its row and above the next one.
    by_area = {a: [p for p, pipe in zip(raw, pipes) if pipe["area"] == a] for a in ("train1", "train2")}
    assert all(p["gridPos"]["y"] > rows[0]["gridPos"]["y"] for p in by_area["train1"])
    assert all(p["gridPos"]["y"] < rows[1]["gridPos"]["y"] for p in by_area["train1"])
    assert all(p["gridPos"]["y"] > rows[1]["gridPos"]["y"] for p in by_area["train2"])
    ids = [p["id"] for p in overview["panels"]]
    assert len(ids) == len(set(ids))
    # No two panels overlap on the grid, and all fit in 24 columns.
    cells = set()
    for p in overview["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24, p["title"]
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, p["title"]
                cells.add((x, y))


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


def test_changed_dq_min_flows_into_threshold():
    import dataclasses

    facility = load(DEFAULT_PATH, stage="poc-dj")
    changed = dataclasses.replace(facility, tunables={**facility.tunables, "dq_min": 0.7})
    props = provision_properties(changed, WS, "https://g-abc123.grafana-workspace.example")
    assert props["DqMin"] == "0.7"
    overview = overview_for(props)
    assert trust_threshold(overview) == [0.7]
    assert isinstance(trust_threshold(overview)[0], float)  # a number, not the string "0.7"
    (text,) = [p for p in overview["panels"] if p["type"] == "text"]
    assert "0.7" in text["options"]["content"]


def test_fill_keeps_numbers_numeric():
    subs = {"__N__": 0.5, "__S__": "x"}
    assert handler.fill({"a": "__N__", "b": "n=__N__", "c": "__S__", "d": [1, None]}, subs) == {
        "a": 0.5, "b": "n=0.5", "c": "x", "d": [1, None]
    }


@pytest.mark.parametrize(("stage", "root", "suffix"), [("poc", "/lng1", ""), ("poc-dj", "/poc-dj/lng1", "-dj")])
def test_overview_queries_only_own_alias_root(stage, root, suffix):
    overview = overview_for(props_for(stage))
    (alias_root,) = overview["templating"]["list"]
    assert alias_root["name"] == "alias_root" and alias_root["query"] == root
    aliases = [t["propertyAlias"] for p in overview["panels"] for t in p.get("targets", [])]
    assert len(aliases) == 24 * 18 and all(a.startswith("${alias_root}/") for a in aliases)
    assert all(t["datasource"]["uid"] == f"sitewise-{stage}" for p in overview["panels"] for t in p.get("targets", []))
    urls = [p["links"][0]["url"] for p in overview["panels"] if p.get("links")]
    assert len(urls) == 24 * 3 and all(u.startswith(f"/d/pipe-detail{suffix}/pipe-detail?") for u in urls)


def test_poc_uses_unsuffixed_uids():
    aws, grafana = FakeAws(), FakeGrafana()
    run("Create", props_for("poc"), aws, grafana)
    assert set(grafana.dashboards) == {"pipe-detail", "facility-overview"}
    assert set(grafana.folders) == {"ongiiot-poc"} and set(grafana.datasources) == {"sitewise-poc"}
    detail = json.dumps(grafana.dashboards["facility-overview"]["dashboard"])
    assert "/d/pipe-detail/pipe-detail?" in detail


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


def test_two_stages_side_by_side():
    aws, grafana = FakeAws(), FakeGrafana()
    run("Create", props_for("poc"), aws, grafana)
    run("Create", props_for("poc-dj"), aws, grafana)
    assert set(grafana.folders) == {"ongiiot-poc", "ongiiot-poc-dj"}
    assert set(grafana.datasources) == {"sitewise-poc", "sitewise-poc-dj"}
    poc = json.dumps(grafana.dashboards["pipe-detail"]["dashboard"])
    assert "/poc-dj/" not in poc and "sitewise-poc-dj" not in poc
    run("Delete", props_for("poc-dj"), aws, grafana, physical_id="grafana-provision-poc-dj")
    assert set(grafana.dashboards) == {"pipe-detail", "facility-overview"}
    assert set(grafana.folders) == {"ongiiot-poc"}


def test_delete_removes_stage_state_and_service_account():
    aws, grafana = FakeAws(), FakeGrafana()
    props = props_for("poc-dj")
    run("Create", props, aws, grafana)
    result = run("Delete", props, aws, grafana, physical_id="grafana-provision-poc-dj")
    assert grafana.folders == {} and grafana.datasources == {} and grafana.dashboards == {}
    assert aws.accounts == {}
    assert result["PhysicalResourceId"] == "grafana-provision-poc-dj"


def test_delete_of_missing_state_succeeds():
    aws, grafana = FakeAws(), FakeGrafana()
    run("Delete", props_for("poc-dj"), aws, grafana, physical_id="grafana-provision-poc-dj")
    assert aws.tokens == {}


def test_token_short_lived_and_always_deleted():
    aws, grafana = FakeAws(), FakeGrafana()
    run("Create", props_for("poc-dj"), aws, grafana)
    assert aws.ttls == [900] and handler.TOKEN_TTL_S <= 900
    assert aws.tokens == {} and aws.deleted_tokens == ["t1"]


def test_token_deleted_when_grafana_fails():
    class Broken(FakeGrafana):
        def request(self, method, path, body=None):
            if path == "/api/dashboards/db":
                raise handler.GrafanaError(method, path, 500, "x" * 10_000)
            return super().request(method, path, body)

    aws = FakeAws()
    with pytest.raises(handler.GrafanaError) as exc:
        run("Create", props_for("poc-dj"), aws, Broken())
    assert aws.tokens == {} and aws.deleted_tokens == ["t1"]
    assert len(str(exc.value)) < 1024  # the Provider puts this in the reason; stays under 4 KB


def test_plugin_pin_installs_when_other_version():
    aws, grafana = FakeAws(), FakeGrafana(plugin_version="2.5.0")
    result = run("Create", props_for("poc-dj"), aws, grafana)
    assert ("POST", "/api/plugins/grafana-iot-sitewise-datasource/install") in grafana.calls
    assert result["Data"]["PluginVersion"] == "2.6.3" and result["Data"]["PluginPinned"] == "true"


def test_plugin_pin_refused_keeps_shipped_version():
    aws, grafana = FakeAws(), FakeGrafana(plugin_version="2.5.0", install_status=403)
    result = run("Create", props_for("poc-dj"), aws, grafana)
    assert result["Data"]["PluginVersion"] == "2.5.0" and result["Data"]["PluginPinned"] == "false"
    assert set(grafana.dashboards) == {"pipe-detail-dj", "facility-overview-dj"}


def test_response_size_under_4kb():
    aws, grafana = FakeAws(), FakeGrafana()
    result = run("Create", props_for("poc-dj"), aws, grafana)
    assert len(json.dumps(result)) < 4096


# -- real HTTP client (urlopen patched) -------------------------------------------------------


def test_grafana_api_maps_http_errors_and_sends_bearer(monkeypatch):
    import io
    import urllib.error

    sent = []
    replies = []

    class Resp:
        def __init__(self, raw):
            self.raw = raw

        def read(self):
            return self.raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent.append(req)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Resp(reply)

    monkeypatch.setattr(handler.urllib.request, "urlopen", fake_urlopen)
    api = handler.GrafanaApi("https://g.example/", "tok")

    replies.append(b'{"ok": true}')
    assert api.request("POST", "/api/folders", {"uid": "f"}) == {"ok": True}
    req = sent[-1]
    assert req.full_url == "https://g.example/api/folders"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"uid": "f"}

    replies.append(urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"x" * 10_000)))
    with pytest.raises(handler.GrafanaError) as exc:
        api.request("GET", "/api/folders/f")
    assert exc.value.status == 404
    assert "x" * handler.MAX_ERROR_CHARS in str(exc.value)
    assert "x" * (handler.MAX_ERROR_CHARS + 1) not in str(exc.value)

    replies.append(b"")
    assert api.request("DELETE", "/api/folders/f") is None

    replies.append(b"<html>not json</html>")
    assert api.request("GET", "/api/health") is None

    replies.append(urllib.error.URLError("connection refused"))
    with pytest.raises(handler.GrafanaError) as exc:
        api.request("GET", "/api/health")
    assert exc.value.status == 0

    replies.append(TimeoutError("timed out"))
    with pytest.raises(handler.GrafanaError) as exc:
        api.request("GET", "/api/health")
    assert exc.value.status == 0


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


@pytest.mark.parametrize(("fail_on", "status"), [("GET", 500), ("GET", 0), ("POST", 500), ("POST", 0)])
def test_plugin_errors_never_fail_the_deploy(fail_on, status):
    aws, grafana = FakeAws(), PluginErrors(fail_on, status, plugin_version="2.5.0")
    result = run("Create", props_for("poc-dj"), aws, grafana)
    assert result["Data"]["PluginPinned"] == "false"
    assert result["Data"]["PluginVersion"] == ("unknown" if fail_on == "GET" else "2.5.0")
    assert set(grafana.dashboards) == {"pipe-detail-dj", "facility-overview-dj"}


def test_plugin_pin_reports_only_version_read_back():
    class NoReadBack(FakeGrafana):
        def request(self, method, path, body=None):
            if path.endswith("/settings") and method == "GET" and ("POST", "/api/plugins/grafana-iot-sitewise-datasource/install") in self.calls:
                self.calls.append((method, path))
                return {}
            return super().request(method, path, body)

    aws, grafana = FakeAws(), NoReadBack(plugin_version="2.5.0")
    result = run("Create", props_for("poc-dj"), aws, grafana)
    assert result["Data"]["PluginVersion"] == "unknown" and result["Data"]["PluginPinned"] == "false"


def test_token_delete_failure_does_not_mask_original_error():
    class TokenStuck(FakeAws):
        def delete_workspace_service_account_token(self, **kw):
            raise RuntimeError("token delete failed")

    class Broken(FakeGrafana):
        def request(self, method, path, body=None):
            if path == "/api/dashboards/db":
                raise handler.GrafanaError(method, path, 500, "original")
            return super().request(method, path, body)

    with pytest.raises(handler.GrafanaError, match="original"):
        run("Create", props_for("poc-dj"), TokenStuck(), Broken())


def test_delete_succeeds_despite_grafana_errors():
    class Forbidden(FakeGrafana):
        def request(self, method, path, body=None):
            if method == "DELETE" and "datasources" in path:
                raise handler.GrafanaError(method, path, 403, "forbidden")
            return super().request(method, path, body)

    aws, grafana = FakeAws(), Forbidden()
    props = props_for("poc-dj")
    run("Create", props, aws, grafana)
    result = run("Delete", props, aws, grafana, physical_id="grafana-provision-poc-dj")
    assert result == {"PhysicalResourceId": "grafana-provision-poc-dj", "Data": {}}
    assert grafana.dashboards == {} and grafana.folders == {}  # later objects still attempted
    assert set(grafana.datasources) == {"sitewise-poc-dj"}
    assert aws.accounts == {} and aws.tokens == {}


def test_delete_succeeds_when_workspace_is_gone():
    class Gone(FakeAws):
        def list_workspace_service_accounts(self, **kw):
            raise RuntimeError("ResourceNotFoundException: workspace not found")

    event = {"RequestType": "Delete", "PhysicalResourceId": "grafana-provision-poc-dj", "ResourceProperties": props_for("poc-dj")}
    result = handler.on_event(event, None, client=Gone(), api_factory=lambda u, t: FakeGrafana())
    assert result["Data"] == {}
