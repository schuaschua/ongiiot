"""AD-8 contract: the Deep link v1 matches ``dashboards/pipe-detail.json`` (uid, variable names, multi-value
``evidence``); fails on drift of either side."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from detect.adapters.deeplink import DASHBOARD_SLUG, build_link
from infra.stacks.grafana_provision import dashboard_uid
from tests.test_deeplink import URL, incident

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboards" / "pipe-detail.json"


def dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def test_dashboard_declares_the_link_contract():
    d = dashboard()
    assert d["uid"] == "pipe-detail" == DASHBOARD_SLUG
    v = {x["name"]: x for x in d["templating"]["list"]}
    assert v["pipe_id"]["type"] == "textbox"
    assert v["area"]["type"] == "textbox"
    assert v["alias_root"]["type"] == "constant"
    assert v["evidence"]["multi"] is True


def test_link_params_are_dashboard_variables():
    d = dashboard()
    names = {x["name"] for x in d["templating"]["list"]}
    for stage in ("poc", "poc-dj"):
        link = build_link(incident(), URL, dashboard_uid(d["uid"], stage))
        parts = urlsplit(link)
        assert parts.path == f"/d/{dashboard_uid('pipe-detail', stage)}/pipe-detail"
        params = parse_qsl(parts.query)
        keys = [k for k, _ in params]
        assert set(keys) == {"orgId", "var-pipe_id", "var-area", "var-evidence", "from", "to"}
        assert {k.removeprefix("var-") for k in keys if k.startswith("var-")} <= names
        assert [v for k, v in params if k == "var-evidence"] == list(incident().evidence_aliases)
    assert dashboard_uid("pipe-detail", "poc") == "pipe-detail"
    assert dashboard_uid("pipe-detail", "poc-dj") == "pipe-detail-dj"
