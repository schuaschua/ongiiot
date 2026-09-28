"""Seed per-pipe SiteWise attribute values by alias (custom resource, Provider framework).

Event ``ResourceProperties``:
    Entries: [{"alias": str, "type": "STRING"|"INTEGER"|"DOUBLE"|"BOOLEAN", "value": str}]
    ModelHash: str   # changes whenever any entry changes, which triggers an Update
    Stage: str

Create and Update write every entry with ``BatchPutAssetPropertyValue`` in batches of
at most 10. Delete is a no-op: the assets are deleted by their own resources.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import boto3
from botocore.config import Config

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

BATCH_SIZE = 10  # BatchPutAssetPropertyValue entry limit
MAX_ATTEMPTS = 8
RETRYABLE = {
    "ResourceNotFoundException",  # asset or alias not yet active right after creation
    "ThrottlingException",
    "LimitExceededException",
    "ServiceUnavailableException",
    "InternalFailureException",
    "ConflictingOperationException",
}

_client = None


def _sitewise():
    global _client
    if _client is None:
        _client = boto3.client("iotsitewise", config=Config(retries={"mode": "standard", "max_attempts": 5}))
    return _client


def on_event(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request = event["RequestType"]
    props = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId") or f"sitewise-attribute-seed-{props.get('Stage', 'unknown')}"
    LOG.info(json.dumps({"request": request, "stage": props.get("Stage"), "model_hash": props.get("ModelHash")}))

    if request in ("Create", "Update"):
        entries = props.get("Entries", [])
        written = write_entries(entries)
        LOG.info(json.dumps({"written": written}))
    return {"PhysicalResourceId": physical_id, "Data": {"ModelHash": props.get("ModelHash", "")}}


def to_variant(type_: str, value: str) -> dict[str, Any]:
    if type_ == "STRING":
        return {"stringValue": value}
    if type_ == "INTEGER":
        return {"integerValue": int(value)}
    if type_ == "DOUBLE":
        return {"doubleValue": float(value)}
    if type_ == "BOOLEAN":
        return {"booleanValue": value.lower() == "true"}
    raise ValueError(f"unsupported attribute type {type_!r}")


def build_batches(entries: list[dict[str, str]], now: float) -> list[list[dict[str, Any]]]:
    seconds = int(now)
    nanos = int((now - seconds) * 1e9)
    requests = [
        {
            "entryId": f"e{i}",
            "propertyAlias": e["alias"],
            "propertyValues": [
                {
                    "value": to_variant(e["type"], e["value"]),
                    "timestamp": {"timeInSeconds": seconds, "offsetInNanos": nanos},
                    "quality": "GOOD",
                }
            ],
        }
        for i, e in enumerate(entries)
    ]
    return [requests[i : i + BATCH_SIZE] for i in range(0, len(requests), BATCH_SIZE)]


def write_entries(entries: list[dict[str, str]], client: Any = None, sleep=time.sleep) -> int:
    client = client or _sitewise()
    batches = build_batches(entries, time.time())
    for batch in batches:
        pending = batch
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = client.batch_put_asset_property_value(entries=pending)
            errors = response.get("errorEntries", [])
            if not errors:
                break
            failed_ids = {e["entryId"] for e in errors}
            codes = {err["errorCode"] for e in errors for err in e.get("errors", [])}
            if attempt == MAX_ATTEMPTS or not codes or not codes <= RETRYABLE:
                raise RuntimeError(f"BatchPutAssetPropertyValue failed: {json.dumps(errors, default=str)[:3000]}")
            pending = [r for r in pending if r["entryId"] in failed_ids]
            sleep(min(2**attempt, 30))
    return len(entries)
