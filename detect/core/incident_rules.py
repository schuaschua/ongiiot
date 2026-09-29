"""Pure incident rules: one OPEN incident per pipe (AD-6) and the alert values derived from it (AD-13).

``decide`` takes one pipe's current state (its OPEN incident, if any, and its latest closed one), the
run's findings on that pipe and the detectors that may count as quiet on it, and returns what the run
does to that pipe's incident:

- ``open``: findings and no OPEN incident (and no quiet close within ``reopen_grace_s``): a new incident.
- ``reopen``: findings within ``reopen_grace_s`` of the pipe's latest close, when that close was ``quiet``:
  the same incident (same ``incident_id``, ``notified_at`` kept) is OPEN again. A ``reset`` close never reopens.
- ``update``: an OPEN incident that stays open (findings upserted and/or quiet counters moved).
- ``close``: every detector of the OPEN incident has ``quiet_runs >= quiet_runs_to_close``.
- ``None``: no OPEN incident and no findings: nothing to do.

``reset_close`` is the demo reset's close (AD-15): the OPEN incident becomes CLOSED with ``closed_reason=reset``,
whatever its quiet counters say. A reset close never reopens (``_reopens`` requires ``quiet``).

Upsert of a detector: latest confidence (the highest of its findings on the pipe this run), the explanation
of that finding, its version, ``first_seen`` kept, ``last_seen`` = the run's ``window_end``, ``quiet_runs = 0``.
Evidence aliases are a sorted union; ``evidence_to`` only widens; ``evidence_from`` is fixed at open.
Incident ``confidence`` = max over detectors, unrounded.

Quiet counting: a detector of the incident with no finding this run gets ``quiet_runs + 1`` only if it is in
``quiet_ok`` (it ran OK and covered the pipe); otherwise its counter is unchanged.

No I/O here: the adapter (``detect/adapters/incidents.py``) reads and writes the table (AD-5 layering).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from detect.core.models import Finding, Tunables

OPEN = "OPEN"
CLOSED = "CLOSED"
QUIET = "quiet"
RESET = "reset"
ALERT_SUMMARY_MAX_BYTES = 1024

Op = Literal["open", "update", "reopen", "close"]


@dataclass(frozen=True)
class DetectorState:
    confidence: float
    explanation: str
    version: str
    first_seen: datetime
    last_seen: datetime
    quiet_runs: int


@dataclass(frozen=True)
class Incident:
    incident_id: str
    pipe_id: str
    area: str
    status: str  # OPEN | CLOSED
    opened_at: datetime
    last_seen: datetime
    evidence_from: datetime
    evidence_to: datetime
    evidence_aliases: tuple[str, ...]  # sorted union over the incident's lifetime
    detectors: Mapping[str, DetectorState]
    confidence: float  # max of detectors[*].confidence, unrounded
    notified_at: str | None = None  # outbox marker (AD-7), opaque here
    closed_at: datetime | None = None
    closed_reason: str | None = None


@dataclass(frozen=True)
class Decision:
    op: Op | None
    incident: Incident | None  # after the run: OPEN, CLOSED (op ``close``) or None


def check_tunables(t: Tunables) -> None:
    """Raise ``ValueError`` unless ``quiet_runs_to_close`` (int >= 1) and ``reopen_grace_s`` (number >= 0) are valid."""
    q = t.get("quiet_runs_to_close")
    if isinstance(q, bool) or not isinstance(q, int) or q < 1:  # an int is always finite
        raise ValueError(f"tunables.quiet_runs_to_close must be an integer >= 1, got {q!r}")
    g = t.get("reopen_grace_s")
    if isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(g) or g < 0:
        raise ValueError(f"tunables.reopen_grace_s must be a finite number >= 0, got {g!r}")


def _upserts(findings: Iterable[Finding]) -> dict[str, Finding]:
    """detector_id -> its strongest finding (ties: the first in evidence order), for one pipe."""
    best: dict[str, Finding] = {}
    for f in sorted(findings, key=lambda x: (x.detector_id, x.evidence_aliases)):
        cur = best.get(f.detector_id)
        if cur is None or f.confidence > cur.confidence:
            best[f.detector_id] = f
    return best


def decide(
    *,
    pipe_id: str,
    area: str,
    open_incident: Incident | None,
    last_closed: Incident | None,
    findings: Sequence[Finding],
    quiet_ok: Collection[str],
    now: datetime,
    tunables: Tunables,
    new_id: Callable[[], str],
) -> Decision:
    """What this run does to ``pipe_id``'s incident. ``now`` is the run's ``window_end``."""
    own = [f for f in findings if f.pipe_id == pipe_id]
    ups = _upserts(own)
    base = open_incident
    op: Op | None = None
    if base is None:
        if not ups:
            return Decision(None, None)
        if last_closed is not None and _reopens(last_closed, now, tunables):
            base = replace(last_closed, status=OPEN, closed_at=None, closed_reason=None)
            op = "reopen"
        else:
            base = Incident(
                incident_id=new_id(),
                pipe_id=pipe_id,
                area=area,
                status=OPEN,
                opened_at=now,
                last_seen=now,
                evidence_from=min(f.window_start for f in own),
                evidence_to=max(f.window_end for f in own),
                evidence_aliases=(),
                detectors=MappingProxyType({}),
                confidence=0.0,
            )
            op = "open"
    else:
        op = "update"

    detectors: dict[str, DetectorState] = {}
    for det_id, st in base.detectors.items():
        if det_id in ups:
            continue
        detectors[det_id] = replace(st, quiet_runs=st.quiet_runs + 1) if det_id in quiet_ok else st
    for det_id, f in ups.items():
        prev = base.detectors.get(det_id)
        detectors[det_id] = DetectorState(
            confidence=float(f.confidence),
            explanation=f.explanation,
            version=f.detector_version,
            first_seen=prev.first_seen if prev is not None else now,
            last_seen=now,
            quiet_runs=0,
        )
    detectors = dict(sorted(detectors.items()))
    if not detectors:  # a stored OPEN item without detectors: nothing can keep it open
        return Decision("close", replace(base, status=CLOSED, closed_at=now, closed_reason=QUIET))
    aliases = tuple(sorted(set(base.evidence_aliases) | {a for f in own for a in f.evidence_aliases}))
    evidence_to = max([base.evidence_to, *(f.window_end for f in own)])
    incident = replace(
        base,
        detectors=MappingProxyType(detectors),
        evidence_aliases=aliases,
        evidence_to=evidence_to,
        last_seen=max(s.last_seen for s in detectors.values()),
        confidence=max(s.confidence for s in detectors.values()),
    )
    if op == "update" and all(s.quiet_runs >= tunables["quiet_runs_to_close"] for s in detectors.values()):
        return Decision("close", replace(incident, status=CLOSED, closed_at=now, closed_reason=QUIET))
    return Decision(op, incident)


def reset_close(incident: Incident, now: datetime) -> Decision:
    """Close ``incident`` for a demo reset (AD-15): ``status=CLOSED``, ``closed_at=now``, ``closed_reason=reset``."""
    return Decision("close", replace(incident, status=CLOSED, closed_at=now, closed_reason=RESET))


def _reopens(closed: Incident, now: datetime, tunables: Tunables) -> bool:
    if closed.closed_reason != QUIET or closed.closed_at is None:
        return False
    return (now - closed.closed_at).total_seconds() <= tunables["reopen_grace_s"]


def latest_closed(closed: Iterable[Incident]) -> Incident | None:
    """The pipe's most recently closed incident (by ``closed_at``, then ``incident_id``), or None."""
    items = [c for c in closed if c.closed_at is not None]
    return max(items, key=lambda c: (c.closed_at, c.incident_id)) if items else None


def alert_state(incident: Incident | None) -> int:
    """Number of detectors in the OPEN incident, or 0 (AD-13)."""
    return len(incident.detectors) if incident is not None and incident.status == OPEN else 0


def alert_summary(incident: Incident | None, max_bytes: int = ALERT_SUMMARY_MAX_BYTES) -> str:
    """Compact JSON ``{"incident_id","confidence","detectors":[{"id","c"}],"evidence":[alias]}`` or ``{}``,
    at most ``max_bytes`` UTF-8 bytes: ``evidence`` is dropped first, then trailing detectors; ``{}`` if even
    that does not fit."""
    if incident is None or incident.status != OPEN:
        return "{}"
    doc = {
        "incident_id": incident.incident_id,
        "confidence": round(incident.confidence, 2),
        "detectors": [{"id": k, "c": round(v.confidence, 2)} for k, v in sorted(incident.detectors.items())],
        "evidence": list(incident.evidence_aliases),
    }

    def dump(d: Mapping[str, object]) -> str:
        return json.dumps(d, separators=(",", ":"), ensure_ascii=False)

    out = dump(doc)
    if len(out.encode()) <= max_bytes:
        return out
    del doc["evidence"]
    while len(dump(doc).encode()) > max_bytes and doc["detectors"]:
        doc["detectors"] = doc["detectors"][:-1]
    out = dump(doc)
    return out if len(out.encode()) <= max_bytes else "{}"
