"""facility/model.py: schema v1 validation, alias derivation, config_hash (AD-14)."""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import pytest
import yaml

from facility.model import (
    DEFAULT_PATH,
    MEASUREMENTS,
    Facility,
    FacilityError,
    load,
    parse,
)


@pytest.fixture
def doc() -> dict:
    return yaml.safe_load(Path(DEFAULT_PATH).read_text())


def pipe(doc: dict, pipe_id: str) -> dict:
    return next(p for p in doc["pipes"] if p["id"] == pipe_id)


# -- shipped model ----------------------------------------------------------------------


def test_shipped_model_loads():
    f = load(DEFAULT_PATH)
    assert isinstance(f, Facility)
    assert len(f.pipes) == 24
    assert [p.id for p in f.pipes] == [f"P-{i:03d}" for i in range(1, 25)]
    assert {p.area for p in f.pipes} == {"train1", "train2"}
    groups = f.peer_groups()
    assert len(groups) == 4 and all(len(ids) == 6 for ids in groups.values())
    for p in f.pipes:
        assert set(p.sensors) == set(MEASUREMENTS)
        assert p.service and p.material and p.age_years >= 0
        for m in p.sensors:
            c = p.contracts[m]
            assert c.unit and c.owner and c.min < c.max and c.rate_hz > 0


def test_valid_model_aliases_default_stage():
    f = load(DEFAULT_PATH)
    assert f.stage == "poc" and f.alias_root == "/lng1"
    assert f.alias("P-007", "flow_in") == "/lng1/train1/P-007/flow_in"
    assert f.derived_alias("P-007", "flow_in_dq") == "/lng1/train1/P-007/derived/flow_in_dq"
    assert f.derived_alias("P-007", "alert_state") == "/lng1/train1/P-007/derived/alert_state"
    assert f.attr_alias("P-007", "flow_in_owner") == "/lng1/train1/P-007/attr/flow_in_owner"


def test_valid_model_aliases_developer_stage():
    f = load(DEFAULT_PATH, stage="poc-dj")
    assert f.alias_root == "/poc-dj/lng1"
    assert f.alias("P-007", "flow_in") == "/poc-dj/lng1/train1/P-007/flow_in"


def test_contract_overrides_applied():
    f = load(DEFAULT_PATH)
    p7 = f.pipe("P-007")
    assert p7.contracts["flow_in"].max == 80
    assert p7.contracts["flow_in"].owner == "Process Engineering"  # rest from default
    assert f.pipe("P-001").contracts["flow_in"].max == 50
    assert f.attribute_values("P-007")["flow_in_max"] == 80


def test_derived_properties_v1():
    rows = {d.name: (d.level, d.type) for d in Facility.derived_properties()}
    assert rows["alert_state"] == ("pipe", "INTEGER")
    assert rows["alert_summary"] == ("pipe", "STRING")
    for m in MEASUREMENTS:
        assert rows[f"{m}_dq"] == ("sensor", "DOUBLE")
        assert rows[f"{m}_dq_fail"] == ("sensor", "STRING")
    assert len(rows) == 2 + 2 * len(MEASUREMENTS)


def test_invalid_stage_rejected():
    with pytest.raises(FacilityError, match="stage"):
        load(DEFAULT_PATH, stage="prod")


# -- I/O & edge-case matrix ---------------------------------------------------------------


def test_config_hash_changes_with_tunables(doc):
    base = parse(doc).config_hash()
    edited = copy.deepcopy(doc)
    edited["tunables"]["dq_min"] = 0.6
    assert parse(edited).config_hash() != base


def test_facility_is_frozen():
    f = load(DEFAULT_PATH)
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.stage = "poc-xx"  # type: ignore[misc]
    with pytest.raises(TypeError):
        f.tunables["dq_min"] = 0.9  # type: ignore[index]


# -- other schema v1 rules --------------------------------------------------------------------


def _step(target, key):
    if isinstance(target, list):
        return next(p for p in target if p["id"] == key)
    return target[key]


def _set(path, value):
    def mutate(doc):
        target = doc
        for k in path[:-1]:
            target = _step(target, k)
        target[path[-1]] = value

    return mutate


def _delete(path):
    def mutate(doc):
        target = doc
        for k in path[:-1]:
            target = _step(target, k)
        del target[path[-1]]

    return mutate


INVALID = {
    "unknown top-level key": _set(["extra"], 1),
    "min above max": _set(["contracts", "flow_in", "min"], 60),
    "tunable missing": _delete(["tunables", "window_len_s"]),
}


@pytest.mark.parametrize("mutate", INVALID.values(), ids=INVALID.keys())
def test_invalid_document(doc, mutate):
    mutate(doc)
    with pytest.raises(FacilityError):
        parse(doc)
