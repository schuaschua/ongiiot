"""Facility model v1: the only loader and validator of ``facility.yaml`` (AD-2, AD-14).

Every consumer (simulator, infra, detection, ops) gets pipes, sensors, contracts,
aliases and tunables from here. Aliases are derived, never authored.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

SCHEMA_VERSION = 1
DEFAULT_PATH = Path(__file__).with_name("facility.yaml")

# Measurements and their fixed units (Consistency Conventions).
UNITS: Mapping[str, str] = MappingProxyType(
    {
        "pressure_in": "Pa",
        "pressure_out": "Pa",
        "flow_in": "kg/s",
        "flow_out": "kg/s",
        "temperature": "K",
        "methane_ppm": "ppmv",
    }
)
MEASUREMENTS: tuple[str, ...] = tuple(UNITS)

CONTRACT_FIELDS: tuple[str, ...] = ("unit", "min", "max", "rate_hz", "owner")
MIN_PEER_GROUP_SIZE = 4
PIPE_ID_RE = re.compile(r"^P-\d{3}$")
_SAFE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
STAGE_RE = re.compile(r"^poc(-[a-z0-9]{1,12})?$")

_TOP_KEYS = {"version", "facility", "areas", "contracts", "pipes", "tunables"}
_PIPE_REQUIRED = {"id", "area", "service", "material", "age_years", "peer_group", "sensors"}
_PIPE_KEYS = _PIPE_REQUIRED | {"contract_overrides"}
_TUNABLE_KEYS = ("window_len_s", "ingest_lag_s", "quiet_runs_to_close", "reopen_grace_s", "dq_min")


class FacilityError(ValueError):
    """Raised when facility.yaml violates schema v1."""


@dataclass(frozen=True)
class Contract:
    unit: str
    min: float
    max: float
    rate_hz: float
    owner: str


@dataclass(frozen=True)
class Pipe:
    id: str
    area: str
    service: str
    material: str
    age_years: int
    peer_group: str
    sensors: tuple[str, ...]
    contracts: Mapping[str, Contract]  # measurement -> effective contract (defaults + overrides)


@dataclass(frozen=True)
class DerivedProperty:
    """One row of the Derived properties v1 table (AD-13)."""

    name: str
    level: str  # "pipe" | "sensor"
    type: str  # SiteWise data type
    measurement: str | None = None  # set for sensor-level properties


@dataclass(frozen=True)
class AttributeProperty:
    """One pipe attribute of the SiteWise Pipe model (AD-14)."""

    name: str
    type: str  # SiteWise data type
    measurement: str | None = None  # set for contract attributes
    field: str | None = None  # contract field for contract attributes


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    areas: tuple[str, ...]
    pipes: tuple[Pipe, ...]
    contract_defaults: Mapping[str, Mapping[str, Any]]  # authored default contract fields per measurement
    tunables: Mapping[str, Any]
    stage: str
    alias_root: str

    # -- lookups -----------------------------------------------------------------

    def pipe(self, pipe_id: str) -> Pipe:
        for p in self.pipes:
            if p.id == pipe_id:
                return p
        raise FacilityError(f"unknown pipe {pipe_id!r}")

    def peer_groups(self) -> dict[str, tuple[str, ...]]:
        groups: dict[str, list[str]] = {}
        for p in self.pipes:
            groups.setdefault(p.peer_group, []).append(p.id)
        return {g: tuple(ids) for g, ids in groups.items()}

    # -- aliases (AD-1, AD-14, Conventions) -------------------------------------

    def _pipe_root(self, pipe_id: str) -> str:
        p = self.pipe(pipe_id)
        return f"{self.alias_root}/{p.area}/{p.id}"

    def alias(self, pipe_id: str, measurement: str) -> str:
        """Raw alias ``<alias_root>/<area>/<pipe_id>/<measurement>``."""
        if measurement not in self.pipe(pipe_id).sensors:
            raise FacilityError(f"pipe {pipe_id}: no sensor {measurement!r}")
        return f"{self._pipe_root(pipe_id)}/{measurement}"

    def derived_alias(self, pipe_id: str, name: str) -> str:
        """Derived alias ``<alias_root>/<area>/<pipe_id>/derived/<name>`` (AD-13)."""
        p = self.pipe(pipe_id)
        valid = {d.name for d in self.derived_properties() if d.measurement is None or d.measurement in p.sensors}
        if name not in valid:
            raise FacilityError(f"pipe {pipe_id}: unknown derived property {name!r}")
        return f"{self._pipe_root(pipe_id)}/derived/{name}"

    def attr_alias(self, pipe_id: str, name: str) -> str:
        """Attribute alias ``<alias_root>/<area>/<pipe_id>/attr/<name>``."""
        p = self.pipe(pipe_id)
        valid = {a.name for a in self.attribute_properties() if a.measurement is None or a.measurement in p.sensors}
        if name not in valid:
            raise FacilityError(f"pipe {pipe_id}: unknown attribute {name!r}")
        return f"{self._pipe_root(pipe_id)}/attr/{name}"

    # -- SiteWise property catalogue --------------------------------------------

    @staticmethod
    def derived_properties() -> tuple[DerivedProperty, ...]:
        """Derived properties v1: name, level, SiteWise type."""
        rows = [
            DerivedProperty("alert_state", "pipe", "INTEGER"),
            DerivedProperty("alert_summary", "pipe", "STRING"),
        ]
        for m in MEASUREMENTS:
            rows.append(DerivedProperty(f"{m}_dq", "sensor", "DOUBLE", m))
            rows.append(DerivedProperty(f"{m}_dq_fail", "sensor", "STRING", m))
        return tuple(rows)

    @staticmethod
    def attribute_properties() -> tuple[AttributeProperty, ...]:
        """Pipe attributes per AD-14, with their SiteWise types."""
        rows = [
            AttributeProperty("pipe_id", "STRING"),
            AttributeProperty("area", "STRING"),
            AttributeProperty("service", "STRING"),
            AttributeProperty("material", "STRING"),
            AttributeProperty("age_years", "INTEGER"),
            AttributeProperty("peer_group", "STRING"),
        ]
        field_types = {"unit": "STRING", "min": "DOUBLE", "max": "DOUBLE", "rate_hz": "DOUBLE", "owner": "STRING"}
        for m in MEASUREMENTS:
            for f in CONTRACT_FIELDS:
                rows.append(AttributeProperty(f"{m}_{f}", field_types[f], m, f))
        return tuple(rows)

    def attribute_values(self, pipe_id: str) -> dict[str, str | int | float]:
        """Per-pipe attribute values, keyed by attribute name (contract overrides applied)."""
        p = self.pipe(pipe_id)
        values: dict[str, str | int | float] = {
            "pipe_id": p.id,
            "area": p.area,
            "service": p.service,
            "material": p.material,
            "age_years": p.age_years,
            "peer_group": p.peer_group,
        }
        for m in p.sensors:
            c = p.contracts[m]
            for f in CONTRACT_FIELDS:
                values[f"{m}_{f}"] = getattr(c, f)
        return values

    # -- tunables -----------------------------------------------------------------

    def config_hash(self) -> str:
        """sha256 of the canonical JSON of ``tunables`` (AD-14)."""
        canonical = json.dumps(_thaw(self.tunables), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- loading ------------------------------------------------------------------------


def alias_root_for(facility_id: str, stage: str) -> str:
    """``/<facility>`` for stage ``poc``, ``/<stage>/<facility>`` otherwise (AD-16)."""
    return f"/{facility_id}" if stage == "poc" else f"/{stage}/{facility_id}"


def load(path: str | Path = DEFAULT_PATH, stage: str = "poc") -> Facility:
    """Parse and validate ``facility.yaml`` schema v1. Raises ``FacilityError``."""
    if not isinstance(stage, str) or not STAGE_RE.match(stage):
        raise FacilityError(f"invalid stage {stage!r}: expected 'poc' or 'poc-<initials>'")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise FacilityError(f"{path}: invalid YAML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise FacilityError(f"{path}: cannot read: {exc}") from exc
    return parse(raw, stage=stage)


def parse(raw: Any, stage: str = "poc") -> Facility:
    """Validate an already-decoded document. Used by ``load``; exposed for tests."""
    if not isinstance(stage, str) or not STAGE_RE.match(stage):
        raise FacilityError(f"invalid stage {stage!r}: expected 'poc' or 'poc-<initials>'")
    doc = _mapping(raw, "document")
    unknown = set(doc) - _TOP_KEYS
    if unknown:
        raise FacilityError(f"unknown top-level keys: {sorted(unknown)}")
    missing = _TOP_KEYS - set(doc)
    if missing:
        raise FacilityError(f"missing top-level keys: {sorted(missing)}")
    if type(doc["version"]) is not int or doc["version"] != SCHEMA_VERSION:
        raise FacilityError(f"unsupported schema version {doc['version']!r}; expected {SCHEMA_VERSION}")

    fac = _mapping(doc["facility"], "facility")
    fac_id = _str(fac.get("id"), "facility.id")
    if not _SAFE_NAME_RE.match(fac_id):
        raise FacilityError(f"facility.id {fac_id!r} must use only lowercase letters, digits, _ or -")
    fac_name = _str(fac.get("name"), "facility.name")

    areas_raw = doc["areas"]
    if not isinstance(areas_raw, list) or not areas_raw:
        raise FacilityError("areas must be a non-empty list")
    areas = tuple(_str(a, "areas[]") for a in areas_raw)
    for a in areas:
        if not _SAFE_NAME_RE.match(a):
            raise FacilityError(f"area {a!r} must use only lowercase letters, digits, _ or -")
    dup_areas = [a for a, n in Counter(areas).items() if n > 1]
    if dup_areas:
        raise FacilityError(f"duplicate areas: {dup_areas}")

    defaults = _mapping(doc["contracts"], "contracts")
    for m, c in defaults.items():
        if m not in UNITS:
            raise FacilityError(f"contracts: unknown measurement {m!r}")
        _check_contract_keys(_mapping(c, f"contracts.{m}"), f"contracts.{m}")

    pipes_raw = doc["pipes"]
    if not isinstance(pipes_raw, list) or not pipes_raw:
        raise FacilityError("pipes must be a non-empty list")

    pipes: list[Pipe] = []
    seen: set[str] = set()
    for i, pr in enumerate(pipes_raw):
        pr = _mapping(pr, f"pipes[{i}]")
        pid = pr.get("id")
        if not isinstance(pid, str) or not PIPE_ID_RE.match(pid):
            raise FacilityError(f"pipes[{i}]: invalid pipe id {pid!r}; expected P-\\d{{3}}")
        if pid in seen:
            raise FacilityError(f"duplicate pipe id {pid}")
        seen.add(pid)
        pipes.append(_parse_pipe(pid, pr, areas, defaults))

    groups = Counter(p.peer_group for p in pipes)
    small = sorted(g for g, n in groups.items() if n < MIN_PEER_GROUP_SIZE)
    if small:
        detail = ", ".join(f"{g} ({groups[g]} pipes)" for g in small)
        raise FacilityError(f"peer groups need at least {MIN_PEER_GROUP_SIZE} pipes: {detail}")

    tunables = _mapping(doc["tunables"], "tunables")
    for k in _TUNABLE_KEYS:
        if k not in tunables:
            raise FacilityError(f"tunables: missing {k!r}")
        if not _is_number(tunables[k]):
            raise FacilityError(f"tunables.{k} must be a finite number")
    for k in ("window_len_s", "reopen_grace_s"):
        if not tunables[k] > 0:
            raise FacilityError(f"tunables.{k} must be > 0")
    if not tunables["ingest_lag_s"] >= 0:
        raise FacilityError("tunables.ingest_lag_s must be >= 0")
    if not isinstance(tunables["quiet_runs_to_close"], int) or tunables["quiet_runs_to_close"] < 1:
        raise FacilityError("tunables.quiet_runs_to_close must be an integer >= 1")
    if not 0 <= tunables["dq_min"] <= 1:
        raise FacilityError("tunables.dq_min must be within [0, 1]")
    _mapping(tunables.get("detectors", {}), "tunables.detectors")

    return Facility(
        id=fac_id,
        name=fac_name,
        areas=areas,
        pipes=tuple(pipes),
        contract_defaults=_freeze(defaults),
        tunables=_freeze(tunables),
        stage=stage,
        alias_root=alias_root_for(fac_id, stage),
    )


def _parse_pipe(pid: str, pr: Mapping[str, Any], areas: tuple[str, ...], defaults: Mapping[str, Any]) -> Pipe:
    unknown = set(pr) - _PIPE_KEYS
    if unknown:
        raise FacilityError(f"pipe {pid}: unknown keys {sorted(unknown)}")
    missing = _PIPE_REQUIRED - set(pr)
    if missing:
        raise FacilityError(f"pipe {pid}: missing keys {sorted(missing)}")

    area = _str(pr["area"], f"pipe {pid}: area")
    if area not in areas:
        raise FacilityError(f"pipe {pid}: area {area!r} is not in areas {list(areas)}")
    service = _str(pr["service"], f"pipe {pid}: service")
    material = _str(pr["material"], f"pipe {pid}: material")
    peer_group = _str(pr["peer_group"], f"pipe {pid}: peer_group")
    age = pr["age_years"]
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        raise FacilityError(f"pipe {pid}: age_years must be a non-negative integer")

    sensors_raw = pr["sensors"]
    if not isinstance(sensors_raw, list) or not sensors_raw:
        raise FacilityError(f"pipe {pid}: sensors must be a non-empty list")
    for s in sensors_raw:
        if s not in UNITS:
            raise FacilityError(f"pipe {pid}: unknown sensor {s!r}; expected one of {list(MEASUREMENTS)}")
    if len(set(sensors_raw)) != len(sensors_raw):
        raise FacilityError(f"pipe {pid}: duplicate sensors")
    sensors = tuple(sensors_raw)

    overrides = _mapping(pr.get("contract_overrides") or {}, f"pipe {pid}: contract_overrides")
    for m, o in overrides.items():
        if m not in sensors:
            raise FacilityError(f"pipe {pid}: contract_overrides for {m!r}, which is not a sensor of this pipe")
        _check_contract_keys(_mapping(o, f"pipe {pid}: contract_overrides.{m}"), f"pipe {pid}: contract_overrides.{m}")

    contracts: dict[str, Contract] = {}
    for m in sensors:
        merged = {**dict(defaults.get(m) or {}), **dict(overrides.get(m) or {})}
        contracts[m] = _contract(pid, m, merged)

    return Pipe(
        id=pid,
        area=area,
        service=service,
        material=material,
        age_years=age,
        peer_group=peer_group,
        sensors=sensors,
        contracts=MappingProxyType(contracts),
    )


def _contract(pid: str, m: str, c: Mapping[str, Any]) -> Contract:
    where = f"pipe {pid}, measurement {m}"
    missing = [f for f in CONTRACT_FIELDS if c.get(f) in (None, "")]
    if missing:
        raise FacilityError(f"{where}: incomplete contract, missing {missing}")
    if c["unit"] != UNITS[m]:
        raise FacilityError(f"{where}: unit {c['unit']!r} must be {UNITS[m]!r}")
    for f in ("min", "max", "rate_hz"):
        if not _is_number(c[f]):
            raise FacilityError(f"{where}: contract {f} must be a finite number")
    if not c["min"] < c["max"]:
        raise FacilityError(f"{where}: contract min must be below max")
    if not c["rate_hz"] > 0:
        raise FacilityError(f"{where}: contract rate_hz must be positive")
    if not isinstance(c["owner"], str):
        raise FacilityError(f"{where}: contract owner must be a string")
    return Contract(
        unit=c["unit"], min=float(c["min"]), max=float(c["max"]), rate_hz=float(c["rate_hz"]), owner=c["owner"]
    )


def _check_contract_keys(c: Mapping[str, Any], where: str) -> None:
    unknown = set(c) - set(CONTRACT_FIELDS)
    if unknown:
        raise FacilityError(f"{where}: unknown contract fields {sorted(unknown)}")


def _mapping(v: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise FacilityError(f"{where} must be a mapping")
    return v


def _str(v: Any, where: str) -> str:
    if not isinstance(v, str) or not v:
        raise FacilityError(f"{where} must be a non-empty string")
    return v


def _is_number(v: Any) -> bool:
    """A finite int or float; bools are rejected."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _freeze(v: Any) -> Any:
    if isinstance(v, Mapping):
        return MappingProxyType({k: _freeze(x) for k, x in v.items()})
    if isinstance(v, list):
        return tuple(_freeze(x) for x in v)
    return v


def _thaw(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {k: _thaw(x) for k, x in v.items()}
    if isinstance(v, tuple):
        return [_thaw(x) for x in v]
    return v
