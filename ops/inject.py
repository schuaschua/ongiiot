"""Create, cancel and list simulator injections: ``python -m ops.inject`` (AD-15).

Writes only ``sim_control`` (sim_control item v1). Items are immutable: ``cancel`` writes a
new item whose ``supersedes`` names the injection, ending it now. ``list`` is read-only: it
scans ``sim_control`` and shows active and recent injections with their effective windows
(the planned end shortened by any cancel).

Environment: ``STAGE`` (default ``poc``), ``SIM_CONTROL_TABLE`` (required unless
``--table`` is given) and ``FACILITY_PATH`` (default: the bundled facility.yaml).

Examples:
    python -m ops.inject leak --pipe P-007 --minutes 20
    python -m ops.inject dq_drift --pipe P-003 --measurement temperature --minutes 30 --per-min 0.5
    python -m ops.inject cancel 01J...
    python -m ops.inject list [--recent-min 60] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from ulid import ULID

from facility.control import (
    INJECTION_TYPES,
    ControlError,
    cancel_of,
    format_ts,
    from_item,
    new_injection,
    to_item,
    windows,
)
from facility.model import DEFAULT_PATH, FacilityError, load

MAX_MINUTES = 7 * 24 * 60


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inject", description="Create or cancel simulator injections (sim_control).")
    p.add_argument("--stage", help="stage (default: $STAGE or poc)")
    p.add_argument("--table", help="sim_control table name (default: $SIM_CONTROL_TABLE)")
    sub = p.add_subparsers(dest="command", required=True, metavar="{" + ",".join((*INJECTION_TYPES, "cancel", "list")) + "}")
    for t in INJECTION_TYPES:
        s = sub.add_parser(t, help=f"inject {t}")
        s.add_argument("--pipe", required=True, help="pipe id, e.g. P-007")
        s.add_argument("--measurement", help="measurement (required for dq_* types)")
        s.add_argument("--minutes", required=True, type=float, help="duration in minutes")
        if t == "leak":
            s.add_argument("--rate-kg-s", type=float, help="leak rate (default: 10%% of the pipe's mean flow)")
        if t == "dq_drift":
            s.add_argument("--per-min", type=float, help="drift per minute in the measurement's unit")
    c = sub.add_parser("cancel", help="cancel an injection now")
    c.add_argument("injection_id")
    ls = sub.add_parser("list", help="list active and recent injections (read-only)")
    ls.add_argument("--recent-min", type=float, default=60.0, help="also show injections ended this many minutes ago (default 60)")
    ls.add_argument("--json", action="store_true", help="print one JSON object per line")
    return p


def _error(msg: str) -> int:
    print(f"inject: error: {msg}", file=sys.stderr)
    return 2


def _table(name: str) -> Any:
    import boto3

    return boto3.resource("dynamodb").Table(name)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    table: Any = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    new_id: Callable[[], str] = lambda: str(ULID()),
) -> int:
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)
    stage = args.stage or env.get("STAGE") or "poc"
    try:
        facility = load(env.get("FACILITY_PATH") or DEFAULT_PATH, stage=stage)
    except FacilityError as exc:
        return _error(str(exc))

    n = now()
    t0 = n.replace(microsecond=(n.microsecond // 1000) * 1000)
    if table is None and not (args.table or env.get("SIM_CONTROL_TABLE")):
        return _error("SIM_CONTROL_TABLE is not set and --table was not given")
    try:
        if table is None:
            table = _table(args.table or env["SIM_CONTROL_TABLE"])
        return _run(args, facility, table, t0, new_id)
    except (ClientError, BotoCoreError) as exc:
        return _error(f"AWS call failed: {exc}")


def _scan(table: Any) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    kwargs: dict[str, Any] = {"ConsistentRead": True}
    while True:
        page = table.scan(**kwargs)
        out.extend(page.get("Items", []))
        key = page.get("LastEvaluatedKey")
        if not key:
            return out
        kwargs["ExclusiveStartKey"] = key


def _list(args: argparse.Namespace, facility: Any, table: Any, now: datetime) -> int:
    if not (math.isfinite(args.recent_min) and args.recent_min >= 0):
        return _error("--recent-min must be >= 0")
    parsed = []
    for raw in _scan(table):
        try:
            parsed.append(from_item(raw, facility))
        except ControlError as exc:
            pk = raw.get("pk") if isinstance(raw, Mapping) else None
            print(f"inject: warning: skipping invalid item {pk!r}: {exc}", file=sys.stderr)
    recent = timedelta(minutes=args.recent_min)
    rows = []
    for inj, planned_end in windows(parsed):
        if inj.start_at > now:
            state = "scheduled"
        elif now < inj.end_at:
            state = "active"
        elif now - inj.end_at <= recent:
            state = "recent"
        else:
            continue
        rows.append(
            {
                "id": inj.injection_id,
                "type": inj.type,
                "pipe": inj.pipe_id,
                "measurement": inj.measurement,
                "start": format_ts(inj.start_at),
                "planned_end": format_ts(planned_end),
                "effective_end": format_ts(inj.end_at),
                "state": state,
            }
        )
    if args.json:
        for r in rows:
            print(json.dumps(r))
        return 0
    if not rows:
        print("no injections")
        return 0
    cols = ["id", "type", "pipe", "measurement", "start", "effective_end", "planned_end", "state"]
    cells = [[str(r[c] if r[c] is not None else "-") for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    for line in [cols, *cells]:
        print("  ".join(v.ljust(w) for v, w in zip(line, widths, strict=True)).rstrip())
    return 0


def _run(args: argparse.Namespace, facility: Any, table: Any, t0: datetime, new_id: Callable[[], str]) -> int:
    if args.command == "list":
        return _list(args, facility, table, t0)
    try:
        if args.command == "cancel":
            got = table.get_item(Key={"pk": args.injection_id}, ConsistentRead=True).get("Item")
            if got is None:
                return _error(f"no injection {args.injection_id!r}")
            item = cancel_of(from_item(got, facility), new_id(), t0)
        else:
            if not (math.isfinite(args.minutes) and 0 < args.minutes <= MAX_MINUTES):
                return _error(f"--minutes must be > 0 and at most {MAX_MINUTES} (7 days)")
            params: dict[str, float] = {}
            if getattr(args, "rate_kg_s", None) is not None:
                params["rate_kg_s"] = args.rate_kg_s
            if getattr(args, "per_min", None) is not None:
                params["per_min"] = args.per_min
            item = new_injection(
                injection_id=new_id(),
                type_=args.command,
                pipe_id=args.pipe,
                start_at=t0,
                end_at=t0 + timedelta(minutes=args.minutes),
                measurement=args.measurement,
                params=params,
                facility=facility,
            )
    except ControlError as exc:
        return _error(str(exc))

    record = to_item(item)
    table.put_item(Item=record, ConditionExpression="attribute_not_exists(pk)")
    print(json.dumps(record, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
