"""In-memory SiteWise fakes for the detection reader and writer (no AWS calls)."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime

from tests.sim_fakes import FakeClientError

_LIKE = re.compile(r"property_alias LIKE '((?:[^']|'')*)%'")
_TS = re.compile(r"event_timestamp (>=|<) TIMESTAMP '([^']+)'")


class FakeSiteWiseRead:
    """``store``: alias -> list of (epoch seconds, value, quality), in any order.

    Serves ``execute_query`` (the reader's LIKE statement, descending like SiteWise, paged by
    ``page``) and ``batch_get_asset_property_value_history`` (inclusive bounds, paged by ``page``
    values per request). ``fail`` raises a client error on every read.
    """

    def __init__(self, store, page: int = 50, fail: str | None = None):
        self.store = store
        self.page = page
        self.fail = fail
        self.queries: list[dict] = []
        self.batch_calls: list[dict] = []

    # -- ExecuteQuery ---------------------------------------------------------------------------

    def execute_query(self, queryStatement, maxResults=None, nextToken=None):
        self.queries.append({"queryStatement": queryStatement, "nextToken": nextToken})
        if self.fail:
            raise FakeClientError(self.fail, "read failed")
        prefix = _LIKE.search(queryStatement).group(1).replace("''", "'")
        bounds = {op: datetime.fromisoformat(v).replace(tzinfo=UTC).timestamp() for op, v in _TS.findall(queryStatement)}
        rows = [
            (alias, t, v, q)
            for alias, values in self.store.items()
            if alias.startswith(prefix)
            for t, v, q in values
            if bounds[">="] <= t < bounds["<"]
        ]
        rows.sort(key=lambda r: (-r[1], r[0]))
        start = int(nextToken or 0)
        page = rows[start : start + self.page]
        out = {
            "columns": [{"name": n} for n in ("property_alias", "event_timestamp", "quality", "double_value")],
            "rows": [
                {
                    "data": [
                        {"scalarValue": alias},
                        {"scalarValue": datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]},
                        {"scalarValue": q},
                        {"nullValue": {"valueType": "DOUBLE"}} if v is None else {"scalarValue": repr(v)},
                    ]
                }
                for alias, t, v, q in page
            ],
        }
        if start + self.page < len(rows):
            out["nextToken"] = str(start + self.page)
        return out

    # -- BatchGetAssetPropertyValueHistory --------------------------------------------------------

    def batch_get_asset_property_value_history(self, entries, maxResults=None, nextToken=None):
        self.batch_calls.append({"entries": copy.deepcopy(entries), "nextToken": nextToken})
        if self.fail:
            raise FakeClientError(self.fail, "read failed")
        assert len(entries) <= 16
        flat = []
        for e in entries:
            lo, hi = e["startDate"].timestamp(), e["endDate"].timestamp()
            values = sorted(v for v in self.store.get(e["propertyAlias"], []) if lo <= v[0] <= hi)
            flat += [(e["entryId"], v) for v in values]
        start = int(nextToken or 0)
        page = flat[start : start + self.page]
        success: dict[str, list] = {}
        for eid, (t, v, q) in page:
            secs = int(t // 1)
            success.setdefault(eid, []).append(
                {
                    "value": {} if v is None else {"doubleValue": v},
                    "timestamp": {"timeInSeconds": secs, "offsetInNanos": round((t - secs) * 1e9)},
                    "quality": q,
                }
            )
        out = {
            "errorEntries": [],
            "skippedEntries": [],
            "successEntries": [{"entryId": eid, "assetPropertyValueHistory": vs} for eid, vs in success.items()],
        }
        if start + self.page < len(flat):
            out["nextToken"] = str(start + self.page)
        return out


class FakeSiteWiseWrite:
    """Records ``batch_put_asset_property_value`` calls; the latest value per (alias, timestamp) wins."""

    def __init__(self, throttle_first: int = 0, entry_error: str | None = None):
        self.calls: list[list[dict]] = []
        self.throttle_first = throttle_first
        self.entry_error = entry_error
        self.stored: dict[tuple[str, int], dict] = {}

    def batch_put_asset_property_value(self, entries):
        self.calls.append(copy.deepcopy(entries))
        if self.throttle_first:
            self.throttle_first -= 1
            raise FakeClientError("ThrottlingException", "slow down")
        if self.entry_error:
            return {
                "errorEntries": [
                    {"entryId": entries[0]["entryId"], "errors": [{"errorCode": self.entry_error, "errorMessage": "x"}]}
                ]
            }
        for e in entries:
            for v in e["propertyValues"]:
                self.stored[(e["propertyAlias"], v["timestamp"]["timeInSeconds"])] = v["value"]
        return {"errorEntries": []}


class FakeSiteWise(FakeSiteWiseRead, FakeSiteWiseWrite):
    def __init__(self, store, page: int = 50, **write_kwargs):
        FakeSiteWiseRead.__init__(self, store, page=page)
        FakeSiteWiseWrite.__init__(self, **write_kwargs)
