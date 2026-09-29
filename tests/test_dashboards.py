"""Dashboard templates in dashboards/ (AD-8, UX-DR1, UX-DR2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from facility.model import MEASUREMENTS

DASHBOARDS = Path(__file__).resolve().parent.parent / "dashboards"


def load(name: str) -> dict:
    return json.loads((DASHBOARDS / f"{name}.json").read_text(encoding="utf-8"))


def variables(dash: dict) -> dict[str, dict]:
    return {v["name"]: v for v in dash["templating"]["list"]}


def targets(dash: dict) -> list[dict]:
    return [t for p in dash["panels"] for t in p.get("targets", [])]


@pytest.mark.parametrize("name", ["pipe-detail"])
def test_uid_equals_file_name(name):
    assert load(name)["uid"] == name


def test_pipe_detail_variables_ad8():
    v = variables(load("pipe-detail"))
    assert v["pipe_id"]["type"] == "textbox" and v["pipe_id"]["query"].startswith("P-")
    assert v["area"]["type"] == "textbox"
    assert v["alias_root"]["type"] == "constant" and v["alias_root"]["query"] == "__ALIAS_ROOT__"
    assert v["evidence"]["multi"] is True
    assert v["evidence"]["includeAll"] is False


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


def pipe_protos() -> dict[str, dict]:
    """The overview's per-pipe prototypes keyed by title."""
    dash = load("facility-overview")
    return {p["title"]: p for p in dash["panels"] if "__PIPE_ID__" in json.dumps(p)}


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


def test_only_known_placeholders():
    known = {"__DS_UID__", "__ALIAS_ROOT__", "__UID_SUFFIX__", "__STAGE__", "__PIPE_ID__", "__AREA__", "__DQ_MIN__"}
    for name in ("pipe-detail", "facility-overview"):
        found = set(re.findall(r"__[A-Z_]+?__", (DASHBOARDS / f"{name}.json").read_text()))
        assert found <= known, found
