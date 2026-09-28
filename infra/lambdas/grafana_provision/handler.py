"""Provision one stage's Grafana folder, SiteWise data source and dashboards (custom resource).

Runs inside ``cdk deploy`` (AD-16, amended 2026-09-28) through the Provider framework.

Event ``ResourceProperties`` (all strings; JSON where noted):
    Stage, WorkspaceId, GrafanaUrl, Region, AliasRoot, UidSuffix ("" for poc, "-<initials>" otherwise)
    FolderUid, DataSourceUid, PluginId, PluginVersion
    Pipes: JSON [{"id": "P-001", "area": "train1"}, ...]   (from facility/model.py)
    Dashboards: JSON {"<base uid>": <template dashboard>, ...}   (from dashboards/*.json)

Every call mints a short-lived token (``TOKEN_TTL_S``) on the stage's service account and
deletes it again, whatever happens. Create/Update pin the SiteWise plugin (best effort,
the version actually running is returned), then upsert the folder, the data source and
the dashboards, keyed by uid so a re-run updates in place. Delete removes the stage's
dashboards, data source and folder, then the service account.

Template placeholders: ``__DS_UID__``, ``__ALIAS_ROOT__``, ``__UID_SUFFIX__``, ``__STAGE__``.
In the overview, panels containing ``__AREA__`` / ``__PIPE_ID__`` are prototypes cloned per
area and per pipe. Responses stay far below the 4 KB custom resource limit.
"""

from __future__ import annotations

import copy
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

TOKEN_TTL_S = 900  # 15 minutes, the plan's upper bound
DATASOURCE_TYPE = "grafana-iot-sitewise-datasource"
MAX_ERROR_CHARS = 600  # keeps the Provider's failure reason well under 4 KB
PIPE_PANEL_W, PIPE_PANEL_H, PIPES_PER_ROW = 6, 6, 4


class GrafanaError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        self.status = status
        super().__init__(f"Grafana {method} {path} -> {status}: {body[:MAX_ERROR_CHARS]}")


# -- Grafana HTTP API ----------------------------------------------------------------------


class GrafanaApi:
    """Minimal Grafana HTTP API client (urllib only, so the Lambda bundle needs no deps)."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise GrafanaError(method, path, exc.code, exc.read().decode("utf-8", "replace")) from None
        return json.loads(raw) if raw else None


def _get_or_none(api: Any, path: str) -> Any:
    try:
        return api.request("GET", path)
    except GrafanaError as exc:
        if exc.status == 404:
            return None
        raise


def _delete_if_present(api: Any, path: str) -> bool:
    try:
        api.request("DELETE", path)
        return True
    except GrafanaError as exc:
        if exc.status == 404:
            return False
        raise


# -- service account token -------------------------------------------------------------------


def service_account_name(stage: str) -> str:
    return f"ongiiot-provision-{stage}"


def _find_service_account(client: Any, workspace_id: str, name: str) -> str | None:
    kwargs: dict[str, Any] = {"workspaceId": workspace_id}
    while True:
        page = client.list_workspace_service_accounts(**kwargs)
        for sa in page.get("serviceAccounts", []):
            if sa["name"] == name:
                return sa["id"]
        if not page.get("nextToken"):
            return None
        kwargs["nextToken"] = page["nextToken"]


def ensure_service_account(client: Any, workspace_id: str, stage: str) -> str:
    name = service_account_name(stage)
    found = _find_service_account(client, workspace_id, name)
    if found:
        return found
    return client.create_workspace_service_account(workspaceId=workspace_id, name=name, grafanaRole="ADMIN")["id"]


@contextmanager
def short_lived_token(client: Any, workspace_id: str, sa_id: str, now: Callable[[], float] = time.time) -> Iterator[str]:
    """Mint a token of at most ``TOKEN_TTL_S`` seconds and always delete it afterwards."""
    created = client.create_workspace_service_account_token(
        workspaceId=workspace_id,
        serviceAccountId=sa_id,
        name=f"deploy-{int(now() * 1000)}",
        secondsToLive=TOKEN_TTL_S,
    )
    token = created["serviceAccountToken"]
    try:
        yield token["key"]
    finally:
        client.delete_workspace_service_account_token(
            workspaceId=workspace_id, serviceAccountId=sa_id, tokenId=token["id"]
        )


# -- templates -------------------------------------------------------------------------------


def fill(value: Any, subs: dict[str, str]) -> Any:
    """Replace placeholders in every string of a JSON value."""
    if isinstance(value, str):
        for key, sub in subs.items():
            value = value.replace(key, sub)
        return value
    if isinstance(value, list):
        return [fill(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, subs) for k, v in value.items()}
    return value


def _mentions(panel: dict[str, Any], placeholder: str) -> bool:
    return placeholder in json.dumps(panel)


def expand_pipe_panels(dashboard: dict[str, Any], pipes: list[dict[str, str]]) -> dict[str, Any]:
    """Clone the ``__AREA__`` row prototype per area and the ``__PIPE_ID__`` panel per pipe."""
    panels = dashboard.get("panels", [])
    pipe_protos = [p for p in panels if _mentions(p, "__PIPE_ID__")]
    if not pipe_protos:
        return dashboard
    row_protos = [p for p in panels if p not in pipe_protos and _mentions(p, "__AREA__")]
    static = [p for p in panels if p not in pipe_protos and p not in row_protos]
    y = max((p["gridPos"]["y"] + p["gridPos"]["h"] for p in static), default=0)
    next_id = max((p.get("id", 0) for p in panels), default=0) + 1
    out = list(static)

    areas: list[str] = []
    for pipe in pipes:
        if pipe["area"] not in areas:
            areas.append(pipe["area"])
    for area in areas:
        for proto in row_protos:
            row = fill(copy.deepcopy(proto), {"__AREA__": area})
            row.update(id=next_id, gridPos={"h": 1, "w": 24, "x": 0, "y": y})
            next_id += 1
            y += 1
            out.append(row)
        in_area = [p for p in pipes if p["area"] == area]
        for i, pipe in enumerate(in_area):
            for proto in pipe_protos:
                panel = fill(copy.deepcopy(proto), {"__PIPE_ID__": pipe["id"], "__AREA__": area})
                panel.update(
                    id=next_id,
                    gridPos={
                        "h": PIPE_PANEL_H,
                        "w": PIPE_PANEL_W,
                        "x": (i % PIPES_PER_ROW) * PIPE_PANEL_W,
                        "y": y + (i // PIPES_PER_ROW) * PIPE_PANEL_H,
                    },
                )
                next_id += 1
                out.append(panel)
        y += -(-len(in_area) // PIPES_PER_ROW) * PIPE_PANEL_H
    return {**dashboard, "panels": out}


def render_dashboards(props: dict[str, Any]) -> list[dict[str, Any]]:
    subs = {
        "__DS_UID__": props["DataSourceUid"],
        "__ALIAS_ROOT__": props["AliasRoot"],
        "__UID_SUFFIX__": props["UidSuffix"],
        "__STAGE__": props["Stage"],
    }
    pipes = json.loads(props["Pipes"])
    out = []
    for base_uid, template in json.loads(props["Dashboards"]).items():
        dash = fill(expand_pipe_panels(template, pipes), subs)
        dash["uid"] = base_uid + props["UidSuffix"]
        dash["id"] = None
        out.append(dash)
    return out


def dashboard_uids(props: dict[str, Any]) -> list[str]:
    return [uid + props["UidSuffix"] for uid in json.loads(props["Dashboards"])]


# -- provisioning ----------------------------------------------------------------------------


def pin_plugin(api: Any, plugin_id: str, version: str) -> tuple[str, bool]:
    """Install ``version`` if it is not already running. Returns (running version, pinned)."""
    settings = _get_or_none(api, f"/api/plugins/{plugin_id}/settings") or {}
    current = (settings.get("info") or {}).get("version", "")
    if current == version:
        return current, True
    try:
        api.request("POST", f"/api/plugins/{plugin_id}/install", {"version": version})
    except GrafanaError as exc:
        # Managed Grafana may refuse plugin changes; keep the shipped version and record it.
        LOG.warning(json.dumps({"plugin_pin_failed": str(exc)[:MAX_ERROR_CHARS], "running": current}))
        return current or "unknown", False
    settings = _get_or_none(api, f"/api/plugins/{plugin_id}/settings") or {}
    running = (settings.get("info") or {}).get("version", "") or version
    return running, running == version


def upsert_folder(api: Any, uid: str, title: str) -> None:
    if _get_or_none(api, f"/api/folders/{uid}") is None:
        api.request("POST", "/api/folders", {"uid": uid, "title": title})
    else:
        api.request("PUT", f"/api/folders/{uid}", {"title": title, "overwrite": True})


def datasource_body(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": props["DataSourceUid"],
        "name": props["DataSourceUid"],
        "type": props.get("PluginId", DATASOURCE_TYPE),
        "access": "proxy",
        "isDefault": False,
        # "ec2_iam_role" is Managed Grafana's "Workspace IAM role" (AWSIoTSiteWiseReadOnlyAccess only).
        "jsonData": {"authType": "ec2_iam_role", "defaultRegion": props["Region"]},
    }


def upsert_datasource(api: Any, props: dict[str, Any]) -> None:
    uid = props["DataSourceUid"]
    body = datasource_body(props)
    if _get_or_none(api, f"/api/datasources/uid/{uid}") is None:
        api.request("POST", "/api/datasources", body)
    else:
        api.request("PUT", f"/api/datasources/uid/{uid}", body)


def provision(api: Any, props: dict[str, Any]) -> dict[str, str]:
    plugin_version, pinned = pin_plugin(api, props["PluginId"], props["PluginVersion"])
    upsert_folder(api, props["FolderUid"], props["FolderUid"])
    upsert_datasource(api, props)
    uids = []
    for dash in render_dashboards(props):
        api.request(
            "POST",
            "/api/dashboards/db",
            {"dashboard": dash, "folderUid": props["FolderUid"], "overwrite": True, "message": "cdk deploy"},
        )
        uids.append(dash["uid"])
    return {
        "FolderUid": props["FolderUid"],
        "DataSourceUid": props["DataSourceUid"],
        "DashboardUids": ",".join(uids),
        "PluginVersion": plugin_version,
        "PluginPinned": str(pinned).lower(),
    }


def deprovision(api: Any, props: dict[str, Any]) -> dict[str, str]:
    for uid in dashboard_uids(props):
        _delete_if_present(api, f"/api/dashboards/uid/{uid}")
    _delete_if_present(api, f"/api/datasources/uid/{props['DataSourceUid']}")
    _delete_if_present(api, f"/api/folders/{props['FolderUid']}")
    return {}


# -- entry point -----------------------------------------------------------------------------

_client = None


def _grafana():
    global _client
    if _client is None:
        _client = boto3.client("grafana")
    return _client


def on_event(event: dict[str, Any], context: Any, *, client: Any = None, api_factory=GrafanaApi) -> dict[str, Any]:
    request = event["RequestType"]
    props = event.get("ResourceProperties", {})
    stage = props["Stage"]
    workspace_id = props["WorkspaceId"]
    physical_id = event.get("PhysicalResourceId") or f"grafana-provision-{stage}"
    LOG.info(json.dumps({"request": request, "stage": stage, "workspace": workspace_id}))
    client = client or _grafana()

    sa_id = ensure_service_account(client, workspace_id, stage)
    with short_lived_token(client, workspace_id, sa_id) as token:
        api = api_factory(props["GrafanaUrl"], token)
        if request in ("Create", "Update"):
            data = provision(api, props)
        else:
            data = deprovision(api, props)
    if request == "Delete":
        client.delete_workspace_service_account(workspaceId=workspace_id, serviceAccountId=sa_id)
    LOG.info(json.dumps({"result": data}))
    return {"PhysicalResourceId": physical_id, "Data": data}
