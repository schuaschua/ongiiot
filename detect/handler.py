"""Detection Lambda entry point: ``detect.handler.handler`` (AD-5, AD-9, AD-12, AD-13, AD-18).

One run per EventBridge Scheduler invocation, event ``{"scheduled_time": "<ISO-8601 UTC>"}``:

1. ``run_id = scheduled_time``; the window is aligned from it (AD-9), never from ``now()``.
2. Read the ``FacilityWindow`` through the reader adapter (``READER_STRATEGY``).
3. Pass 1: the ``dq_*`` detectors (``dq_flatline``, ``dq_stuck``, ``dq_gap``: pipe; ``dq_drift``: facility),
   dispatched by ``kind`` (``pipe``: once per pipe; ``facility``: once per run), then ``dq_score`` with
   the no-sample gap rule (one ``dq_gap`` finding per empty alias).
4. Write ``<m>_dq`` and ``<m>_dq_fail`` for every sensor at ``window_end``.
5. Log one JSON summary line.

Environment: ``STAGE`` (required), ``READER_STRATEGY`` (``batch_get`` or ``execute_query``; default
``reader.DEFAULT_STRATEGY``),
``FACILITY_PATH`` (default: the bundled ``facility.yaml``). The facility model, pipe metadata and
SiteWise clients are built once per cold start; each registered detector's tunables are checked then,
so a missing key fails the cold start.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from detect.adapters.derived_writer import DerivedWriter
from detect.adapters.reader import DEFAULT_STRATEGY, Reader, make_reader
from detect.core import dq_score
from detect.core.detectors import PASS1
from detect.core.models import Contract, Detector, FacilityWindow, Finding, PipeMeta, PipeWindow, Tunables, Window
from detect.core.window import window_bounds
from facility.model import DEFAULT_PATH, Facility, load

PASS1_DETECTORS: Sequence[Detector] = PASS1
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


@dataclass
class Runtime:
    facility: Facility
    pipes: Mapping[str, PipeMeta]
    dq_targets: Mapping[str, tuple[str, str]]
    reader: Reader
    writer: DerivedWriter


def run(
    rt: Runtime,
    scheduled_time: datetime,
    detectors: Sequence[Detector] = PASS1_DETECTORS,
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
    for det in detectors:
        # A detector's findings count only if it completed on every pipe and all are well formed.
        try:
            own = _run_detector(det, fw, tunables)
        except Exception as exc:  # noqa: BLE001 - AD-5: one detector's failure never stops the run
            log(json.dumps({"stage": f.stage, "run_id": run_id, "detector_id": det.id, "error": repr(exc)}))
            continue
        findings += own
        ran_ok.append(det.id)
    scores = dq_score.score_facility(fw, findings)
    pipes = {
        pid: replace(p, dq=MappingProxyType({m: scores[a][0] for m, a in p.aliases.items()}))
        for pid, p in fw.pipes.items()
    }
    fw = FacilityWindow(pipes=pipes, window=fw.window)

    values: dict[str, float | str] = {}
    for raw, (dq, failing) in scores.items():
        dq_alias, fail_alias = rt.dq_targets[raw]
        values[dq_alias] = dq
        values[fail_alias] = failing
    t1 = clock()
    written = rt.writer.write(values, end)
    write_s = clock() - t1

    summary = {
        "stage": f.stage,
        "run_id": run_id,
        "config_hash": f.config_hash(),
        "window_start": iso(start),
        "window_end": iso(end),
        "reader_strategy": rt.reader.strategy,
        "read_duration_s": round(read_s, 3),
        "write_duration_s": round(write_s, 3),
        "samples": sum(len(s) for s in fw.window.series.values()),
        "aliases_no_data": sorted(a for a, s in fw.window.series.items() if not s),
        "values_written": written,
        "detectors": {d.id: d.version for d in detectors},
        "ran_ok": ran_ok,
        "run_s": round(clock() - t_run, 3),
    }
    log(json.dumps(summary, separators=(",", ":")))
    return summary


def _run_detector(det: Detector, fw: FacilityWindow, tunables: Tunables) -> list[Finding]:
    """Findings of one detector, dispatched by ``kind``; each ``dq_*`` finding must name one of its pipe's aliases."""
    kind = getattr(det, "kind", None)
    if kind == "pipe":
        own: list[Finding] = []
        for pipe in fw.pipes.values():
            found = list(det.detect(_pipe_window(fw, pipe), tunables))
            for finding in found:
                if finding.pipe_id != pipe.pipe_id:
                    raise ValueError(f"{det.id}: finding for {finding.pipe_id} from the window of {pipe.pipe_id}")
                dq_score.check_finding(finding, pipe.aliases.values())
            own += found
        return own
    if kind == "facility":
        found = list(det.detect(fw, tunables))
        for finding in found:
            pipe = fw.pipes.get(finding.pipe_id)
            if pipe is None:
                raise ValueError(f"{det.id}: finding for unknown pipe {finding.pipe_id!r}")
            dq_score.check_finding(finding, pipe.aliases.values())
        return found
    raise ValueError(f"{getattr(det, 'id', det)!r}: unknown detector kind {kind!r}")


def _pipe_window(fw: FacilityWindow, pipe: PipeMeta) -> PipeWindow:
    w = fw.window
    return PipeWindow(
        pipe=pipe,
        window=Window(start=w.start, end=w.end, series={a: w.series[a] for a in pipe.aliases.values()}),
    )


_RUNTIME: Runtime | None = None


def runtime(env: Mapping[str, str] = os.environ, client: Any = None) -> Runtime:
    """Build the per-cold-start runtime once (facility model, metadata, clients)."""
    global _RUNTIME
    if _RUNTIME is None:
        stage = env.get("STAGE")
        if not stage:
            raise ValueError("STAGE is not set")
        facility = load(env.get("FACILITY_PATH") or DEFAULT_PATH, stage=stage)
        check_detectors(PASS1_DETECTORS, facility.tunables)
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
        targets = dq_aliases(facility)
        _RUNTIME = Runtime(
            facility=facility,
            pipes=pipe_metas(facility),
            dq_targets=targets,
            reader=make_reader(env.get("READER_STRATEGY") or DEFAULT_STRATEGY, client),
            writer=DerivedWriter(write_client, allowed=[a for pair in targets.values() for a in pair]),
        )
    return _RUNTIME


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    return run(runtime(), parse_scheduled_time(event))
