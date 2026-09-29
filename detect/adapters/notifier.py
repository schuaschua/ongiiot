"""Operator email through the ``notified_at`` outbox, one email per incident (AD-7; Email v1).

Runs in-process at the end of each detection run, after the incident and alert writes (no DynamoDB Stream).
For every OPEN incident without ``notified_at``, in pipe order:

1. publish one Email v1 message to the stage's operator SNS topic (``operators-<stage>``);
2. then ``incidents.mark_notified`` sets ``notified_at``, conditional on the OPEN slot still holding the same
   ``incident_id`` and having no ``notified_at`` yet.

A failed publish is logged, recorded in ``failures`` and retried by the next run (``notified_at`` stays unset);
it never fails the run. A conditional failure on the mark (another writer set it, or the slot changed) is
logged and recorded; the next run reads ``notified_at`` (or a different incident) and does not publish again.
Any other mark error is logged and recorded too: the email went out, so the next run may send it once more
(at-least-once, the outbox's trade-off).

Detectors joining an already-notified incident, and a reopened incident (``notified_at`` kept by
``incident_rules``), send nothing: their ``notified_at`` is set.

Email v1: subject ``[POC] Pipe <pipe_id> incident <incident_id>``; body lines: the POC banner; pipe ID and
area; incident confidence (2 dp); one line per detector (id, confidence 2 dp, explanation); the contributing
aliases; the Deep link v1 (``deeplink.build_link``). Each ``notified`` entry carries the ``link`` that was
emailed, so the run's summary line records it (read back by ``ops/smoke.py``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from detect.adapters.deeplink import build_link
from detect.adapters.incidents import Incidents
from detect.core import incident_rules as rules
from detect.core.incident_rules import Incident

BANNER = "POC - simulated data - evidence only, not an operational alert or official record"
SUBJECT_MAX = 100  # SNS email subject: ASCII, no line breaks, at most 100 characters
DEADLINE_MS = 10_000  # stop publishing when the Lambda has less than this left


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def subject(inc: Incident) -> str:
    raw = _one_line(f"[POC] Pipe {inc.pipe_id} incident {inc.incident_id}")
    return raw.encode("ascii", "replace").decode("ascii")[:SUBJECT_MAX]


def body(inc: Incident, link: str) -> str:
    lines = [
        BANNER,
        f"Pipe: {inc.pipe_id} (area {inc.area})",
        f"Incident confidence: {inc.confidence:.2f}",
        "Detectors:",
        *(f"- {det_id}: confidence {d.confidence:.2f} - {_one_line(d.explanation)}" for det_id, d in sorted(inc.detectors.items())),
        "Contributing sensors: " + ", ".join(inc.evidence_aliases),
        f"Evidence in Grafana: {link}",
    ]
    return "\n".join(lines)


@dataclass
class NotifyResult:
    notified: list[dict[str, str]] = field(default_factory=list)  # {pipe, incident_id, message_id, link}
    failures: list[dict[str, str]] = field(default_factory=list)  # {pipe, incident_id, step, error}


def _error(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return f"{code}: {exc}" if code else repr(exc)


class Notifier:
    def __init__(
        self,
        sns: Any,
        topic_arn: str,
        incidents: Incidents,
        grafana_url: str,
        dashboard_uid: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        log: Callable[[str], None] = print,
    ):
        for name, value in (("topic_arn", topic_arn), ("grafana_url", grafana_url), ("dashboard_uid", dashboard_uid)):
            if not value:
                raise ValueError(f"notifier {name} is empty")
        self._sns = sns
        self._topic = topic_arn
        self._incidents = incidents
        self._grafana_url = grafana_url
        self._dashboard_uid = dashboard_uid
        now = clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(f"notifier clock must return a tz-aware datetime, got {now!r}")
        self._clock = clock
        self._log = log

    def link(self, inc: Incident) -> str:
        return build_link(inc, self._grafana_url, self._dashboard_uid)

    def notify(
        self,
        open_incidents: Iterable[Incident | None],
        run_id: str = "",
        skip: Collection[str] = (),
        remaining_ms: Callable[[], int] | None = None,
    ) -> NotifyResult:
        """``skip``: pipes not to notify this run (replayed or conflicted: their state is not this run's).
        ``remaining_ms``: the Lambda's remaining time; below ``DEADLINE_MS`` the rest is left to the next run."""
        result = NotifyResult()
        pending = [
            i
            for i in open_incidents
            if i is not None and i.status == rules.OPEN and i.notified_at is None and i.pipe_id not in skip
        ]
        for inc in sorted(pending, key=lambda i: i.pipe_id):
            ref = {"pipe": inc.pipe_id, "incident_id": inc.incident_id}
            if remaining_ms is not None and remaining_ms() < DEADLINE_MS:
                self._fail(result, ref, "deadline", f"less than {DEADLINE_MS} ms left; left to the next run", run_id)
                continue
            try:
                link = self.link(inc)
                subj, msg = subject(inc), body(inc, link)
            except ValueError as exc:  # a malformed incident: recorded, never published
                self._fail(result, ref, "render", repr(exc), run_id)
                continue
            try:
                resp = self._sns.publish(TopicArn=self._topic, Subject=subj, Message=msg)
            except Exception as exc:  # noqa: BLE001 - AD-7: logged, retried by the next run
                self._fail(result, ref, "publish", _error(exc), run_id)
                continue
            message_id = str(resp.get("MessageId", "")) if isinstance(resp, dict) else ""
            try:
                marked = self._incidents.mark_notified(inc.pipe_id, inc.incident_id, self._clock())
            except Exception as exc:  # noqa: BLE001 - the email went out; record it, do not fail the run
                self._fail(result, {**ref, "message_id": message_id}, "mark", _error(exc), run_id)
                continue
            if not marked:
                self._fail(result, {**ref, "message_id": message_id}, "mark", "notified_at already set or incident changed", run_id)
                continue
            result.notified.append({**ref, "message_id": message_id, "link": link})
        return result

    def _fail(self, result: NotifyResult, ref: dict[str, str], step: str, error: str, run_id: str) -> None:
        entry = {**ref, "step": step, "error": error}
        result.failures.append(entry)
        self._log(json.dumps({"run_id": run_id, "pipe_id": ref["pipe"], "incident_id": ref["incident_id"], "level": "WARNING", "msg": "notify failed", **entry}))
