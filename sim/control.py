"""sim_control poller and injection_log writer (AD-4, AD-15).

The poller only reads ``sim_control`` (scan, at most every ``POLL_MAX_S`` seconds). Each
injection is logged to ``injection_log`` the first time it applies, with a conditional put
(``attribute_not_exists(pk)``) so a restart never logs it twice.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from facility.control import (
    ControlError,
    Injection,
    effective,
    format_ts,
    from_item,
)
from facility.model import Facility

POLL_MAX_S = 5.0

LOG = logging.getLogger("sim")


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


class ControlPoller:
    def __init__(self, control_table: Any, log_table: Any, facility: Facility, poll_s: float = POLL_MAX_S):
        if not 0 < poll_s <= POLL_MAX_S:
            raise ValueError(f"poll interval must be within (0, {POLL_MAX_S}] seconds")
        self._control = control_table
        self._log = log_table
        self._facility = facility
        self._poll_s = poll_s
        self._last_poll: float | None = None
        self._items: list[Injection] = []
        self._logged: set[str] = set()
        self._rejected: set[str] = set()

    @property
    def items(self) -> list[Injection]:
        return list(self._items)

    def poll(self, now: float) -> bool:
        """Scan ``sim_control`` if the poll interval has elapsed. Returns True if it scanned."""
        if self._last_poll is not None and now - self._last_poll < self._poll_s:
            return False
        self._last_poll = now
        try:
            raw = self._scan()
        except Exception as exc:  # noqa: BLE001 - keep the previous items; retry next interval
            LOG.error("sim_control scan failed", extra={"fields": {"error": str(exc)}})
            return False
        items = []
        for item in raw:
            try:
                items.append(from_item(item, self._facility))
            except ControlError as exc:
                pk = item.get("pk") if isinstance(item, Mapping) else None
                if pk not in self._rejected:
                    self._rejected.add(pk)
                    LOG.warning("ignoring invalid sim_control item", extra={"fields": {"pk": pk, "error": str(exc)}})
        self._items = items
        return True

    def _scan(self) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        kwargs: dict[str, Any] = {"ConsistentRead": True}
        while True:
            page = self._control.scan(**kwargs)
            out.extend(page.get("Items", []))
            key = page.get("LastEvaluatedKey")
            if not key:
                return out
            kwargs["ExclusiveStartKey"] = key

    def active(self, now: datetime) -> list[Injection]:
        return effective(self._items, now)

    def log_first_apply(
        self, inj: Injection, params: Mapping[str, float], applied_at: datetime, stage: str
    ) -> bool:
        """Append the first apply of ``inj`` to ``injection_log``. Returns True if this call wrote it."""
        if inj.injection_id in self._logged:
            return False
        item: dict[str, Any] = {
            "pk": inj.injection_id,
            "type": inj.type,
            "pipe_id": inj.pipe_id,
            "params": {k: Decimal(repr(float(v))) for k, v in params.items()},
            "start_at": format_ts(inj.start_at),
            "end_at": format_ts(inj.end_at),
            "applied_at": format_ts(applied_at),
            "stage": stage,
        }
        if inj.measurement is not None:
            item["measurement"] = inj.measurement
        try:
            self._log.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except Exception as exc:  # noqa: BLE001 - boto errors vary; a failed log write retries next tick
            if _error_code(exc) == "ConditionalCheckFailedException":
                self._logged.add(inj.injection_id)
                return False
            LOG.error(
                "injection_log write failed",
                extra={"fields": {"injection_id": inj.injection_id, "error": str(exc)}},
            )
            return False
        self._logged.add(inj.injection_id)
        LOG.info(
            "injection applied",
            extra={"fields": {"injection_id": inj.injection_id, "pipe_id": inj.pipe_id, "type": inj.type}},
        )
        return True
