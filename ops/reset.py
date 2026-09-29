"""Reset the demo between rehearsals: ``python -m ops.reset`` (AD-15, Story 4.2).

1. Invokes the stage's detection Lambda ``detection-<stage>`` synchronously with ``{"action": "reset"}``, so
   ``detect/adapters/incidents.py`` closes every pipe's OPEN incident with ``closed_reason=reset``.
2. Only if that succeeded: deletes every ``sim_control`` item (paginated scan, batched deletes). The simulator
   re-scans every <= 5 s and drops vanished items, which stops their effect.

Raw SiteWise data and ``injection_log`` are never touched. If the invoke fails or returns a Lambda error, the
script exits 1 and deletes nothing.

A detection run within about 2 minutes of the reset may still see the fault in its trailing window and open a
new incident: wait about 2 minutes and run the reset again if one appears.

Environment: ``STAGE`` (default ``poc``) and ``SIM_CONTROL_TABLE`` (required unless ``--table`` is given).

Examples:
    SIM_CONTROL_TABLE=sim-control-poc-dj STAGE=poc-dj python -m ops.reset
    python -m ops.reset --stage poc-dj --table sim-control-poc-dj --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from ops.inject import _scan

STRAY_HINT = (
    "note: a detection run within about 2 minutes may still see the fault in its window and open a new "
    "incident; if one appears, wait about 2 minutes and run the reset again."
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reset", description="Close all OPEN incidents, then delete all sim_control injections.")
    p.add_argument("--stage", help="stage (default: $STAGE or poc)")
    p.add_argument("--table", help="sim_control table name (default: $SIM_CONTROL_TABLE)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return p


def _error(msg: str) -> int:
    print(f"reset: error: {msg}", file=sys.stderr)
    return 1


def _lambda_client() -> Any:
    import boto3
    from botocore.config import Config

    # The function times out at 50 s and runs one at a time (AD-12): allow for it and retry throttles.
    return boto3.client("lambda", config=Config(read_timeout=70, retries={"mode": "standard", "max_attempts": 5}))


def _table(name: str) -> Any:
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _invoke(client: Any, function: str) -> dict[str, Any]:
    """Synchronous reset invoke; raises ``RuntimeError`` on a Lambda error or an unexpected payload."""
    resp = client.invoke(FunctionName=function, InvocationType="RequestResponse", Payload=json.dumps({"action": "reset"}).encode())
    body = resp["Payload"].read() if resp.get("Payload") is not None else b""
    if resp.get("FunctionError"):
        raise RuntimeError(f"{function} returned a {resp['FunctionError']} error: {body.decode(errors='replace')}")
    try:
        summary = json.loads(body)
    except ValueError as exc:
        raise RuntimeError(f"{function} returned an unreadable payload: {body[:200]!r}") from exc
    if not isinstance(summary, dict) or summary.get("action") != "reset":
        raise RuntimeError(f"{function} did not run a reset: {summary!r}")
    return summary


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    table: Any = None,
    lambda_client: Any = None,
    ask: Callable[[str], str] = input,
) -> int:
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)
    stage = args.stage or env.get("STAGE") or "poc"
    function = f"detection-{stage}"
    table_name = args.table or env.get("SIM_CONTROL_TABLE")
    if table is None and not table_name:
        return _error("SIM_CONTROL_TABLE is not set and --table was not given")
    if not args.yes:
        try:
            answer = ask(f"Close all OPEN incidents via {function} and delete every injection in {table_name}? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("reset: aborted, nothing changed")
            return 1

    try:
        summary = _invoke(lambda_client if lambda_client is not None else _lambda_client(), function)
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        return _error(f"invoke of {function} failed, nothing deleted: {exc}")
    closed = summary.get("closed") or []
    for c in closed:
        print(f"closed incident {c.get('incident_id')} on {c.get('pipe')}")
    for pipe in summary.get("conflicts") or []:
        print(f"reset: warning: {pipe}: the close lost a race with a detection run; run the reset again", file=sys.stderr)

    try:
        if table is None:
            table = _table(table_name)
        deleted = []
        with table.batch_writer() as batch:
            for item in _scan(table):
                batch.delete_item(Key={"pk": item["pk"]})
                deleted.append(item)
    except (ClientError, BotoCoreError) as exc:
        return _error(f"deleting injections from {table_name} failed (incidents are already closed; run the reset again): {exc}")
    for item in deleted:
        print(f"deleted injection {item['pk']} ({item.get('type')} on {item.get('pipe_id')})")
    print(f"closed {len(closed)} incidents, deleted {len(deleted)} injections")
    print(STRAY_HINT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
