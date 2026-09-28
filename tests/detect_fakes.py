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

    def __init__(self, throttle_first: int = 0, entry_error: str | None = None, fail_once=(), fail_once_code="ThrottlingException"):
        self.calls: list[list[dict]] = []
        self.fail_once = set(fail_once)  # aliases whose entry fails (fail_once_code) the first time only
        self.fail_once_code = fail_once_code
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
        failed = [e for e in entries if e["propertyAlias"] in self.fail_once]
        self.fail_once -= {e["propertyAlias"] for e in failed}
        for e in entries:
            if e in failed:
                continue
            for v in e["propertyValues"]:
                self.stored[(e["propertyAlias"], v["timestamp"]["timeInSeconds"])] = v["value"]
        return {
            "errorEntries": [
                {"entryId": e["entryId"], "errors": [{"errorCode": self.fail_once_code, "errorMessage": "x"}]}
                for e in failed
            ]
        }


class FakeSiteWise(FakeSiteWiseRead, FakeSiteWiseWrite):
    def __init__(self, store, page: int = 50, **write_kwargs):
        FakeSiteWiseRead.__init__(self, store, page=page)
        FakeSiteWiseWrite.__init__(self, **write_kwargs)


# -- DynamoDB (low-level client, typed AttributeValues) -----------------------------------------------

_CLAUSE_FN = re.compile(r"^(attribute_exists|attribute_not_exists)\(\s*([#\w]+)\s*\)$")
_CLAUSE_CMP = re.compile(r"^([#\w]+)\s*(=|<>|<=|>=|<|>)\s*(:\w+)$")


def _typed_value(v: dict):
    """Comparable Python value of a scalar AttributeValue (S or N)."""
    ((t, x),) = v.items()
    if t == "N":
        from decimal import Decimal

        return Decimal(x)
    if t == "S":
        return x
    raise AssertionError(f"fake compares only S and N, got {t}")


def evaluate_condition(expr: str | None, item: dict | None, names: dict | None = None, values: dict | None = None) -> bool:
    """DynamoDB condition semantics for the subset the adapters use: clauses joined by ``AND``, each
    ``attribute_exists(x)``, ``attribute_not_exists(x)`` or ``x <op> :v``. A comparison on a missing
    attribute is false, as in DynamoDB. Any other syntax is an error (so the fake never silently passes)."""
    if expr is None:
        return True
    names, values, item = names or {}, values or {}, item or {}
    for clause in re.split(r"\s+AND\s+", expr.strip()):
        m = _CLAUSE_FN.match(clause.strip())
        if m:
            attr = names.get(m.group(2), m.group(2))
            present = attr in item
            if present != (m.group(1) == "attribute_exists"):
                return False
            continue
        m = _CLAUSE_CMP.match(clause.strip())
        if not m:
            raise AssertionError(f"fake cannot evaluate condition clause {clause!r}")
        attr = names.get(m.group(1), m.group(1))
        if attr not in item:
            return False
        a, b = _typed_value(item[attr]), _typed_value(values[m.group(3)])
        ok = {"=": a == b, "<>": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[m.group(2)]
        if not ok:
            return False
    return True


class FakeDynamoDB:
    """In-memory DynamoDB low-level client for tables keyed ``pk``/``sk`` (both S): ``query`` (``pk = :pk``,
    ascending ``sk``, paged by ``page``), ``get_item``, ``put_item``, ``delete_item`` and ``transact_write_items``
    (all-or-nothing, ``TransactionCanceledException`` with ``CancellationReasons``), each honouring
    ``ConditionExpression``. ``before_write`` (callable(fake, op)) runs before every write, so a test can
    make another writer win a race. ``fail_writes``: a client error code raised on every write."""

    def __init__(self, table: str, page: int = 100):
        self.table = table
        self.page = page
        self.items: dict[tuple[str, str], dict] = {}
        self.writes: list[tuple[str, dict]] = []
        self.before_write = None
        self.fail_writes: str | None = None

    def _key(self, TableName, key_or_item):
        assert TableName == self.table, TableName
        return key_or_item["pk"]["S"], key_or_item["sk"]["S"]

    def _pre(self, op):
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook(self, op)
        if self.fail_writes:
            raise FakeClientError(self.fail_writes, "write failed")

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, ExclusiveStartKey=None, **kwargs):
        assert TableName == self.table and KeyConditionExpression == "pk = :pk"
        pk = ExpressionAttributeValues[":pk"]["S"]
        keys = sorted(k for k in self.items if k[0] == pk)
        start = 0
        if ExclusiveStartKey:
            start = keys.index((ExclusiveStartKey["pk"]["S"], ExclusiveStartKey["sk"]["S"])) + 1
        page = keys[start : start + self.page]
        out = {"Items": [copy.deepcopy(self.items[k]) for k in page]}
        if start + self.page < len(keys):
            out["LastEvaluatedKey"] = {"pk": {"S": page[-1][0]}, "sk": {"S": page[-1][1]}}
        return out

    def get_item(self, TableName, Key, **kwargs):
        item = self.items.get(self._key(TableName, Key))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def _check(self, key, cond, names, values):
        return evaluate_condition(cond, self.items.get(key), names, values)

    def put_item(self, TableName, Item, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        self._pre("put_item")
        key = self._key(TableName, Item)
        if not self._check(key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
            raise FakeClientError("ConditionalCheckFailedException", "The conditional request failed")
        self.items[key] = copy.deepcopy(Item)
        self.writes.append(("put_item", {"Item": copy.deepcopy(Item), "ConditionExpression": ConditionExpression}))
        return {}

    def delete_item(self, TableName, Key, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):
        self._pre("delete_item")
        key = self._key(TableName, Key)
        if not self._check(key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
            raise FakeClientError("ConditionalCheckFailedException", "The conditional request failed")
        self.items.pop(key, None)
        self.writes.append(("delete_item", {"Key": copy.deepcopy(Key), "ConditionExpression": ConditionExpression}))
        return {}

    def transact_write_items(self, TransactItems):
        self._pre("transact_write_items")
        assert 1 <= len(TransactItems) <= 100
        ops, reasons, touched = [], [], set()
        for t in TransactItems:
            ((kind, body),) = t.items()
            key = self._key(body["TableName"], body["Item"] if kind == "Put" else body["Key"])
            assert key not in touched, "one transaction may not touch an item twice"
            touched.add(key)
            ok = self._check(key, body.get("ConditionExpression"), body.get("ExpressionAttributeNames"), body.get("ExpressionAttributeValues"))
            reasons.append({"Code": "None"} if ok else {"Code": "ConditionalCheckFailed"})
            ops.append((kind, key, body))
        if any(r["Code"] != "None" for r in reasons):
            err = FakeClientError("TransactionCanceledException", "Transaction cancelled")
            err.response["CancellationReasons"] = reasons
            raise err
        for kind, key, body in ops:
            if kind == "Put":
                self.items[key] = copy.deepcopy(body["Item"])
            elif kind == "Delete":
                self.items.pop(key, None)
            elif kind != "ConditionCheck":
                raise AssertionError(f"fake does not support {kind}")
        self.writes.append(("transact_write_items", copy.deepcopy(TransactItems)))
        return {}

    # -- helpers for assertions ---------------------------------------------------------------------

    def plain(self) -> dict[tuple[str, str], dict]:
        """Every item, deserialized to plain Python values."""
        from boto3.dynamodb.types import TypeDeserializer

        de = TypeDeserializer()
        return {k: {a: de.deserialize(v) for a, v in item.items()} for k, item in self.items.items()}
