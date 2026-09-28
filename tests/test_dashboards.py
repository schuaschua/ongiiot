"""Dashboard templates in dashboards/ (AD-8, UX-DR1, UX-DR2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from facility.model import MEASUREMENTS

DASHBOARDS = Path(__file__).resolve().parent.parent / "dashboards"
DS = {"type": "grafana-iot-sitewise-datasource", "uid": "__DS_UID__"}


def load(name: str) -> dict:
    return json.loads((DASHBOARDS / f"{name}.json").read_text(encoding="utf-8"))


def variables(dash: dict) -> dict[str, dict]:
    return {v["name"]: v for v in dash["templating"]["list"]}


def targets(dash: dict) -> list[dict]:
    return [t for p in dash["panels"] for t in p.get("targets", [])]


@pytest.mark.parametrize("name", ["pipe-detail", "facility-overview"])
def test_uid_equals_file_name(name):
    assert load(name)["uid"] == name


def test_pipe_detail_variables_ad8():
    v = variables(load("pipe-detail"))
    assert v["pipe_id"]["type"] == "textbox" and v["pipe_id"]["query"].startswith("P-")
    assert v["area"]["type"] == "textbox"
    assert v["alias_root"]["type"] == "constant" and v["alias_root"]["query"] == "__ALIAS_ROOT__"
    assert v["evidence"]["multi"] is True
    assert v["evidence"]["includeAll"] is False


def test_evidence_options_are_this_pipes_raw_aliases():
    # Deep link v1 passes aliases; each option shows the measurement and carries the alias.
    query = variables(load("pipe-detail"))["evidence"]["query"]
    options = [o.split(" : ") for o in query.split(",")]
    assert [text for text, _ in options] == list(MEASUREMENTS)
    assert [value for _, value in options] == [f"${{alias_root}}/${{area}}/${{pipe_id}}/{m}" for m in MEASUREMENTS]


def test_pipe_detail_six_raw_trends_by_alias():
    dash = load("pipe-detail")
    trends = [p for p in dash["panels"] if p["type"] == "timeseries" and "repeat" not in p]
    assert [p["title"] for p in trends] == list(MEASUREMENTS)
    for panel, m in zip(trends, MEASUREMENTS):
        (t,) = panel["targets"]
        assert t["queryType"] == "PropertyValueHistory"  # raw values, not aggregates
        assert t["propertyAlias"] == f"${{alias_root}}/${{area}}/${{pipe_id}}/{m}"
        assert t["datasource"] == DS and panel["datasource"] == DS


def test_evidence_highlighted():
    dash = load("pipe-detail")
    (ev,) = [p for p in dash["panels"] if p.get("repeat") == "evidence"]
    (t,) = ev["targets"]
    assert t["propertyAlias"] == "${evidence}" and t["queryType"] == "PropertyValueHistory"
    assert ev["fieldConfig"]["defaults"]["color"] == {"mode": "fixed", "fixedColor": "red"}
    assert ev["fieldConfig"]["defaults"]["custom"]["lineWidth"] > 1


def test_no_aggregate_queries():
    for name in ("pipe-detail", "facility-overview"):
        for t in targets(load(name)):
            assert t["queryType"] in ("PropertyValueHistory", "PropertyValue")
            assert "aggregates" not in t


LINK = "/d/pipe-detail__UID_SUFFIX__/pipe-detail?var-pipe_id=__PIPE_ID__&var-area=__AREA__"


def pipe_protos() -> dict[str, dict]:
    """The overview's per-pipe prototypes keyed by title."""
    dash = load("facility-overview")
    return {p["title"]: p for p in dash["panels"] if "__PIPE_ID__" in json.dumps(p)}


def test_overview_has_three_pipe_prototypes():
    assert list(pipe_protos()) == ["__PIPE_ID__ (__AREA__)", "__PIPE_ID__ trust", "__PIPE_ID__ failing checks"]


@pytest.mark.parametrize("title", ["__PIPE_ID__ (__AREA__)", "__PIPE_ID__ trust", "__PIPE_ID__ failing checks"])
def test_overview_pipe_prototypes_link_to_pipe_detail(title):
    proto = pipe_protos()[title]
    assert proto["links"][0]["url"].startswith(LINK)
    assert proto["fieldConfig"]["defaults"]["links"][0]["url"].startswith(LINK)
    assert proto["datasource"] == DS
    assert all(t["queryType"] == "PropertyValue" and t["datasource"] == DS for t in proto["targets"])  # latest value


def test_overview_raw_prototype_targets():
    proto = pipe_protos()["__PIPE_ID__ (__AREA__)"]
    assert [t["propertyAlias"] for t in proto["targets"]] == [
        f"${{alias_root}}/__AREA__/__PIPE_ID__/{m}" for m in MEASUREMENTS
    ]


def test_overview_trust_prototype_uses_dq_min_threshold():
    proto = pipe_protos()["__PIPE_ID__ trust"]
    assert [t["propertyAlias"] for t in proto["targets"]] == [
        f"${{alias_root}}/__AREA__/__PIPE_ID__/derived/{m}_dq" for m in MEASUREMENTS
    ]
    assert [t["refId"] for t in proto["targets"]] == list(MEASUREMENTS)
    defaults = proto["fieldConfig"]["defaults"]
    assert defaults["color"] == {"mode": "thresholds"}
    # Red below dq_min, green at or above; the value is filled in at provision time.
    assert defaults["thresholds"]["steps"] == [{"color": "red", "value": None}, {"color": "green", "value": "__DQ_MIN__"}]
    assert proto["options"]["colorMode"] == "background"
    assert proto["options"]["reduceOptions"]["calcs"] == ["lastNotNull"]


def test_overview_failing_prototype_shows_non_empty_checks():
    proto = pipe_protos()["__PIPE_ID__ failing checks"]
    assert [t["propertyAlias"] for t in proto["targets"]] == [
        f"${{alias_root}}/__AREA__/__PIPE_ID__/derived/{m}_dq_fail" for m in MEASUREMENTS
    ]
    # String fields are shown only through an explicit field regex.
    assert proto["options"]["reduceOptions"]["fields"] == "/_dq_fail$/"
    (mapping,) = proto["fieldConfig"]["defaults"]["mappings"]
    assert mapping["type"] == "special" and mapping["options"]["match"] == "empty"
    assert mapping["options"]["result"]["text"] == "none"
    assert proto["fieldConfig"]["defaults"]["thresholds"]["steps"] == [{"color": "red", "value": None}]


def test_only_known_placeholders():
    known = {"__DS_UID__", "__ALIAS_ROOT__", "__UID_SUFFIX__", "__STAGE__", "__PIPE_ID__", "__AREA__", "__DQ_MIN__"}
    for name in ("pipe-detail", "facility-overview"):
        found = set(re.findall(r"__[A-Z_]+?__", (DASHBOARDS / f"{name}.json").read_text()))
        assert found <= known, found
