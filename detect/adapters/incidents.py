"""The only writer of the ``incidents`` table (AD-6, AD-12; Incident item v1).

Per pipe and run: ``Query`` the pipe's items (``pk=<pipe_id>``: the ``OPEN`` slot and the ``C#<incident_id>``
history), let ``detect/core/incident_rules.decide`` pick the change, then write it:

- open: ``PutItem`` of ``sk=OPEN`` with ``attribute_not_exists(pk)``;
- update: ``PutItem`` of ``sk=OPEN`` with ``attribute_exists(pk) AND last_run < :run_id``;
- close: one ``TransactWriteItems``: delete ``OPEN`` (``last_run < :run_id``) and put ``C#<incident_id>``
  (``status=CLOSED``, ``closed_reason=quiet``, ``attribute_not_exists(pk)``);
- reopen: one ``TransactWriteItems``: delete ``C#<incident_id>`` (``closed_reason = quiet AND last_run < :run_id``)
  and put ``OPEN`` (``attribute_not_exists(pk)``) with the same ``incident_id`` and ``notified_at``.
- notified (AD-7 outbox, called by ``notifier.py`` after a publish): ``UpdateItem`` of ``sk=OPEN``,
  ``SET notified_at`` conditional on ``incident_id = :iid AND attribute_not_exists(notified_at)``. It does not
  touch ``last_run``, so it never blocks the next run's update.

Every item carries ``last_run`` (the run's ``run_id``) and ``config_hash``. A run whose ``run_id`` is at or
before the ``last_run`` of any of the pipe's items makes no write for that pipe (AD-12 replay guard). A
conditional failure (``ConditionalCheckFailed``: for example another run created the slot first) re-reads the
pipe and decides again once; a second conflict is logged and the pipe is left to the next run. Any other
error propagates: an adapter failure fails the invocation (Conventions).

Times are stored as ``YYYY-MM-DDTHH:MM:SS.sssZ`` and converted to tz-aware UTC ``datetime`` here (AD-9);
numbers go out as DynamoDB ``N`` (confidence unrounded).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from detect.core import incident_rules as rules
from detect.core.incident_rules import DetectorState, Incident
from detect.core.models import Finding, Tunables

OPEN_SK = "OPEN"
CLOSED_PREFIX = "C#"
OPS = ("opened", "updated", "closed", "reopened")
_OP_KEY = {"open": "opened", "update": "updated", "close": "closed", "reopen": "reopened"}

_ser = TypeSerializer()
_de = TypeDeserializer()


def iso(t: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.sssZ`` (AD-9 stored strings); a naive ``datetime`` is refused."""
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError(f"datetime must be tz-aware, got {t!r}")
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _num(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def to_item(inc: Incident, run_id: str, config_hash: str) -> dict[str, Any]:
    """Incident item v1 as plain Python values (``sk`` from the status)."""
    item: dict[str, Any] = {
        "pk": inc.pipe_id,
        "sk": OPEN_SK if inc.status == rules.OPEN else f"{CLOSED_PREFIX}{inc.incident_id}",
        "incident_id": inc.incident_id,
        "pipe_id": inc.pipe_id,
        "area": inc.area,
        "status": inc.status,
        "opened_at": iso(inc.opened_at),
        "last_seen": iso(inc.last_seen),
        "evidence_from": iso(inc.evidence_from),
        "evidence_to": iso(inc.evidence_to),
        "evidence_aliases": list(inc.evidence_aliases),
        "detectors": {
            k: {
                "confidence": _num(d.confidence),
                "explanation": d.explanation,
                "version": d.version,
                "first_seen": iso(d.first_seen),
                "last_seen": iso(d.last_seen),
                "quiet_runs": d.quiet_runs,
            }
            for k, d in inc.detectors.items()
        },
        "confidence": _num(inc.confidence),
        "last_run": run_id,
        "config_hash": config_hash,
    }
    if inc.notified_at is not None:
        item["notified_at"] = inc.notified_at
    if inc.closed_at is not None:
        item["closed_at"] = iso(inc.closed_at)
    if inc.closed_reason is not None:
        item["closed_reason"] = inc.closed_reason
    return item


def from_item(item: Mapping[str, Any]) -> Incident:
    """Plain Python item (deserialized) -> ``Incident``."""
    return Incident(
        incident_id=item["incident_id"],
        pipe_id=item["pipe_id"],
        area=item["area"],
        status=item["status"],
        opened_at=parse_iso(item["opened_at"]),
        last_seen=parse_iso(item["last_seen"]),
        evidence_from=parse_iso(item["evidence_from"]),
        evidence_to=parse_iso(item["evidence_to"]),
        evidence_aliases=tuple(item.get("evidence_aliases", ())),
        detectors=MappingProxyType(
            {
                k: DetectorState(
                    confidence=float(d["confidence"]),
                    explanation=d["explanation"],
                    version=d["version"],
                    first_seen=parse_iso(d["first_seen"]),
                    last_seen=parse_iso(d["last_seen"]),
                    quiet_runs=int(d["quiet_runs"]),
                )
                for k, d in sorted(item.get("detectors", {}).items())
            }
        ),
        confidence=float(item["confidence"]),
        notified_at=item.get("notified_at"),
        closed_at=parse_iso(item["closed_at"]) if item.get("closed_at") else None,
        closed_reason=item.get("closed_reason"),
    )


def serialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _ser.serialize(v) for k, v in item.items()}


def deserialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _de.deserialize(v) for k, v in item.items()}


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def is_conflict(exc: BaseException) -> bool:
    """A conditional failure: ``ConditionalCheckFailedException``, or a cancelled transaction with at least
    one ``ConditionalCheckFailed`` reason (other cancellations, e.g. throttling, are real errors)."""
    code = _error_code(exc)
    if code == "ConditionalCheckFailedException":
        return True
    if code == "TransactionCanceledException":
        response = getattr(exc, "response", {}) or {}
        return any(r.get("Code") == "ConditionalCheckFailed" for r in response.get("CancellationReasons") or [])
    return False


def default_new_id(now: datetime) -> str:
    from ulid import ULID

    return str(ULID.from_datetime(now))


@dataclass
class PipeState:
    open: Incident | None
    closed: list[Incident]
    max_last_run: str | None  # highest ``last_run`` over all of the pipe's items


@dataclass
class ApplyResult:
    open: dict[str, Incident | None]  # pipe_id -> OPEN incident after the run (or None)
    ops: dict[str, list[dict[str, str]]] = field(default_factory=lambda: {k: [] for k in OPS})
    replayed: set[str] = field(default_factory=set)  # pipes the replay guard left untouched
    conflicts: list[str] = field(default_factory=list)  # pipes left to the next run after a second conflict


class Incidents:
    def __init__(
        self,
        client: Any,
        table_name: str,
        new_id: Callable[[datetime], str] = default_new_id,
        log: Callable[[str], None] = print,
    ):
        if not table_name:
            raise ValueError("incidents table name is empty")
        self._client = client
        self._table = table_name
        self._new_id = new_id
        self._log = log

    # -- reads ------------------------------------------------------------------------------------

    def read(self, pipe_id: str) -> PipeState:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pipe_id}},
            "ConsistentRead": True,
        }
        while True:
            page = self._client.query(**kwargs)
            items += [deserialize(i) for i in page.get("Items", [])]
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        open_ = next((from_item(i) for i in items if i["sk"] == OPEN_SK), None)
        closed = [from_item(i) for i in items if str(i["sk"]).startswith(CLOSED_PREFIX)]
        runs = [str(i["last_run"]) for i in items if i.get("last_run")]
        return PipeState(open=open_, closed=closed, max_last_run=max(runs) if runs else None)

    # -- the outbox marker (AD-7) ------------------------------------------------------------------

    def mark_notified(self, pipe_id: str, incident_id: str, when: datetime) -> bool:
        """``SET notified_at`` on the pipe's OPEN slot if it still holds ``incident_id`` and has none yet.
        ``False`` on a conditional failure (already notified, or the slot changed); other errors propagate."""
        try:
            self._client.update_item(
                TableName=self._table,
                Key={"pk": {"S": pipe_id}, "sk": {"S": OPEN_SK}},
                UpdateExpression="SET #na = :when",
                ConditionExpression="#iid = :iid AND attribute_not_exists(#na)",
                ExpressionAttributeNames={"#na": "notified_at", "#iid": "incident_id"},
                ExpressionAttributeValues={":when": {"S": iso(when)}, ":iid": {"S": incident_id}},
            )
        except Exception as exc:
            if is_conflict(exc):
                return False
            raise
        return True

    # -- the run ----------------------------------------------------------------------------------

    def apply(
        self,
        *,
        run_id: str,
        now: datetime,
        pipes: Mapping[str, str],
        findings: Sequence[Finding],
        quiet_ok: Mapping[str, Collection[str]],
        tunables: Tunables,
        config_hash: str,
    ) -> ApplyResult:
        """Apply one run to every pipe in ``pipes`` (pipe_id -> area). ``quiet_ok[pipe]``: the detectors that
        ran OK and covered the pipe this run. Returns each pipe's OPEN incident after the run and the ops."""
        by_pipe: dict[str, list[Finding]] = {p: [] for p in pipes}
        for f in findings:
            if f.pipe_id not in by_pipe:
                raise ValueError(f"finding for unknown pipe {f.pipe_id!r}")
            by_pipe[f.pipe_id].append(f)
        result = ApplyResult(open={})
        for pipe_id in sorted(pipes):
            result.open[pipe_id] = self._apply_pipe(
                pipe_id, pipes[pipe_id], by_pipe[pipe_id], quiet_ok.get(pipe_id, ()), run_id, now, tunables, config_hash, result
            )
        return result

    def _apply_pipe(
        self,
        pipe_id: str,
        area: str,
        findings: Sequence[Finding],
        quiet_ok: Collection[str],
        run_id: str,
        now: datetime,
        tunables: Tunables,
        config_hash: str,
        result: ApplyResult,
    ) -> Incident | None:
        for attempt in (1, 2):
            state = self.read(pipe_id)
            if state.max_last_run is not None and state.max_last_run >= run_id:
                result.replayed.add(pipe_id)
                return state.open  # AD-12: this run (or a later one) already wrote this pipe
            decision = rules.decide(
                pipe_id=pipe_id,
                area=area,
                open_incident=state.open,
                last_closed=rules.latest_closed(state.closed),
                findings=findings,
                quiet_ok=quiet_ok,
                now=now,
                tunables=tunables,
                new_id=lambda: self._new_id(now),
            )
            if decision.op is None:
                return None
            try:
                self._write(decision, run_id, config_hash)
            except Exception as exc:
                if not is_conflict(exc):
                    raise
                code = _error_code(exc)
                if attempt == 1:
                    continue
                self._log(
                    json.dumps(
                        {"run_id": run_id, "pipe_id": pipe_id, "level": "WARNING", "error": f"incident write conflict ({code}); left to the next run"}
                    )
                )
                result.conflicts.append(pipe_id)
                return self.read(pipe_id).open
            inc = decision.incident
            assert inc is not None
            result.ops[_OP_KEY[decision.op]].append({"pipe": pipe_id, "incident_id": inc.incident_id})
            return inc if inc.status == rules.OPEN else None
        raise AssertionError("unreachable")

    def _write(self, decision: rules.Decision, run_id: str, config_hash: str) -> None:
        inc = decision.incident
        assert inc is not None
        item = serialize(to_item(inc, run_id, config_hash))
        guard = {"ExpressionAttributeNames": {"#lr": "last_run"}, "ExpressionAttributeValues": {":run_id": {"S": run_id}}}
        if decision.op == "open":
            self._client.put_item(TableName=self._table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        elif decision.op == "update":
            self._client.put_item(
                TableName=self._table, Item=item, ConditionExpression="attribute_exists(pk) AND #lr < :run_id", **guard
            )
        elif decision.op == "close":
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self._table,
                            "Key": {"pk": {"S": inc.pipe_id}, "sk": {"S": OPEN_SK}},
                            "ConditionExpression": "#lr < :run_id",
                            **guard,
                        }
                    },
                    {"Put": {"TableName": self._table, "Item": item, "ConditionExpression": "attribute_not_exists(pk)"}},
                ]
            )
        elif decision.op == "reopen":
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self._table,
                            "Key": {"pk": {"S": inc.pipe_id}, "sk": {"S": f"{CLOSED_PREFIX}{inc.incident_id}"}},
                            "ConditionExpression": "#cr = :quiet AND #lr < :run_id",
                            "ExpressionAttributeNames": {"#cr": "closed_reason", "#lr": "last_run"},
                            "ExpressionAttributeValues": {":quiet": {"S": rules.QUIET}, ":run_id": {"S": run_id}},
                        }
                    },
                    {"Put": {"TableName": self._table, "Item": item, "ConditionExpression": "attribute_not_exists(pk)"}},
                ]
            )
        else:  # pragma: no cover - decide() returns only these ops
            raise ValueError(f"unknown op {decision.op!r}")
