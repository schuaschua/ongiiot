"""Smoke-test the full chain before the demo: ``python -m ops.smoke`` (AD-15, Story 4.3).

Proves in one step that injection -> incident -> email -> evidence link works on a stage:

1. precheck: the test pipe has no OPEN incident (else "run ops.reset first"; nothing is injected), and the
   stage's Grafana outputs (``SharedGrafanaStack``: ``WorkspaceId``, ``GrafanaUrl``; ``GrafanaStack-<stage>``:
   ``PipeDetailUid``) can be read;
2. inject: a default leak on the pipe (a ``sim_control`` item, as ``ops/inject.py`` writes it), lasting the
   budget plus 2 minutes;
3. incident: poll ``incidents-<stage>`` every 10 s until the pipe's OPEN slot holds an incident opened at or
   after the injection start;
4. email: wait until that incident has ``notified_at``;
5. link: read the link that was emailed for that incident from the detection run's summary line (``notified``
   entries carry ``link``) in ``/aws/lambda/detection-<stage>``, with ``FilterLogEvents``;
6. resolve: mint a short-lived token on the stage's Grafana service account, ``GET /api/dashboards/uid/<uid>``
   for the uid in the link, and check it is the stage's ``pipe-detail`` with variables ``pipe_id``, ``area``
   and ``evidence``; the token is deleted again.

One budget (``--budget-s``, default 300 s) counts from the injection; every wait checks the time left. The
injection is always cancelled at the end (pass, fail or Ctrl-C) with a cancel item; the incident stays OPEN,
so run ``ops.reset`` before the demo. The last line is ``PASS ...`` or ``FAIL: <step>: <reason>`` (exit 1).

Environment: ``STAGE`` (default ``poc``), ``SIM_CONTROL_TABLE`` (required unless ``--table`` is given) and
``FACILITY_PATH`` (default: the bundled facility.yaml).

Examples:
    SIM_CONTROL_TABLE=sim-control-poc-dj uv run python -m ops.smoke --stage poc-dj
    uv run python -m ops.smoke --stage poc-dj --table sim-control-poc-dj --pipe P-007 --budget-s 300
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from ulid import ULID

from facility.control import ControlError, cancel_of, format_ts, new_injection, to_item
from facility.model import DEFAULT_PATH, FacilityError, load
from infra.lambdas.grafana_provision.handler import (
    GrafanaApi,
    GrafanaError,
    ensure_service_account,
    short_lived_token,
)

POLL_S = 10.0  # detection runs once a minute
EXTRA_INJECTION_S = 120.0  # the leak outlasts the budget by 2 minutes
REQUIRED_VARS = ("pipe_id", "area", "evidence")
SHARED_GRAFANA_STACK = "SharedGrafanaStack"


class Fail(Exception):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smoke", description="Inject a leak and check incident, email and evidence link.")
    p.add_argument("--stage", help="stage (default: $STAGE or poc)")
    p.add_argument("--table", help="sim_control table name (default: $SIM_CONTROL_TABLE)")
    p.add_argument("--pipe", default="P-007", help="test pipe (default P-007, the calibrated leak pipe)")
    p.add_argument("--budget-s", type=float, default=300.0, help="time from injection to a resolved link (default 300)")
    return p


def _resource_table(name: str) -> Any:
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _client(service: str) -> Any:
    import boto3

    return boto3.client(service)


def _stack_outputs(stack: str) -> dict[str, str]:
    resp = _client("cloudformation").describe_stacks(StackName=stack)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def link_in(message: str, incident_id: str) -> str | None:
    """The emailed link for ``incident_id`` in one log line (a run summary), or None."""
    start = message.find("{")
    if start < 0:
        return None
    try:
        doc = json.loads(message[start:])
    except ValueError:
        return None
    if isinstance(doc, dict) and "notified" not in doc and "message" in doc:  # Lambda JSON log format
        doc = doc["message"]
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError:
                return None
    if not isinstance(doc, dict):
        return None
    for entry in doc.get("notified") or []:
        if isinstance(entry, dict) and entry.get("incident_id") == incident_id and entry.get("link"):
            return str(entry["link"])
    return None


def find_link(logs: Any, group: str, start_ms: int, incident_id: str) -> str | None:
    kwargs: dict[str, Any] = {"logGroupName": group, "startTime": start_ms, "filterPattern": f'"{incident_id}"'}
    while True:
        page = logs.filter_log_events(**kwargs)
        for event in page.get("events", []):
            found = link_in(event.get("message", ""), incident_id)
            if found:
                return found
        token = page.get("nextToken")
        if not token:
            return None
        kwargs["nextToken"] = token


def link_uid(link: str) -> str | None:
    parts = urllib.parse.urlparse(link).path.strip("/").split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "d" else None


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    table: Any = None,
    incidents: Any = None,
    logs: Any = None,
    grafana: Any = None,
    api_factory: Callable[[str, str], Any] = GrafanaApi,
    outputs: Callable[[str], Mapping[str, str]] = _stack_outputs,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    new_id: Callable[[], str] = lambda: str(ULID()),
) -> int:
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)
    stage = args.stage or env.get("STAGE") or "poc"
    table_name = args.table or env.get("SIM_CONTROL_TABLE")
    if table is None and not table_name:
        print("smoke: error: SIM_CONTROL_TABLE is not set and --table was not given", file=sys.stderr)
        return 2
    if not (math.isfinite(args.budget_s) and args.budget_s > 0):
        print("smoke: error: --budget-s must be > 0", file=sys.stderr)
        return 2
    try:
        facility = load(env.get("FACILITY_PATH") or DEFAULT_PATH, stage=stage)
    except FacilityError as exc:
        print(f"smoke: error: {exc}", file=sys.stderr)
        return 2

    run = _Smoke(args, stage, table_name, facility, clock, sleep, now, new_id)
    run.table, run.incidents, run.logs, run.grafana = table, incidents, logs, grafana
    run.api_factory, run.outputs = api_factory, outputs
    return run.execute()


class _Smoke:
    def __init__(self, args, stage, table_name, facility, clock, sleep, now, new_id) -> None:
        self.args, self.stage, self.table_name, self.facility = args, stage, table_name, facility
        self.pipe = args.pipe
        self.budget = args.budget_s
        self.clock, self.sleep, self.now, self.new_id = clock, sleep, now, new_id
        self.t_start = clock()
        self.deadline = math.inf
        self.t_inject = self.t_start
        self.step = "precheck"
        self.injection = None
        self.table = self.incidents = self.logs = self.grafana = None
        self.api_factory: Callable[[str, str], Any] = GrafanaApi
        self.outputs: Callable[[str], Mapping[str, str]] = _stack_outputs

    def say(self, msg: str) -> None:
        print(f"[{self.clock() - self.t_start:6.1f} s] {self.step}: {msg}", flush=True)

    def wait(self, reason: str) -> None:
        """Sleep one poll interval, or fail the current step when the budget is spent."""
        left = self.deadline - self.clock()
        if left <= 0:
            raise Fail(self.step, f"{reason} after {self.clock() - self.t_inject:.0f} s")
        self.sleep(min(POLL_S, left))

    def open_slot(self) -> Mapping[str, Any] | None:
        return self.incidents.get_item(Key={"pk": self.pipe, "sk": "OPEN"}, ConsistentRead=True).get("Item")

    def execute(self) -> int:
        result: Fail | None = None
        try:
            self._steps()
        except Fail as exc:
            result = exc
        except (ClientError, BotoCoreError) as exc:
            result = Fail(self.step, f"AWS call failed: {exc}")
        except KeyboardInterrupt:
            result = Fail(self.step, "interrupted")
        except Exception as exc:  # noqa: BLE001 - always end with the FAIL line; cleanup runs in finally
            result = Fail(self.step, f"unexpected error: {exc!r}")
        finally:
            cancel_error = self._cancel()
        if result is None and cancel_error is not None:
            result = Fail("cancel", cancel_error)
        if result is not None:
            print(f"FAIL: {result.step}: {result.reason}", flush=True)
            return 1
        print(
            f"PASS: incident {self.incident_id} on {self.pipe} was emailed and its link opens {self.expected_uid}; "
            f"it stays OPEN, so run ops.reset before the demo "
            f"(uv run python -m ops.reset --stage {self.stage} --table {self.table_name} --yes)",
            flush=True,
        )
        return 0

    def _steps(self) -> None:
        pipes = {p.id for p in self.facility.pipes}
        if self.pipe not in pipes:
            raise Fail("precheck", f"unknown pipe {self.pipe!r}")
        if self.incidents is None:
            self.incidents = _resource_table(f"incidents-{self.stage}")
        slot = self.open_slot()
        if slot is not None:
            raise Fail(
                "precheck",
                f"{self.pipe} has OPEN incident {slot.get('incident_id')} (opened {slot.get('opened_at')}); run ops.reset first",
            )
        if self.table_name != f"sim-control-{self.stage}":
            raise Fail("precheck", f"sim_control table {self.table_name!r} is not sim-control-{self.stage}")
        shared = self.outputs(SHARED_GRAFANA_STACK)
        stage_out = self.outputs(f"GrafanaStack-{self.stage}")
        try:
            self.workspace_id, self.grafana_url = shared["WorkspaceId"], shared["GrafanaUrl"]
            self.expected_uid = stage_out["PipeDetailUid"]
        except KeyError as exc:
            raise Fail("precheck", f"stack output {exc.args[0]} not found; is the stage deployed?") from None
        self.say(f"{self.pipe} has no OPEN incident; dashboard {self.expected_uid} in workspace {self.workspace_id}")

        self.step = "inject"
        if self.table is None:
            self.table = _resource_table(self.table_name)
        n = self.now()
        t0 = n.replace(microsecond=(n.microsecond // 1000) * 1000)
        try:
            inj = new_injection(
                injection_id=self.new_id(),
                type_="leak",
                pipe_id=self.pipe,
                start_at=t0,
                end_at=t0 + timedelta(seconds=self.budget + EXTRA_INJECTION_S),
                facility=self.facility,
            )
        except ControlError as exc:
            raise Fail("inject", str(exc)) from None
        self.t_inject = self.clock()
        self.deadline = self.t_inject + self.budget
        self.injection = inj  # before the put: an ambiguous write is still cancelled
        self.table.put_item(Item=to_item(inj), ConditionExpression="attribute_not_exists(pk)")
        start_ts = format_ts(t0)
        self.say(f"leak {inj.injection_id} on {self.pipe} from {start_ts} until {format_ts(inj.end_at)}")

        self.step = "incident"
        while True:
            slot = self.open_slot()
            if slot and slot.get("incident_id") and str(slot.get("opened_at", "")) >= start_ts:
                break
            self.wait("none")
        self.incident_id = str(slot["incident_id"])
        self.say(f"OPEN incident {self.incident_id} opened {slot['opened_at']}")

        self.step = "email"
        while True:
            slot = self.open_slot()
            if not slot or slot.get("incident_id") != self.incident_id:
                raise Fail("email", f"incident {self.incident_id} is no longer OPEN on {self.pipe}")
            if slot.get("notified_at"):
                break
            self.wait(f"incident {self.incident_id} has no notified_at")
        self.say(f"notified_at {slot['notified_at']}")

        self.step = "link"
        if self.logs is None:
            self.logs = _client("logs")
        group = f"/aws/lambda/detection-{self.stage}"
        start_ms = int(t0.timestamp() * 1000)
        while True:
            link = find_link(self.logs, group, start_ms, self.incident_id)
            if link:
                break
            self.wait(f"no emailed link for {self.incident_id} in {group}")
        self.say(f"emailed link {link}")

        uid = link_uid(link)
        if uid != self.expected_uid:
            raise Fail("link", f"link uid {uid!r} is not the stage's pipe-detail uid {self.expected_uid!r}")
        if self.grafana is None:
            self.grafana = _client("grafana")
        sa_id = ensure_service_account(self.grafana, self.workspace_id, self.stage)
        path = f"/api/dashboards/uid/{uid}"
        with short_lived_token(self.grafana, self.workspace_id, sa_id) as token:
            try:
                got = self.api_factory(self.grafana_url, token).request("GET", path)
            except GrafanaError as exc:
                raise Fail("link", f"GET {path} returned {exc.status}") from None
        dash = (got or {}).get("dashboard") or {}
        if dash.get("uid") != self.expected_uid:
            raise Fail("link", f"GET {path} returned dashboard uid {dash.get('uid')!r}")
        names = {v.get("name") for v in (dash.get("templating") or {}).get("list") or [] if isinstance(v, dict)}
        missing = [v for v in REQUIRED_VARS if v not in names]
        if missing:
            raise Fail("link", f"dashboard {uid} lacks variables {', '.join(missing)}")
        if self.clock() > self.deadline:
            raise Fail(self.step, f"budget spent after {self.clock() - self.t_inject:.0f} s")
        self.say(f"dashboard {uid} resolves with variables {', '.join(REQUIRED_VARS)}")

    def _cancel(self) -> str | None:
        """Cancel the injection if one was written; returns an error text if that failed."""
        if self.injection is None:
            return None
        inj, self.injection = self.injection, None
        try:
            item = cancel_of(inj, self.new_id(), self.now())
            self.table.put_item(Item=to_item(item), ConditionExpression="attribute_not_exists(pk)")
        except (ClientError, BotoCoreError, ControlError) as exc:
            msg = f"cancelling {inj.injection_id} failed ({exc}); run: python -m ops.inject cancel {inj.injection_id}"
            print(f"smoke: warning: {msg}", file=sys.stderr, flush=True)
            return msg
        self.step = "cancel"
        self.say(f"injection {inj.injection_id} cancelled ({item.injection_id})")
        return None


if __name__ == "__main__":
    sys.exit(main())
