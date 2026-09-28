"""sim_control item v1: the injection shape shared by ``sim/`` and ``ops/`` (AD-4, AD-15).

Items are immutable. A cancel is a new item whose ``supersedes`` names the injection it
cancels; the cancelled injection then ends at the canceller's ``end_at`` (a cancel never
extends a window). A cancel item is written with ``start_at == end_at`` so it never applies itself.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from facility.model import Facility, FacilityError

ITEM_VERSION = 1

# Injection catalogue (Consistency Conventions): type -> allowed params.
CATALOGUE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "leak": frozenset({"rate_kg_s"}),
        "dq_flatline": frozenset(),
        "dq_stuck": frozenset(),
        "dq_drift": frozenset({"per_min"}),
        "dq_gap": frozenset(),
    }
)
INJECTION_TYPES: tuple[str, ...] = tuple(CATALOGUE)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_ITEM_KEYS = {"pk", "type", "pipe_id", "measurement", "params", "start_at", "end_at", "supersedes"}


class ControlError(ValueError):
    """Raised when a sim_control item violates v1 or the facility model."""


def format_ts(dt: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.sssZ`` (AD-9)."""
    if dt.tzinfo is None:
        raise ControlError("timestamps must be timezone-aware UTC")
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_ts(s: Any, where: str = "timestamp") -> datetime:
    if not isinstance(s, str):
        raise ControlError(f"{where} must be a string")
    try:
        return datetime.strptime(s, _TS_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ControlError(f"{where} {s!r} is not YYYY-MM-DDTHH:MM:SS.sssZ") from exc


@dataclass(frozen=True)
class Injection:
    injection_id: str
    type: str
    pipe_id: str
    start_at: datetime
    end_at: datetime
    measurement: str | None = None
    params: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    supersedes: str | None = None

    @property
    def is_cancel(self) -> bool:
        return self.supersedes is not None

    def active_at(self, now: datetime) -> bool:
        return self.start_at <= now < self.end_at


def validate(inj: Injection, facility: Facility | None = None) -> Injection:
    """Check an injection against v1, the catalogue and (if given) the facility model."""
    if not isinstance(inj.injection_id, str) or not inj.injection_id:
        raise ControlError("injection_id must be a non-empty string")
    if not isinstance(inj.type, str) or inj.type not in CATALOGUE:
        raise ControlError(f"unknown injection type {inj.type!r}; expected one of {list(INJECTION_TYPES)}")
    if not isinstance(inj.pipe_id, str) or not inj.pipe_id:
        raise ControlError("pipe_id must be a non-empty string")
    if inj.measurement is not None and (not isinstance(inj.measurement, str) or not inj.measurement):
        raise ControlError("measurement must be a non-empty string")
    if inj.supersedes is not None and (not isinstance(inj.supersedes, str) or not inj.supersedes):
        raise ControlError("supersedes must be absent or a non-empty string")
    if inj.type.startswith("dq_"):
        if not inj.measurement:
            raise ControlError(f"{inj.type} requires a measurement")
    elif inj.measurement is not None:
        raise ControlError(f"{inj.type} takes no measurement")
    for k, v in inj.params.items():
        if k not in CATALOGUE[inj.type]:
            raise ControlError(f"{inj.type}: unknown param {k!r}; allowed {sorted(CATALOGUE[inj.type])}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ControlError(f"{inj.type}: param {k} must be a finite number")
    if "rate_kg_s" in inj.params and not inj.params["rate_kg_s"] > 0:
        raise ControlError("leak: rate_kg_s must be > 0")
    if "per_min" in inj.params and inj.params["per_min"] == 0:
        raise ControlError("dq_drift: per_min must be non-zero")
    for name in ("start_at", "end_at"):
        ts = getattr(inj, name)
        if not isinstance(ts, datetime) or ts.tzinfo is None:
            raise ControlError(f"{name} must be a timezone-aware datetime")
    if inj.is_cancel:
        if inj.supersedes == inj.injection_id:
            raise ControlError("an item cannot supersede itself")
        if inj.end_at < inj.start_at:
            raise ControlError("end_at must not be before start_at")
    elif not inj.start_at < inj.end_at:
        raise ControlError("start_at must be before end_at")
    if facility is not None:
        try:
            pipe = facility.pipe(inj.pipe_id)
        except FacilityError as exc:
            raise ControlError(str(exc)) from exc
        if inj.measurement is not None and inj.measurement not in pipe.sensors:
            raise ControlError(f"pipe {inj.pipe_id}: no sensor {inj.measurement!r}")
        if inj.type == "leak" and not {"flow_in", "flow_out"} <= set(pipe.sensors):
            raise ControlError(f"pipe {inj.pipe_id}: leak needs flow_in and flow_out sensors")
    return inj


def new_injection(
    injection_id: str,
    type_: str,
    pipe_id: str,
    start_at: datetime,
    end_at: datetime,
    measurement: str | None = None,
    params: Mapping[str, float] | None = None,
    supersedes: str | None = None,
    facility: Facility | None = None,
) -> Injection:
    inj = Injection(
        injection_id=injection_id,
        type=type_,
        pipe_id=pipe_id,
        start_at=start_at,
        end_at=end_at,
        measurement=measurement,
        params=MappingProxyType({k: float(v) if _is_num(v) else v for k, v in (params or {}).items()}),
        supersedes=supersedes,
    )
    return validate(inj, facility)


def cancel_of(target: Injection, injection_id: str, now: datetime) -> Injection:
    """A cancel item for ``target``: same target fields, empty window at ``now``."""
    if target.is_cancel:
        raise ControlError(f"{target.injection_id} is itself a cancel item")
    return validate(
        Injection(
            injection_id=injection_id,
            type=target.type,
            pipe_id=target.pipe_id,
            measurement=target.measurement,
            start_at=now,
            end_at=now,
            supersedes=target.injection_id,
        )
    )


# -- DynamoDB conversion (boto3 resource format: numbers are Decimal) ---------------------


def to_item(inj: Injection) -> dict[str, Any]:
    item: dict[str, Any] = {
        "pk": inj.injection_id,
        "type": inj.type,
        "pipe_id": inj.pipe_id,
        "params": {k: Decimal(repr(float(v))) for k, v in inj.params.items()},
        "start_at": format_ts(inj.start_at),
        "end_at": format_ts(inj.end_at),
    }
    if inj.measurement is not None:
        item["measurement"] = inj.measurement
    if inj.supersedes is not None:
        item["supersedes"] = inj.supersedes
    return item


def from_item(item: Mapping[str, Any], facility: Facility | None = None) -> Injection:
    if not isinstance(item, Mapping):
        raise ControlError("item must be a mapping")
    unknown = set(item) - _ITEM_KEYS
    if unknown:
        raise ControlError(f"unknown item fields {sorted(unknown)}")
    for k in ("pk", "type", "pipe_id", "start_at", "end_at"):
        if k not in item:
            raise ControlError(f"item missing {k!r}")
    params_raw = item.get("params") or {}
    if not isinstance(params_raw, Mapping):
        raise ControlError("params must be a map")
    params = {}
    for k, v in params_raw.items():
        if isinstance(v, Decimal):
            v = float(v)
        params[k] = v
    return new_injection(
        injection_id=item["pk"],
        type_=item["type"],
        pipe_id=item["pipe_id"],
        start_at=parse_ts(item["start_at"], "start_at"),
        end_at=parse_ts(item["end_at"], "end_at"),
        measurement=item.get("measurement"),
        params=params,
        supersedes=item.get("supersedes"),
        facility=facility,
    )


# -- resolution -------------------------------------------------------------------------


def resolve(items: Iterable[Injection]) -> list[Injection]:
    """Apply ``supersedes``: a cancelled injection ends at its earliest canceller's ``end_at``, never later.

    Returns all items with effective windows, in ``(start_at, injection_id)`` order.
    """
    items = list(items)
    cancel_end: dict[str, datetime] = {}
    for c in items:
        if c.is_cancel:
            prev = cancel_end.get(c.supersedes)
            cancel_end[c.supersedes] = c.end_at if prev is None else min(prev, c.end_at)
    out = []
    for inj in items:
        if inj.injection_id in cancel_end and not inj.is_cancel:
            inj = replace(inj, end_at=min(inj.end_at, cancel_end[inj.injection_id]))
        out.append(inj)
    return sorted(out, key=lambda i: (i.start_at, i.injection_id))


def effective(items: Iterable[Injection], now: datetime) -> list[Injection]:
    """Injections applied at ``now``: windows resolved, ``start_at <= now < end_at``."""
    return [i for i in resolve(items) if not i.is_cancel and i.active_at(now)]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
