"""Custom resource that writes per-pipe attribute values by alias (CAP-2).

Asset model attributes only carry defaults. This resource writes each pipe's own
values (service, material, age, peer group and effective contract, overrides
included) to ``<alias_root>/<area>/<pipe_id>/attr/<name>`` with
``BatchPutAssetPropertyValue``. It re-runs whenever the facility model hash
(canonical JSON of every entry it writes) changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iotsitewise as sitewise
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

from facility.model import Facility

HANDLER_DIR = Path(__file__).resolve().parent.parent / "lambdas" / "seed_attributes"


def attribute_entries(facility: Facility) -> list[dict[str, str]]:
    """One entry per pipe attribute: alias, SiteWise type and value as a string.

    CloudFormation passes custom resource properties as strings, so the type travels
    with the value and the handler converts it back.
    """
    types = {a.name: a.type for a in facility.attribute_properties()}
    entries: list[dict[str, str]] = []
    for pipe in facility.pipes:
        for name, value in facility.attribute_values(pipe.id).items():
            entries.append(
                {"alias": facility.attr_alias(pipe.id, name), "type": types[name], "value": str(value)}
            )
    return entries


def model_hash(entries: list[dict[str, str]]) -> str:
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SeedAttributes(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        facility: Facility,
        pipe_assets: dict[str, sitewise.CfnAsset],
    ) -> None:
        super().__init__(scope, construct_id)
        stage = facility.stage
        entries = attribute_entries(facility)

        self.function = lambda_.Function(
            self,
            "Handler",
            function_name=f"seed-attributes-{stage}",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="handler.on_event",
            code=lambda_.Code.from_asset(str(HANDLER_DIR), exclude=["__pycache__", "*.pyc"]),
            timeout=cdk.Duration.minutes(5),
            memory_size=256,
            log_group=logs.LogGroup(
                self,
                "HandlerLogs",
                log_group_name=f"/aws/lambda/seed-attributes-{stage}",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iotsitewise:BatchPutAssetPropertyValue"],
                resources=[a.attr_asset_arn for a in pipe_assets.values()],
            )
        )

        provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=self.function,
            provider_function_name=f"seed-attributes-provider-{stage}",
            log_group=logs.LogGroup(
                self,
                "ProviderLogs",
                log_group_name=f"/aws/lambda/seed-attributes-provider-{stage}",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )

        self.resource = cdk.CustomResource(
            self,
            "Resource",
            service_token=provider.service_token,
            resource_type="Custom::SiteWiseAttributeSeed",
            properties={
                "Stage": stage,
                "ModelHash": model_hash(entries),
                # Replaced pipe assets get new IDs, which forces a re-seed.
                "PipeAssetIds": [pipe_assets[p.id].attr_asset_id for p in facility.pipes],
                "Entries": entries,
            },
        )
        for asset in pipe_assets.values():
            self.resource.node.add_dependency(asset)
