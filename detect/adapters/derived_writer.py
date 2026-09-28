"""Derived SiteWise writer: the only writer of ``derived/`` aliases (AD-3, AD-13, AD-17).

- Refuses any alias that is not ``…/derived/<name>`` (and, when given, not in the allowed set)
  before any API call. IAM cannot scope writes by alias, so this guard is the raw/derived boundary.
- Every value of one ``write`` carries the same timestamp (the run's ``window_end``), so a replayed
  run overwrites identically (AD-9).
- The value variant comes from the property's SiteWise data type (``types``: alias -> ``DOUBLE`` |
  ``INTEGER`` | ``STRING``, from the facility's derived properties), never from the Python type:
  DOUBLE takes a finite number (sent as ``doubleValue``), INTEGER an ``int`` in the 32-bit signed range
  (``integerValue``), STRING a ``str`` (``stringValue``). ``bool`` is always refused. An alias without a
  declared type is DOUBLE, or STRING for a ``str`` value (never INTEGER).
- Requests of at most 10 entries, one value each. Throttling and transient errors are retried a
  few times; anything else raises ``WriteError`` (an adapter failure fails the invocation).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

MAX_ENTRIES = 10  # entries per BatchPutAssetPropertyValue request
MAX_ATTEMPTS = 4
RETRYABLE = frozenset(
    {
        "ThrottlingException",
        "LimitExceededException",
        "ServiceUnavailableException",
        "InternalFailureException",
        "RequestLimitExceeded",
    }
)
DERIVED_SEGMENT = "derived"


class AliasError(ValueError):
    """The writer was asked to write an alias outside ``derived/``."""


class WriteError(RuntimeError):
    """SiteWise rejected a derived write, or retries were exhausted."""


def check_derived_alias(alias: str, allowed: frozenset[str] | None = None) -> None:
    if not isinstance(alias, str) or not alias.startswith("/"):
        raise AliasError(f"invalid alias {alias!r}")
    parts = alias.split("/")
    if len(parts) < 3 or parts[-2] != DERIVED_SEGMENT or not parts[-1] or parts.count(DERIVED_SEGMENT) != 1:
        raise AliasError(f"refusing to write {alias}: the detection run writes derived/ aliases only")
    if allowed is not None and alias not in allowed:
        raise AliasError(f"refusing to write {alias}: not a derived alias of the facility model")


INT_MIN, INT_MAX = -(2**31), 2**31 - 1


DATA_TYPES = frozenset({"DOUBLE", "INTEGER", "STRING"})


def _variant(alias: str, value: Any, data_type: str | None = None) -> dict[str, Any]:
    if data_type is None:
        data_type = "STRING" if isinstance(value, str) else "DOUBLE"
    if isinstance(value, bool):
        raise ValueError(f"{alias}: bool is not a valid {data_type} value")
    if data_type == "STRING":
        if not isinstance(value, str):
            raise ValueError(f"{alias}: STRING property needs a str, got {value!r}")
        return {"stringValue": value}
    if data_type == "INTEGER":
        if not isinstance(value, int) or not INT_MIN <= value <= INT_MAX:
            raise ValueError(f"{alias}: INTEGER property needs a 32-bit int, got {value!r}")
        return {"integerValue": value}
    if data_type == "DOUBLE":
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{alias}: DOUBLE property needs a finite number, got {value!r}")
        return {"doubleValue": float(value)}
    raise ValueError(f"{alias}: unknown data type {data_type!r}")


def _timestamp(t: datetime) -> dict[str, int]:
    if t.tzinfo is None or t.utcoffset() != timedelta(0):
        raise ValueError(f"timestamp must be tz-aware UTC, got {t!r}")
    seconds = math.floor(t.timestamp())
    return {"timeInSeconds": seconds, "offsetInNanos": t.microsecond * 1000}


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


class DerivedWriter:
    def __init__(
        self,
        client: Any,
        allowed: Iterable[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        types: Mapping[str, str] | None = None,
    ):
        self._client = client
        self._allowed = frozenset(allowed) if allowed is not None else None
        if self._allowed is not None:
            for a in self._allowed:
                check_derived_alias(a)
        self._types = dict(types or {})
        bad = {t for t in self._types.values() if t not in DATA_TYPES}
        if bad:
            raise ValueError(f"unknown data types {sorted(bad)}")
        self._sleep = sleep
        self._max_attempts = max_attempts

    def write(self, values: Mapping[str, float | int | str], at: datetime) -> int:
        """Write ``alias -> value`` at ``at``. Returns the number of values written."""
        # Validate everything before the first API call.
        for alias in values:
            check_derived_alias(alias, self._allowed)
        ts = _timestamp(at)
        entries = [
            {
                "entryId": f"e{i}",
                "propertyAlias": alias,
                "propertyValues": [{"value": _variant(alias, v, self._types.get(alias)), "timestamp": ts, "quality": "GOOD"}],
            }
            for i, (alias, v) in enumerate(sorted(values.items()))
        ]
        for i in range(0, len(entries), MAX_ENTRIES):
            self._send(entries[i : i + MAX_ENTRIES])
        return len(entries)

    def _send(self, batch: list[dict[str, Any]]) -> None:
        pending = batch
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            try:
                response = self._client.batch_put_asset_property_value(entries=pending)
            except Exception as exc:  # noqa: BLE001 - botocore errors carry their code in .response
                code = _error_code(exc)
                if code not in RETRYABLE or last:
                    raise WriteError(f"BatchPutAssetPropertyValue failed ({code or type(exc).__name__}): {exc}") from exc
                self._sleep(0.1 * 2 ** (attempt - 1))
                continue
            errors = response.get("errorEntries", [])
            if not errors:
                return
            failed = {e["entryId"] for e in errors}
            pending = [e for e in pending if e["entryId"] in failed]
            codes = {err.get("errorCode") for e in errors for err in e.get("errors", [])}
            if last or not codes or not codes <= RETRYABLE:
                raise WriteError(f"BatchPutAssetPropertyValue errors: {json.dumps(errors, default=str)[:1000]}")
            self._sleep(0.1 * 2 ** (attempt - 1))
