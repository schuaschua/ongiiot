"""In-memory AWS fakes for simulator and ops tests (no AWS calls), in the FakeSiteWise style."""

from __future__ import annotations

import copy


class FakeClientError(Exception):
    """Mimics botocore ClientError: carries ``response['Error']['Code']``."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeSiteWise:
    def __init__(self, unassociated=(), missing=(), throttle_first: int = 0, entry_errors=None):
        self.calls: list[list[dict]] = []
        self.described: list[str] = []
        self.unassociated = set(unassociated)
        self.missing = set(missing)
        self.throttle_first = throttle_first
        self.entry_errors = list(entry_errors or [])  # queue of errorCode per call (applied to first entry)

    def describe_time_series(self, alias):
        self.described.append(alias)
        if alias in self.missing:
            raise FakeClientError("ResourceNotFoundException", alias)
        out = {"alias": alias, "dataType": "DOUBLE", "timeSeriesId": f"ts-{alias}"}
        if alias not in self.unassociated:
            out |= {"assetId": "asset-1", "propertyId": f"prop-{alias}"}
        return out

    def batch_put_asset_property_value(self, entries):
        self.calls.append(copy.deepcopy(entries))
        if self.throttle_first:
            self.throttle_first -= 1
            raise FakeClientError("ThrottlingException", "slow down")
        if self.entry_errors:
            code = self.entry_errors.pop(0)
            return {
                "errorEntries": [
                    {"entryId": entries[0]["entryId"], "errors": [{"errorCode": code, "errorMessage": code}]}
                ]
            }
        return {"errorEntries": []}

    def values(self) -> dict[str, list[dict]]:
        """alias -> every propertyValue sent, in send order (successful and retried calls)."""
        out: dict[str, list[dict]] = {}
        for call in self.calls:
            for e in call:
                out.setdefault(e["propertyAlias"], []).extend(e["propertyValues"])
        return out


class FakeTable:
    """DynamoDB resource Table subset: scan, get_item, put_item (conditional)."""

    def __init__(self, items=(), page_size: int | None = None):
        self.items: dict[str, dict] = {i["pk"]: copy.deepcopy(i) for i in items}
        self.page_size = page_size
        self.scans = 0
        self.writes = 0
        self.fail_scans = 0  # the next N scans raise
        self.fail_puts = 0  # the next N puts raise

    def _maybe_fail(self, attr: str) -> None:
        if getattr(self, attr):
            setattr(self, attr, getattr(self, attr) - 1)
            raise FakeClientError("InternalServerError", "transient")

    def scan(self, **kwargs):
        self.scans += 1
        self._maybe_fail("fail_scans")
        keys = sorted(self.items)
        start = 0
        if "ExclusiveStartKey" in kwargs:
            start = keys.index(kwargs["ExclusiveStartKey"]["pk"]) + 1
        end = len(keys) if self.page_size is None else min(len(keys), start + self.page_size)
        page = {"Items": [copy.deepcopy(self.items[k]) for k in keys[start:end]]}
        if end < len(keys):
            page["LastEvaluatedKey"] = {"pk": keys[end - 1]}
        return page

    def get_item(self, Key, **kwargs):
        item = self.items.get(Key["pk"])
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, Item, ConditionExpression=None):
        self._maybe_fail("fail_puts")
        if ConditionExpression == "attribute_not_exists(pk)" and Item["pk"] in self.items:
            raise FakeClientError("ConditionalCheckFailedException", "exists")
        self.writes += 1
        self.items[Item["pk"]] = copy.deepcopy(Item)


class FakeClock:
    def __init__(self, t: float):
        self.t = float(t)
        self.on_sleep = None

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)
        if self.on_sleep:
            self.on_sleep(self)
