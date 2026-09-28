"""Per-stage Grafana provisioning as code, during ``cdk deploy`` (Story 1.5; AD-8, AD-16).

``GrafanaStack-<stage>`` holds one custom resource (``Custom::GrafanaProvision``, in the
style of ``SeedAttributes``) that provisions, in the shared workspace:

- folder ``ongiiot-<stage>``;
- SiteWise data source ``sitewise-<stage>`` (region ap-southeast-1, workspace IAM role);
- dashboards ``pipe-detail`` / ``facility-overview`` (``poc``) or ``pipe-detail-<initials>`` /
  ``facility-overview-<initials>``, from the templates in ``dashboards/`` with
  ``alias_root`` and the pipe list taken from ``facility/model.py``.

The handler mints a service-account token of at most 15 minutes and deletes it again.
Its role holds only the Grafana service-account actions on this workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

from facility.model import Facility
from infra.stacks.grafana import SharedGrafanaStack, workspace_arn

ROOT = Path(__file__).resolve().parent.parent.parent
HANDLER_DIR = ROOT / "infra" / "lambdas" / "grafana_provision"
DASHBOARDS_DIR = ROOT / "dashboards"
DASHBOARD_BASE_UIDS: tuple[str, ...] = ("pipe-detail", "facility-overview")
PLUGIN_ID = "grafana-iot-sitewise-datasource"
PLUGIN_VERSION = "2.6.3"  # spine Stack table; the handler reports the version actually running
REGION = "ap-southeast-1"

SERVICE_ACCOUNT_ACTIONS = (
    "grafana:CreateWorkspaceServiceAccount",
    "grafana:ListWorkspaceServiceAccounts",
    "grafana:DeleteWorkspaceServiceAccount",
    "grafana:CreateWorkspaceServiceAccountToken",
    "grafana:DeleteWorkspaceServiceAccountToken",
)


def uid_suffix(stage: str) -> str:
    """``""`` for ``poc``, ``-<initials>`` for ``poc-<initials>`` (AD-16)."""
    return "" if stage == "poc" else "-" + stage.removeprefix("poc-")


def dashboard_uid(base: str, stage: str) -> str:
    return base + uid_suffix(stage)


def folder_uid(stage: str) -> str:
    return f"ongiiot-{stage}"


def datasource_uid(stage: str) -> str:
    return f"sitewise-{stage}"


def load_templates() -> dict[str, dict[str, Any]]:
    """Dashboard templates keyed by base uid; each file's ``uid`` must equal its base uid."""
    out = {}
    for base in DASHBOARD_BASE_UIDS:
        doc = json.loads((DASHBOARDS_DIR / f"{base}.json").read_text(encoding="utf-8"))
        if doc.get("uid") != base:
            raise ValueError(f"dashboards/{base}.json: uid must be {base!r}, got {doc.get('uid')!r}")
        out[base] = doc
    return out


def provision_properties(facility: Facility, workspace_id: str, grafana_url: str) -> dict[str, str]:
    """Custom resource properties. Everything is a string: CloudFormation stringifies properties."""
    stage = facility.stage
    return {
        "Stage": stage,
        "WorkspaceId": workspace_id,
        "GrafanaUrl": grafana_url,
        "Region": REGION,
        "AliasRoot": facility.alias_root,
        "UidSuffix": uid_suffix(stage),
        "FolderUid": folder_uid(stage),
        "DataSourceUid": datasource_uid(stage),
        "PluginId": PLUGIN_ID,
        "PluginVersion": PLUGIN_VERSION,
        "Pipes": json.dumps([{"id": p.id, "area": p.area} for p in facility.pipes], separators=(",", ":")),
        "Dashboards": json.dumps(load_templates(), sort_keys=True, separators=(",", ":")),
    }


class GrafanaProvision(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        facility: Facility,
        workspace_id: str,
        grafana_url: str,
    ) -> None:
        super().__init__(scope, construct_id)
        stage = facility.stage
        stack = cdk.Stack.of(self)

        self.function = lambda_.Function(
            self,
            "Handler",
            function_name=f"grafana-provision-{stage}",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="handler.on_event",
            code=lambda_.Code.from_asset(str(HANDLER_DIR), exclude=["__pycache__", "*.pyc"]),
            timeout=cdk.Duration.minutes(5),
            memory_size=256,
            log_group=logs.LogGroup(
                self,
                "HandlerLogs",
                log_group_name=f"/aws/lambda/grafana-provision-{stage}",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(actions=list(SERVICE_ACCOUNT_ACTIONS), resources=[workspace_arn(stack, workspace_id)])
        )

        provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=self.function,
            provider_function_name=f"grafana-provision-provider-{stage}",
            log_group=logs.LogGroup(
                self,
                "ProviderLogs",
                log_group_name=f"/aws/lambda/grafana-provision-provider-{stage}",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
        )

        self.resource = cdk.CustomResource(
            self,
            "Resource",
            service_token=provider.service_token,
            resource_type="Custom::GrafanaProvision",
            properties=provision_properties(facility, workspace_id, grafana_url),
        )


class GrafanaStack(cdk.Stack):
    """Per-stage Grafana folder, data source and dashboards in the shared workspace."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        facility: Facility,
        shared: SharedGrafanaStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        stage = facility.stage
        self.provision = GrafanaProvision(
            self,
            "Provision",
            facility=facility,
            workspace_id=shared.workspace_id,
            grafana_url=shared.grafana_url,
        )
        self.add_stack_dependency(shared)

        # For the detection Lambda (Epic 3: GRAFANA_URL, DASHBOARD_UID).
        cdk.CfnOutput(self, "GrafanaUrl", value=shared.grafana_url)
        cdk.CfnOutput(self, "PipeDetailUid", value=dashboard_uid("pipe-detail", stage))
        cdk.CfnOutput(self, "FacilityOverviewUid", value=dashboard_uid("facility-overview", stage))
        cdk.CfnOutput(self, "FolderUid", value=folder_uid(stage))
        cdk.CfnOutput(self, "DataSourceUid", value=datasource_uid(stage))
        cdk.CfnOutput(self, "SiteWisePluginVersion", value=self.provision.resource.get_att_string("PluginVersion"))
