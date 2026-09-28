"""Shared detector types, v1 (spine: Shared Shapes; AD-5).

Every detector imports its types from here only. Times are tz-aware UTC ``datetime``;
conversion from SiteWise representations happens only in ``detect/adapters/`` (AD-9).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

Quality = Literal["GOOD", "BAD", "UNCERTAIN"]
QUALITIES: frozenset[str] = frozenset({"GOOD", "BAD", "UNCERTAIN"})

Tunables = Mapping[str, Any]  # facility.yaml `tunables`


@dataclass(frozen=True)
class Sample:
    ts: datetime  # tz-aware UTC; gaps are absent samples, never filled
    value: float | None
    quality: Quality


@dataclass(frozen=True)
class Contract:
    unit: str
    min: float
    max: float
    rate_hz: float
    owner: str


@dataclass(frozen=True)
class PipeMeta:
    pipe_id: str
    area: str
    service: str
    material: str
    age_years: int
    peer_group: str
    aliases: Mapping[str, str]  # measurement -> raw alias
    contracts: Mapping[str, Contract]  # measurement -> contract
    dq: Mapping[str, float] = field(default_factory=dict)  # measurement -> dq; filled after pass 1


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    series: Mapping[str, list[Sample]]  # alias -> samples, ascending


@dataclass(frozen=True)
class PipeWindow:
    pipe: PipeMeta
    window: Window  # only that pipe's aliases


@dataclass(frozen=True)
class FacilityWindow:
    pipes: Mapping[str, PipeMeta]
    window: Window


@dataclass(frozen=True)
class Finding:
    detector_id: str
    detector_version: str
    pipe_id: str
    evidence_aliases: tuple[str, ...]  # dq_* findings name exactly one alias
    window_start: datetime
    window_end: datetime
    confidence: float  # 0..1
    explanation: str  # one plain sentence


DetectorKind = Literal["pipe", "facility"]


@runtime_checkable
class PipeDetector(Protocol):
    """Called once per pipe with that pipe's window (AD-5). Pure: no I/O, no environment, no ``_dq``."""

    id: str
    version: str
    kind: Literal["pipe"]

    def detect(self, pw: PipeWindow, t: Tunables) -> list[Finding]: ...


@runtime_checkable
class FacilityDetector(Protocol):
    """Called once per run with the whole facility window (AD-5). Pure: no I/O, no environment, no ``_dq``."""

    id: str
    version: str
    kind: Literal["facility"]

    def detect(self, fw: FacilityWindow, t: Tunables) -> list[Finding]: ...


Detector = PipeDetector | FacilityDetector
