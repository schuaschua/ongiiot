"""SiteWise window reader: the only detection read path (AD-18).

``read(pipes, start, end) -> FacilityWindow`` returns raw samples for ``[start, end)`` with every
expected raw alias present (an empty list when there is no data), samples ascending by time.
Two strategies share that contract; ``READER_STRATEGY`` picks one (``make_reader``; default
``DEFAULT_STRATEGY``, ``batch_get``):

- ``execute_query``: SiteWise ``ExecuteQuery`` on ``raw_time_series``, filtered by the aliases'
  common prefix and ``event_timestamp``, paged.
- ``batch_get``: ``BatchGetAssetPropertyValueHistory``, 16 entries per request, requests in parallel.

UTC conversion from SiteWise representations happens only here (AD-9). The alias prefix and request
groups depend only on the expected aliases, so they are built once per cold start (AD-18).
Any SiteWise error raises: an adapter failure fails the invocation (Consistency Conventions).
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from detect.core.models import QUALITIES, FacilityWindow, PipeMeta, Sample, Window

STRATEGIES = ("execute_query", "batch_get")
DEFAULT_STRATEGY = "batch_get"  # AD-18 spike (2026-09-29): the only default; handler and stack import it
BATCH_GET_ENTRIES = 16  # BatchGetAssetPropertyValueHistory entries per request
BATCH_GET_MAX_RESULTS = 20000  # values per page
QUERY_MAX_RESULTS = 20000  # rows per page
MAX_WORKERS = 10
QUERY_COLUMNS = ("property_alias", "event_timestamp", "quality", "double_value")


class ReaderError(RuntimeError):
    """SiteWise returned something the reader cannot use (error entries, unknown alias, bad row)."""


class Reader(Protocol):
    strategy: str

    def read(self, pipes: Mapping[str, PipeMeta], start: datetime, end: datetime) -> FacilityWindow: ...


def make_reader(strategy: str | None, client: Any) -> Reader:
    name = strategy or DEFAULT_STRATEGY
    if name == "execute_query":
        return ExecuteQueryReader(client)
    if name == "batch_get":
        return BatchGetReader(client)
    raise ValueError(f"READER_STRATEGY={strategy!r} is invalid: use one of {STRATEGIES}")


def expected_aliases(pipes: Mapping[str, PipeMeta]) -> tuple[str, ...]:
    return tuple(sorted(a for p in pipes.values() for a in p.aliases.values()))


def _check_bounds(start: datetime, end: datetime) -> None:
    for t in (start, end):
        if t.tzinfo is None or t.utcoffset() != timedelta(0):
            raise ValueError(f"window bounds must be tz-aware UTC, got {t!r}")
    if not start < end:
        raise ValueError(f"empty window [{start}, {end})")


def _assemble(
    pipes: Mapping[str, PipeMeta], start: datetime, end: datetime, rows: Iterable[tuple[str, Sample]]
) -> FacilityWindow:
    """Group samples by alias, keep ``[start, end)``, drop duplicate timestamps, sort ascending."""
    series: dict[str, dict[datetime, Sample]] = {a: {} for a in expected_aliases(pipes)}
    for alias, s in rows:
        if alias not in series:
            raise ReaderError(f"SiteWise returned an unexpected alias {alias!r}")
        if start <= s.ts < end:
            series[alias][s.ts] = s
    return FacilityWindow(
        pipes=pipes,
        window=Window(start=start, end=end, series={a: [by_ts[t] for t in sorted(by_ts)] for a, by_ts in series.items()}),
    )


def _quality(q: Any) -> str:
    if q not in QUALITIES:
        raise ReaderError(f"unknown quality {q!r}")
    return q


def _pool_map(fn: Callable[[Any], list], items: Sequence[Any]) -> list:
    if len(items) <= 1:
        return [x for item in items for x in fn(item)]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(items))) as pool:
        return [x for chunk in pool.map(fn, items) for x in chunk]


# -- ExecuteQuery ---------------------------------------------------------------------------------


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sql_ts(t: datetime) -> str:
    return "TIMESTAMP '" + t.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + "'"


def _parse_query_ts(s: str) -> datetime:
    """``YYYY-MM-DD HH:MM:SS[.fff…]`` in UTC (SiteWise query output) -> tz-aware datetime."""
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise ReaderError(f"unparseable event_timestamp {s!r}") from exc


def _scalar(datum: Mapping[str, Any]) -> str | None:
    if datum.get("nullValue"):
        return None
    return datum.get("scalarValue")


class ExecuteQueryReader:
    """One ``LIKE '<common prefix>%'`` statement over every expected alias, paged.

    A long ``property_alias IN (...)`` list is not used: in the spike (2026-09-29) SiteWise returned
    samples for only a subset of a 144-alias ``IN`` list. The prefix is the longest common string
    prefix of the expected aliases, trimmed to its last ``/`` so it never ends mid-segment
    (``/poc-dj`` would also match ``/poc-dj2/...``); no alias parsing (AD-1). Rows for any other alias under it,
    such as ``derived/`` ones, are dropped here.
    """

    strategy = "execute_query"

    def __init__(self, client: Any, max_results: int = QUERY_MAX_RESULTS):
        self._client = client
        self._max_results = max_results
        self._prefixes: dict[tuple[str, ...], str] = {}  # per cold start

    @staticmethod
    def statement(prefix: str, start: datetime, end: datetime) -> str:
        cols = ", ".join(f"r.{c}" for c in QUERY_COLUMNS)
        return (
            f"SELECT {cols} FROM raw_time_series r "
            f"WHERE r.property_alias LIKE {_sql_literal(prefix + '%')} "
            f"AND r.event_timestamp >= {_sql_ts(start)} AND r.event_timestamp < {_sql_ts(end)}"
        )

    def _prefix(self, aliases: tuple[str, ...]) -> str:
        if aliases not in self._prefixes:
            common = os.path.commonprefix(list(aliases))
            self._prefixes[aliases] = common[: common.rfind("/") + 1]
        return self._prefixes[aliases]

    def _run(self, statement: str, wanted: frozenset[str]) -> list[tuple[str, Sample]]:
        out: list[tuple[str, Sample]] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {"queryStatement": statement, "maxResults": self._max_results}
            if token:
                kwargs["nextToken"] = token
            resp = self._client.execute_query(**kwargs)
            names = [c["name"] for c in resp.get("columns", [])]
            if names and names != list(QUERY_COLUMNS):
                raise ReaderError(f"unexpected ExecuteQuery columns {names}")
            for row in resp.get("rows", []):
                data = row.get("data", [])
                if len(data) != len(QUERY_COLUMNS):
                    raise ReaderError(f"unexpected ExecuteQuery row width {len(data)}")
                alias, ts, quality, value = (_scalar(d) for d in data)
                if alias is None or ts is None:
                    raise ReaderError("ExecuteQuery row without alias or timestamp")
                if alias not in wanted:
                    continue
                num = None if value is None else float(value)
                if num is not None and not math.isfinite(num):
                    num = None
                out.append((alias, Sample(ts=_parse_query_ts(ts), value=num, quality=_quality(quality))))
            token = resp.get("nextToken")
            if not token:
                return out

    def read(self, pipes: Mapping[str, PipeMeta], start: datetime, end: datetime) -> FacilityWindow:
        _check_bounds(start, end)
        aliases = expected_aliases(pipes)
        # Second granularity in the literals: widen to whole seconds, `_assemble` trims to [start, end).
        lo = start.replace(microsecond=0)
        hi = end if end.microsecond == 0 else end.replace(microsecond=0) + timedelta(seconds=1)
        rows = self._run(self.statement(self._prefix(aliases), lo, hi), frozenset(aliases))
        return _assemble(pipes, start, end, rows)


# -- BatchGetAssetPropertyValueHistory ------------------------------------------------------------


def _variant_value(v: Mapping[str, Any]) -> float | None:
    if "doubleValue" in v:
        num = float(v["doubleValue"])
    elif "integerValue" in v:
        num = float(v["integerValue"])
    else:
        return None
    return num if math.isfinite(num) else None


def _epoch_ts(t: Mapping[str, Any]) -> datetime:
    return datetime.fromtimestamp(t["timeInSeconds"], tz=UTC) + timedelta(microseconds=t.get("offsetInNanos", 0) // 1000)


class BatchGetReader:
    strategy = "batch_get"

    def __init__(self, client: Any, per_request: int = BATCH_GET_ENTRIES, max_results: int = BATCH_GET_MAX_RESULTS):
        self._client = client
        self._per_request = per_request
        self._max_results = max_results
        self._groups: dict[tuple[str, ...], list[tuple[str, ...]]] = {}  # per cold start

    def _group(self, aliases: tuple[str, ...]) -> list[tuple[str, ...]]:
        if aliases not in self._groups:
            n = self._per_request
            self._groups[aliases] = [aliases[i : i + n] for i in range(0, len(aliases), n)]
        return self._groups[aliases]

    def _run(self, job: tuple[tuple[str, ...], datetime, datetime]) -> list[tuple[str, Sample]]:
        aliases, start, end = job
        ids = {f"e{i}": a for i, a in enumerate(aliases)}
        base = [
            {"entryId": eid, "propertyAlias": a, "startDate": start, "endDate": end, "timeOrdering": "ASCENDING"}
            for eid, a in ids.items()
        ]
        out: list[tuple[str, Sample]] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {"entries": base, "maxResults": self._max_results}
            if token:
                kwargs["nextToken"] = token
            resp = self._client.batch_get_asset_property_value_history(**kwargs)
            if resp.get("errorEntries"):
                raise ReaderError(f"BatchGetAssetPropertyValueHistory errors: {resp['errorEntries'][:3]}")
            for skipped in resp.get("skippedEntries", []):
                if skipped.get("completionStatus") != "SUCCESS":
                    raise ReaderError(f"BatchGetAssetPropertyValueHistory skipped an entry: {skipped}")
            for entry in resp.get("successEntries", []):
                alias = ids.get(entry["entryId"])
                if alias is None:
                    raise ReaderError(f"unknown entryId {entry['entryId']!r}")
                for v in entry.get("assetPropertyValueHistory", []):
                    s = Sample(ts=_epoch_ts(v["timestamp"]), value=_variant_value(v["value"]), quality=_quality(v.get("quality", "GOOD")))
                    out.append((alias, s))
            token = resp.get("nextToken")
            if not token:
                return out

    def read(self, pipes: Mapping[str, PipeMeta], start: datetime, end: datetime) -> FacilityWindow:
        _check_bounds(start, end)
        # SiteWise date bounds are whole seconds with loosely documented inclusivity: widen by a
        # second on both sides and let `_assemble` trim to [start, end).
        lo = start.replace(microsecond=0) - timedelta(seconds=1)
        hi = end.replace(microsecond=0) + timedelta(seconds=1)
        jobs = [(g, lo, hi) for g in self._group(expected_aliases(pipes))]
        return _assemble(pipes, start, end, _pool_map(self._run, jobs))
