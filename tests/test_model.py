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


def test_unknown_alias_requests_fail():
    f = load(DEFAULT_PATH)
    with pytest.raises(FacilityError):
        f.alias("P-999", "flow_in")
    with pytest.raises(FacilityError):
        f.alias("P-007", "vibration")
    with pytest.raises(FacilityError):
        f.derived_alias("P-007", "bogus")
    with pytest.raises(FacilityError):
        f.attr_alias("P-007", "bogus")


def test_invalid_stage_rejected():
    with pytest.raises(FacilityError, match="stage"):
        load(DEFAULT_PATH, stage="prod")


# -- I/O & edge-case matrix ---------------------------------------------------------------


def test_duplicate_pipe_id(doc):
    doc["pipes"][1]["id"] = "P-007"
    with pytest.raises(FacilityError, match="P-007"):
        parse(doc)


def test_bad_pipe_id(doc):
    pipe(doc, "P-007")["id"] = "P-7"
    with pytest.raises(FacilityError, match="P-7"):
        parse(doc)


def test_unknown_area(doc):
    pipe(doc, "P-007")["area"] = "train9"
    with pytest.raises(FacilityError, match=r"P-007.*train9"):
        parse(doc)


def test_incomplete_contract(doc):
    del doc["contracts"]["methane_ppm"]["owner"]
    with pytest.raises(FacilityError, match=r"pipe P-\d{3}, measurement methane_ppm.*owner"):
        parse(doc)


def test_incomplete_contract_fixed_by_override_is_valid(doc):
    del doc["contracts"]["methane_ppm"]["owner"]
    for p in doc["pipes"]:
        p.setdefault("contract_overrides", {}).setdefault("methane_ppm", {})["owner"] = "HSE"
    parse(doc)


def test_wrong_unit(doc):
    doc["contracts"]["flow_in"]["unit"] = "m3/h"
    with pytest.raises(FacilityError, match="m3/h"):
        parse(doc)


def test_wrong_unit_in_override(doc):
    pipe(doc, "P-007")["contract_overrides"] = {"flow_in": {"unit": "m3/h"}}
    with pytest.raises(FacilityError, match=r"P-007.*flow_in.*m3/h"):
        parse(doc)


def test_small_peer_group(doc):
    for pid in ("P-001", "P-002", "P-003"):
        pipe(doc, pid)["peer_group"] = "tiny-group"
    with pytest.raises(FacilityError, match="tiny-group"):
        parse(doc)


def test_unknown_sensor(doc):
    pipe(doc, "P-007")["sensors"] = ["vibration"]
    with pytest.raises(FacilityError, match=r"P-007.*vibration"):
        parse(doc)


def test_config_hash_changes_with_tunables(doc):
    base = parse(doc).config_hash()
    edited = copy.deepcopy(doc)
    edited["tunables"]["dq_min"] = 0.6
    assert parse(edited).config_hash() != base


def test_config_hash_ignores_key_order(doc):
    base = parse(doc).config_hash()
    reordered = copy.deepcopy(doc)
    t = reordered["tunables"]
    reordered["tunables"] = {k: t[k] for k in reversed(list(t))}
    reordered["tunables"]["detectors"] = {k: v for k, v in reversed(list(t["detectors"].items()))}
    assert list(reordered["tunables"]) != list(doc["tunables"])
    assert parse(reordered).config_hash() == base


def test_config_hash_ignores_non_tunable_edits(doc):
    base = parse(doc).config_hash()
    pipe(doc, "P-007")["age_years"] = 13
    assert parse(doc).config_hash() == base


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
    "version unsupported": _set(["version"], 2),
    "version bool": _set(["version"], True),
    "version float": _set(["version"], 1.0),
    "unknown top-level key": _set(["extra"], 1),
    "missing top-level key": _delete(["tunables"]),
    "unknown pipe key": _set(["pipes", "P-007", "colour"], "red"),
    "missing pipe key": _delete(["pipes", "P-007", "material"]),
    "facility id uppercase": _set(["facility", "id"], "LNG1"),
    "facility id slash": _set(["facility", "id"], "lng/1"),
    "duplicate areas": _set(["areas"], ["train1", "train2", "train1"]),
    "area not alias-safe": _set(["areas"], ["train1", "train 2"]),
    "unknown contract measurement": _set(["contracts", "vibration"], {"unit": "mm/s"}),
    "unknown contract field": _set(["contracts", "flow_in", "precision"], 2),
    "unknown override field": _set(["pipes", "P-007", "contract_overrides"], {"flow_in": {"precision": 2}}),
    "negative age": _set(["pipes", "P-007", "age_years"], -1),
    "bool age": _set(["pipes", "P-007", "age_years"], True),
    "duplicate sensors": _set(["pipes", "P-007", "sensors"], ["flow_in", "flow_in"]),
    "override for missing sensor": lambda d: pipe(d, "P-007").update(
        sensors=["flow_in", "flow_out"], contract_overrides={"methane_ppm": {"max": 1}}
    ),
    "min equals max": _set(["contracts", "flow_in", "min"], 50),
    "min above max": _set(["contracts", "flow_in", "min"], 60),
    "rate_hz zero": _set(["contracts", "flow_in", "rate_hz"], 0),
    "rate_hz negative": _set(["contracts", "flow_in", "rate_hz"], -1),
    "owner not string": _set(["contracts", "flow_in", "owner"], 42),
    "contract max inf": _set(["contracts", "flow_in", "max"], float("inf")),
    "contract min nan": _set(["contracts", "flow_in", "min"], float("nan")),
    "tunable missing": _delete(["tunables", "window_len_s"]),
    "tunable non-numeric": _set(["tunables", "window_len_s"], "900"),
    "window_len_s zero": _set(["tunables", "window_len_s"], 0),
    "window_len_s inf": _set(["tunables", "window_len_s"], float("inf")),
    "reopen_grace_s negative": _set(["tunables", "reopen_grace_s"], -1),
    "ingest_lag_s negative": _set(["tunables", "ingest_lag_s"], -1),
    "ingest_lag_s nan": _set(["tunables", "ingest_lag_s"], float("nan")),
    "quiet_runs_to_close zero": _set(["tunables", "quiet_runs_to_close"], 0),
    "quiet_runs_to_close float": _set(["tunables", "quiet_runs_to_close"], 2.5),
    "quiet_runs_to_close bool": _set(["tunables", "quiet_runs_to_close"], True),
    "dq_min above 1": _set(["tunables", "dq_min"], 1.5),
    "dq_min below 0": _set(["tunables", "dq_min"], -0.1),
    "dq_min nan": _set(["tunables", "dq_min"], float("nan")),
}


@pytest.mark.parametrize("mutate", INVALID.values(), ids=INVALID.keys())
def test_invalid_document(doc, mutate):
    mutate(doc)
    with pytest.raises(FacilityError):
        parse(doc)


def test_ingest_lag_zero_is_valid(doc):
    doc["tunables"]["ingest_lag_s"] = 0
    parse(doc)


def test_load_wraps_read_errors(tmp_path):
    with pytest.raises(FacilityError):
        load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(FacilityError):
        load(bad)
