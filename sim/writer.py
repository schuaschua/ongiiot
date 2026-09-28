"""Raw SiteWise writer: alias-addressed ``BatchPutAssetPropertyValue`` only (AD-1, AD-3, AD-17).

- Refuses any alias with a ``derived`` or ``attr`` segment, and any alias outside the
  allowed raw set, before any API call.
- Per-alias timestamps strictly increase for the life of the writer; a repeat or an older
  timestamp raises. Values are sent as produced, never clamped.
- Flushes in requests of at most 10 entries, each with at most 10 values.
- Retries throttling and transient errors (both raised exceptions and ``errorEntries``),
  sleeping at most ``MAX_BACKOFF_PER_FLUSH_S`` in total per flush. This is the only retry
  layer: the boto client is created with SDK retries off.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable
from typing import Any

MAX_ENTRIES = 10  # entries per BatchPutAssetPropertyValue request
MAX_VALUES = 10  # values per entry
MAX_ATTEMPTS = 6
MAX_BACKOFF_PER_FLUSH_S = 1.0  # total retry sleep per flush; keeps the 1 Hz loop live
RETRYABLE = frozenset(
    {
        "ThrottlingException",
        "LimitExceededException",
        "ServiceUnavailableException",
        "InternalFailureException",
        "RequestLimitExceeded",
    }
)
FORBIDDEN_SEGMENTS = frozenset({"derived", "attr"})
QUALITIES = frozenset({"GOOD", "BAD", "UNCERTAIN"})


class AliasError(ValueError):
    """The writer was asked to write an alias it must never write."""


class TimestampError(ValueError):
    """A per-alias timestamp did not strictly increase."""


class WriteError(RuntimeError):
    """SiteWise rejected a write, or retries were exhausted."""


def check_raw_alias(alias: str, allowed: frozenset[str] | None = None) -> None:
    if not isinstance(alias, str) or not alias.startswith("/"):
        raise AliasError(f"invalid alias {alias!r}")
    hit = FORBIDDEN_SEGMENTS.intersection(alias.split("/"))
    if hit:
        raise AliasError(f"refusing to write {min(hit)}/ alias {alias}: the simulator writes raw aliases only")
    if allowed is not None and alias not in allowed:
        raise AliasError(f"refusing to write {alias}: not a raw alias of the facility model")


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


class RawWriter:
    def __init__(
        self,
        client: Any,
        allowed: Iterable[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        self._client = client
        self._allowed = frozenset(allowed) if allowed is not None else None
        if self._allowed is not None:
            for a in self._allowed:
                check_raw_alias(a)
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._buffer: dict[str, list[tuple[float, float, str]]] = {}
        self._last_ts: dict[str, float] = {}
        self._entry_seq = 0
        self._budget = MAX_BACKOFF_PER_FLUSH_S

    @property
    def pending(self) -> int:
        return sum(len(v) for v in self._buffer.values())

    def add(self, alias: str, t: float, value: float, quality: str = "GOOD") -> None:
        check_raw_alias(alias, self._allowed)
        if quality not in QUALITIES:
            raise ValueError(f"invalid quality {quality!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{alias}: value must be a finite number, got {value!r}")
        last = self._last_ts.get(alias)
        if last is not None and not t > last:
            raise TimestampError(f"{alias}: timestamp {t} does not increase past {last}")
        self._last_ts[alias] = t
        self._buffer.setdefault(alias, []).append((t, float(value), quality))

    def _entries(self) -> list[dict[str, Any]]:
        entries = []
        for alias, values in self._buffer.items():
            for i in range(0, len(values), MAX_VALUES):
                self._entry_seq += 1
                entries.append(
                    {
                        "entryId": f"e{self._entry_seq}",
                        "propertyAlias": alias,
                        "propertyValues": [_value(t, v, q) for t, v, q in values[i : i + MAX_VALUES]],
                    }
                )
        return entries

    def flush(self) -> int:
        """Send every buffered value. Returns the number of values written.

        Every batch is attempted even if an earlier one fails; failures are then raised as one
        ``WriteError``. Retry backoff sleeps at most ``MAX_BACKOFF_PER_FLUSH_S`` per flush so the
        1 Hz loop is never blocked for long. The buffer is cleared even on failure: values are
        never resent later out of order.
        """
        entries = self._entries()
        count = self.pending
        self._buffer.clear()
        self._budget = MAX_BACKOFF_PER_FLUSH_S
        failed: list[dict[str, Any]] = []
        reasons: list[str] = []
        for i in range(0, len(entries), MAX_ENTRIES):
            bad, reason = self._send(entries[i : i + MAX_ENTRIES])
            if bad:
                failed.extend(bad)
                reasons.append(reason)
        if failed:
            aliases = sorted({e["propertyAlias"] for e in failed})
            n = sum(len(e["propertyValues"]) for e in failed)
            raise WriteError(
                f"{n} of {count} values not written for {len(aliases)} aliases "
                f"({', '.join(aliases[:10])}{', ...' if len(aliases) > 10 else ''}): {'; '.join(reasons)[:2000]}"
            )
        return count

    def _backoff(self, attempt: int) -> bool:
        """Sleep before a retry within the per-flush budget. False when the budget is spent."""
        delay = min(0.1 * 2 ** (attempt - 1), self._budget)
        if delay <= 0:
            return False
        self._budget -= delay
        self._sleep(delay)
        return True

    def _send(self, batch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """Send one request with retries. Returns (entries not written, reason)."""
        pending = batch
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.batch_put_asset_property_value(entries=pending)
            except Exception as exc:  # noqa: BLE001 - botocore errors carry their code in .response
                code = _error_code(exc)
                if code not in RETRYABLE or attempt == self._max_attempts or not self._backoff(attempt):
                    return pending, f"BatchPutAssetPropertyValue failed ({code or type(exc).__name__}): {exc}"
                continue
            errors = response.get("errorEntries", [])
            if not errors:
                return [], ""
            failed = {e["entryId"] for e in errors}
            pending = [e for e in pending if e["entryId"] in failed]
            codes = {err.get("errorCode") for e in errors for err in e.get("errors", [])}
            if attempt == self._max_attempts or not codes or not codes <= RETRYABLE or not self._backoff(attempt):
                return pending, f"BatchPutAssetPropertyValue errors: {json.dumps(errors, default=str)[:500]}"
        return pending, "retries exhausted"


def _value(t: float, v: float, q: str) -> dict[str, Any]:
    seconds = math.floor(t)
    nanos = round((t - seconds) * 1e9)
    if nanos >= 1_000_000_000:
        seconds, nanos = seconds + 1, 0
    return {
        "value": {"doubleValue": v},
        "timestamp": {"timeInSeconds": seconds, "offsetInNanos": nanos},
        "quality": q,
    }
