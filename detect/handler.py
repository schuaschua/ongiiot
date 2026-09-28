"""Detection Lambda entry point: ``detect.handler.handler`` (AD-5, AD-9, AD-12, AD-13, AD-18).

One run per EventBridge Scheduler invocation, event ``{"scheduled_time": "<ISO-8601 UTC>"}``:

1. ``run_id = scheduled_time``; the window is aligned from it (AD-9), never from ``now()``.
2. Read the ``FacilityWindow`` through the reader adapter (``READER_STRATEGY``).
3. Pass 1: the ``dq_*`` detectors (``dq_flatline``, ``dq_stuck``, ``dq_gap``: pipe; ``dq_drift``: facility),
   dispatched by ``kind`` (``pipe``: once per pipe; ``facility``: once per run), then ``dq_score`` with
   the no-sample gap rule (one ``dq_gap`` finding per empty alias).
4. Pass 2: the ``leak_*`` detectors (``leak_mass_balance``: pipe; ``leak_methane_rise``: facility) and
   ``outlier_peer`` (facility) on the dq-annotated window (``PipeMeta.dq`` filled); they exclude sensors with ``dq < tunables.dq_min``.
   If any pass-1 detector failed, its sensors would wrongly score 1.0, so pass 2 is skipped for that run
   (an ERROR line and ``pass2_skipped: true``).
5. Write ``<m>_dq`` and ``<m>_dq_fail`` for every sensor at ``window_end``.
5b. Incidents (AD-6, AD-12, ``detect/adapters/incidents.py``): every finding of both passes (``dq_*`` included,
   with the gap rule's) joins its pipe's one OPEN incident. A detector counts as quiet on a pipe only if it ran
   OK and covered the pipe (not listed in ``suppressed`` for it); none does when pass 2 was skipped. Then
   ``alert_state`` (INTEGER) and ``alert_summary`` (JSON STRING) for every pipe at ``window_end`` (AD-13).
6. Log one JSON summary line, with pass 2's ``findings: [{detector, pipe, confidence, explanation,
   evidence_aliases}]``, ``suppressed: [{detector, pipe, reason}]`` (a required sensor untrusted, or an ``outlier_peer`` feature skipped) and
   ``pass2_skipped``, ``incident_ops`` (``opened``/``updated``/``closed``/``reopened``: ``[{pipe, incident_id}]``)
   and ``alert_state`` (pipe -> detectors in its OPEN incident, pipes with none left out).

Environment: ``STAGE`` (required), ``INCIDENTS_TABLE`` (required), ``READER_STRATEGY`` (``batch_get`` or ``execute_query``; default
``reader.DEFAULT_STRATEGY``),
``FACILITY_PATH`` (default: the bundled ``facility.yaml``). The facility model, pipe metadata and
SiteWise clients are built once per cold start; each registered detector's tunables are checked then,
so a missing key fails the cold start.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from detect.adapters.derived_writer import DerivedWriter
from detect.adapters.incidents import Incidents
from detect.adapters.reader import DEFAULT_STRATEGY, Reader, make_reader
from detect.core import dq_score, incident_rules
from detect.core.detectors import PASS1, PASS2
from detect.core.detectors._pass2 import dq_min, dq_of, trusted, untrusted_reason
from detect.core.models import Contract, Detector, FacilityWindow, Finding, PipeMeta, PipeWindow, Tunables, Window
from detect.core.window import window_bounds
from facility.model import DEFAULT_PATH, Facility, load

PASS1_DETECTORS: Sequence[Detector] = PASS1
PASS2_DETECTORS: Sequence[Detector] = PASS2
KINDS = ("pipe", "facility")


def check_detectors(detectors: Sequence[Detector], tunables: Tunables) -> None:
    """Cold-start check: unique ids, a known ``kind`` for every detector and its tunables present and valid."""
    ids = [getattr(det, "id", None) for det in detectors]
    dupes = sorted({i for i in ids if ids.count(i) > 1}, key=str)
    if dupes:
        raise ValueError(f"duplicate detector ids: {dupes}")
    for det in detectors:
        if getattr(det, "kind", None) not in KINDS:
            raise ValueError(f"detector {getattr(det, 'id', det)!r}: kind must be one of {KINDS}")
        check = getattr(det, "check_tunables", None)
        if check is not None:
            check(tunables)


def iso(t: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.sssZ`` (AD-9 stored strings)."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse_scheduled_time(event: Mapping[str, Any]) -> datetime:
    raw = event.get("scheduled_time") if isinstance(event, Mapping) else None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"event needs a scheduled_time string, got {event!r}")
    t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"scheduled_time must carry a UTC offset, got {raw!r}")
    return t.astimezone(UTC)


def pipe_metas(facility: Facility) -> dict[str, PipeMeta]:
    """PipeMeta per pipe from the bundled facility model (the source SiteWise metadata is generated from)."""
    out = {}
    for p in facility.pipes:
        out[p.id] = PipeMeta(
            pipe_id=p.id,
            area=p.area,
            service=p.service,
            material=p.material,
            age_years=p.age_years,
            peer_group=p.peer_group,
            aliases=MappingProxyType({m: facility.alias(p.id, m) for m in p.sensors}),
            contracts=MappingProxyType(
                {
                    m: Contract(unit=c.unit, min=c.min, max=c.max, rate_hz=c.rate_hz, owner=c.owner)
                    for m, c in p.contracts.items()
                }
            ),
        )
    return out


def dq_aliases(facility: Facility) -> dict[str, tuple[str, str]]:
    """raw alias -> (``<m>_dq`` alias, ``<m>_dq_fail`` alias) for every sensor."""
    return {
        facility.alias(p.id, m): (facility.derived_alias(p.id, f"{m}_dq"), facility.derived_alias(p.id, f"{m}_dq_fail"))
        for p in facility.pipes
        for m in p.sensors
    }


def alert_aliases(facility: Facility) -> dict[str, tuple[str, str]]:
    """pipe_id -> (``alert_state`` alias, ``alert_summary`` alias)."""
    return {p.id: (facility.derived_alias(p.id, "alert_state"), facility.derived_alias(p.id, "alert_summary")) for p in facility.pipes}


def derived_targets(facility: Facility) -> list[str]:
    """Every derived alias the run may write (the writer's allowed set)."""
    return [a for pair in (*dq_aliases(facility).values(), *alert_aliases(facility).values()) for a in pair]


@dataclass
class Runtime:
    facility: Facility
    pipes: Mapping[str, PipeMeta]
    dq_targets: Mapping[str, tuple[str, str]]
    reader: Reader
    writer: DerivedWriter
    incidents: Incidents
    alert_targets: Mapping[str, tuple[str, str]]


def run(
    rt: Runtime,
    scheduled_time: datetime,
    detectors: Sequence[Detector] = PASS1_DETECTORS,
    pass2: Sequence[Detector] = PASS2_DETECTORS,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    t_run = clock()
    f = rt.facility
    tunables = f.tunables
    start, end = window_bounds(scheduled_time, tunables)
    run_id = iso(scheduled_time)

    t0 = clock()
    fw = rt.reader.read(rt.pipes, start, end)
    read_s = clock() - t0

    # Pass 1: dq_* detectors (AD-5), then dq_score owns the score (AD-13).
    findings: list[Finding] = []
    ran_ok: list[str] = []
    pass1_failed: list[str] = []
    for det in detectors:
        # A detector's findings count only if it completed on every pipe and all are well formed.
        try:
            own = _run_detector(det, fw, tunables)
        except Exception as exc:  # noqa: BLE001 - AD-5: one detector's failure never stops the run
            log(json.dumps({"stage": f.stage, "run_id": run_id, "detector_id": det.id, "error": repr(exc)}))
            pass1_failed.append(det.id)
            continue
        findings += own
        ran_ok.append(det.id)
    scores = dq_score.score_facility(fw, findings)
    pipes = {
        pid: replace(p, dq=MappingProxyType({m: scores[a][0] for m, a in p.aliases.items()}))
        for pid, p in fw.pipes.items()
    }
    fw = FacilityWindow(pipes=pipes, window=fw.window)

    # Pass 2: leak_* and outlier_peer on the dq-annotated window, with the same isolation (AD-5).
    # A failed pass-1 detector leaves its sensors at 1.0 (trusted), so pass 2 could trust a railed sensor.
    leaks: list[Finding] = []
    suppressed: list[dict[str, str]] = []
    pass2_skipped = bool(pass1_failed) and bool(pass2)
    if pass2_skipped:
        log(
            json.dumps(
                {
                    "stage": f.stage,
                    "run_id": run_id,
                    "level": "ERROR",
                    "error": "pass 2 skipped: pass-1 detector(s) failed",
                    "failed": pass1_failed,
                }
            )
        )
    for det in () if pass2_skipped else pass2:
        try:
            own = _run_detector(det, fw, tunables, check=check_pass2_finding)
            check_pass2_set(det, own)
            skipped = _suppressed(det, fw, tunables)
        except Exception as exc:  # noqa: BLE001 - AD-5: one detector's failure never stops the run
            log(json.dumps({"stage": f.stage, "run_id": run_id, "detector_id": det.id, "error": repr(exc)}))
            continue
        leaks += own
        suppressed += skipped
        ran_ok.append(det.id)

    values: dict[str, float | int | str] = {}
    for raw, (dq, failing) in scores.items():
        dq_alias, fail_alias = rt.dq_targets[raw]
        values[dq_alias] = float(dq)
        values[fail_alias] = failing
    t1 = clock()
    written = rt.writer.write(values, end)
    write_s = clock() - t1

    # Incidents (AD-6, AD-12): every finding of any family joins its pipe's incident, then alert state (AD-13).
    t2 = clock()
    applied = rt.incidents.apply(
        run_id=run_id,
        now=end,
        pipes={pid: p.area for pid, p in fw.pipes.items()},
        findings=[*dq_score.with_gap_rule(fw, findings), *leaks],
        quiet_ok=covered(fw.pipes, ran_ok, suppressed, pass2_skipped),
        tunables=tunables,
        config_hash=f.config_hash(),
    )
    alerts: dict[str, float | int | str] = {}
    for pid, inc in applied.open.items():
        state_alias, summary_alias = rt.alert_targets[pid]
        alerts[state_alias] = incident_rules.alert_state(inc)
        alerts[summary_alias] = incident_rules.alert_summary(inc)
    written += rt.writer.write(alerts, end)
    incidents_s = clock() - t2

    summary = {
        "stage": f.stage,
        "run_id": run_id,
        "config_hash": f.config_hash(),
        "window_start": iso(start),
        "window_end": iso(end),
        "reader_strategy": rt.reader.strategy,
        "read_duration_s": round(read_s, 3),
        "write_duration_s": round(write_s, 3),
        "incidents_duration_s": round(incidents_s, 3),
        "samples": sum(len(s) for s in fw.window.series.values()),
        "aliases_no_data": sorted(a for a, s in fw.window.series.items() if not s),
        "values_written": written,
        "detectors": {d.id: d.version for d in (*detectors, *pass2)},
        "ran_ok": ran_ok,
        "findings": [
            {
                "detector": x.detector_id,
                "pipe": x.pipe_id,
                "confidence": round(x.confidence, 3),
                "explanation": x.explanation,
                "evidence_aliases": list(x.evidence_aliases),
            }
            for x in leaks
        ],
        "suppressed": suppressed,
        "pass2_skipped": pass2_skipped,
        "incident_ops": applied.ops,
        "alert_state": {pid: incident_rules.alert_state(inc) for pid, inc in applied.open.items() if inc is not None},
        "run_s": round(clock() - t_run, 3),
    }
    log(json.dumps(summary, separators=(",", ":")))
    return summary


def covered(
    pipes: Mapping[str, PipeMeta], ran_ok: Sequence[str], suppressed: Sequence[Mapping[str, str]], pass2_skipped: bool
) -> dict[str, set[str]]:
    """pipe_id -> the detectors that may count as quiet on it this run: a detector covers a pipe when it ran OK and
    the pipe is not listed as suppressed or skipped for it. If pass 2 was skipped, no detector counts as quiet."""
    if pass2_skipped:
        return {pid: set() for pid in pipes}
    out = {pid: set(ran_ok) for pid in pipes}
    for s in suppressed:
        out.get(s["pipe"], set()).discard(s["detector"])
    return out


def check_pass2_finding(finding: Finding, known_aliases: Sequence[str]) -> None:
    """Raise ``ValueError`` unless a pass-2 finding is well formed: not a ``dq_*`` id, a finite confidence
    in [0, 1], at least one evidence alias, all from its own pipe, and a non-empty explanation."""
    if dq_score.is_dq(finding):
        raise ValueError(f"{finding.detector_id}: a pass-2 detector cannot emit dq_* findings")
    c = finding.confidence
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or not 0 <= c <= 1:
        raise ValueError(f"{finding.detector_id}: confidence must be in [0, 1], got {c!r}")
    known = set(known_aliases)
    if not finding.evidence_aliases or any(a not in known for a in finding.evidence_aliases):
        raise ValueError(f"{finding.detector_id}: evidence {finding.evidence_aliases} is not an expected alias set of the pipe")
    if not isinstance(finding.explanation, str) or not finding.explanation.strip():
        raise ValueError(f"{finding.detector_id}: empty explanation")


def check_pass2_set(det: Detector, found: Sequence[Finding]) -> None:
    """Raise ``ValueError`` unless every finding carries ``det.id`` and no pipe is named twice."""
    seen: set[str] = set()
    for x in found:
        if x.detector_id != det.id:
            raise ValueError(f"{det.id}: finding carries detector_id {x.detector_id!r}")
        if x.pipe_id in seen:
            raise ValueError(f"{det.id}: more than one finding for {x.pipe_id}")
        seen.add(x.pipe_id)


def check_passes(pass1: Sequence[Detector], pass2: Sequence[Detector], tunables: Tunables) -> None:
    """Cold start: both passes together (ids unique across passes, kinds, tunables); pass 1 holds only
    ``dq_*`` ids and pass 2 none (a ``dq_*`` id in pass 2 would collide with the dq score's namespace)."""
    check_detectors((*pass1, *pass2), tunables)
    for det in pass1:
        if not str(getattr(det, "id", "")).startswith(dq_score.DQ_PREFIX):
            raise ValueError(f"pass-1 detector {getattr(det, 'id', det)!r} must have a dq_* id")
    for det in pass2:
        if str(getattr(det, "id", "")).startswith(dq_score.DQ_PREFIX):
            raise ValueError(f"pass-2 detector {det.id!r} must not have a dq_* id")


def _suppressed(det: Detector, fw: FacilityWindow, tunables: Tunables) -> list[dict[str, str]]:
    """``{detector, pipe, reason}`` for each pipe where one of ``det.requires`` is untrusted (the pass-2 trust
    rule, ``_pass2.trusted``), plus the features a detector's optional ``skipped(fw, t)`` reports per pipe."""
    floor = dq_min(tunables)
    extra = det.skipped(fw, tunables) if hasattr(det, "skipped") else {}
    out = []
    for pid in sorted(fw.pipes):
        p = fw.pipes[pid]
        reasons = [
            untrusted_reason(m, dq_of(p, m), floor)
            for m in getattr(det, "requires", ())
            if m in p.aliases and not trusted(p, m, floor)
        ]
        reasons += extra.get(pid, [])
        if reasons:
            out.append({"detector": det.id, "pipe": pid, "reason": "; ".join(reasons)})
    return out


def _run_detector(
    det: Detector,
    fw: FacilityWindow,
    tunables: Tunables,
    check: Callable[[Finding, Sequence[str]], None] = dq_score.check_finding,
) -> list[Finding]:
    """Findings of one detector, dispatched by ``kind``; ``check`` validates each finding against its pipe's aliases
    (pass 1: a ``dq_*`` finding names one of them; pass 2: ``check_pass2_finding``)."""
    kind = getattr(det, "kind", None)
    if kind == "pipe":
        own: list[Finding] = []
        for pipe in fw.pipes.values():
            found = list(det.detect(_pipe_window(fw, pipe), tunables))
            for finding in found:
                if finding.pipe_id != pipe.pipe_id:
                    raise ValueError(f"{det.id}: finding for {finding.pipe_id} from the window of {pipe.pipe_id}")
                check(finding, list(pipe.aliases.values()))
            own += found
        return own
    if kind == "facility":
        found = list(det.detect(fw, tunables))
        for finding in found:
            pipe = fw.pipes.get(finding.pipe_id)
            if pipe is None:
                raise ValueError(f"{det.id}: finding for unknown pipe {finding.pipe_id!r}")
            check(finding, list(pipe.aliases.values()))
        return found
    raise ValueError(f"{getattr(det, 'id', det)!r}: unknown detector kind {kind!r}")


def _pipe_window(fw: FacilityWindow, pipe: PipeMeta) -> PipeWindow:
    w = fw.window
    return PipeWindow(
        pipe=pipe,
        window=Window(start=w.start, end=w.end, series={a: w.series[a] for a in pipe.aliases.values()}),
    )


_RUNTIME: Runtime | None = None


def runtime(env: Mapping[str, str] = os.environ, client: Any = None, dynamodb: Any = None) -> Runtime:
    """Build the per-cold-start runtime once (facility model, metadata, clients)."""
    global _RUNTIME
    if _RUNTIME is None:
        stage = env.get("STAGE")
        if not stage:
            raise ValueError("STAGE is not set")
        table = env.get("INCIDENTS_TABLE")
        if not table:
            raise ValueError("INCIDENTS_TABLE is not set")
        facility = load(env.get("FACILITY_PATH") or DEFAULT_PATH, stage=stage)
        check_passes(PASS1_DETECTORS, PASS2_DETECTORS, facility.tunables)
        incident_rules.check_tunables(facility.tunables)
        if dynamodb is None:
            import boto3
            from botocore.config import Config

            dynamodb = boto3.client("dynamodb", config=Config(retries={"mode": "standard", "max_attempts": 3}))
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "iotsitewise", config=Config(retries={"mode": "standard", "max_attempts": 3}, max_pool_connections=20)
            )
            # Writes: the derived writer is the only retry layer inside the 50 s budget (as sim/main.py).
            write_client = boto3.client("iotsitewise", config=Config(retries={"mode": "standard", "max_attempts": 1}))
        else:
            write_client = client
        _RUNTIME = Runtime(
            facility=facility,
            pipes=pipe_metas(facility),
            dq_targets=dq_aliases(facility),
            reader=make_reader(env.get("READER_STRATEGY") or DEFAULT_STRATEGY, client),
            writer=DerivedWriter(write_client, allowed=derived_targets(facility)),
            incidents=Incidents(dynamodb, table),
            alert_targets=alert_aliases(facility),
        )
    return _RUNTIME


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    return run(runtime(), parse_scheduled_time(event))
