"""Shared Amazon Managed Grafana workspace (Story 1.5; AD-11, AD-16 as amended 2026-09-28).

Fixed name ``SharedGrafanaStack`` (no stage suffix), like ``SharedNetworkStack``: every
``GrafanaStack-<stage>`` depends on it, so any stage deploy creates or reuses it, and
deleting a stage leaves it in place.

- Workspace: Grafana 12.4, IAM Identity Center (``AWS_SSO``) sign-in, current account,
  service-managed permissions, data source ``SITEWISE`` only, plugin admin enabled.
- Workspace role: ``AWSIoTSiteWiseReadOnlyAccess`` and nothing else (AD-11, AD-17).
- Admin assignment for the Identity Center users in ``ADMIN_USER_IDS`` through
  ``grafana:UpdatePermissions`` (CloudFormation has no property for it).
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_grafana as grafana
from aws_cdk import aws_iam as iam
from aws_cdk import custom_resources as cr
from constructs import Construct

STACK_NAME = "SharedGrafanaStack"
WORKSPACE_NAME = "ongiiot"
GRAFANA_VERSION = "12.4"
SITEWISE_READ_ONLY = "AWSIoTSiteWiseReadOnlyAccess"
# Human decision 2026-09-28: only the workspace owner, as Admin. Add users here
# (docs/ci-cd.md). Removing an ID here does NOT revoke it on update (the update call only
# ADDs); revoke by hand with `aws grafana update-permissions` (docs/ci-cd.md section 6).
ADMIN_USER_IDS: tuple[str, ...] = ("00000000-0000-0000-0000-000000000000",)

# grafana:UpdatePermissions for SSO users also calls Identity Center on the caller's behalf
# (the same actions as the AWSGrafanaWorkspacePermissionManagementV2 managed policy).
# These actions do not support resource-level permissions.
SSO_ACTIONS = (
    "sso:DescribeRegisteredRegions",
    "sso:GetSharedSsoConfiguration",
    "sso:ListDirectoryAssociations",
    "sso:GetManagedApplicationInstance",
    "sso:ListProfiles",
    "sso:AssociateProfile",
    "sso:DisassociateProfile",
    "sso:GetProfile",
    "sso:ListProfileAssociations",
    "sso-directory:DescribeUser",
    "identitystore:DescribeUser",
)


def workspace_arn(stack: cdk.Stack, workspace_id: str) -> str:
    """``arn:aws:grafana:<region>:<account>:/workspaces/<id>``."""
    return stack.format_arn(service="grafana", resource="/workspaces", resource_name=workspace_id)


class SharedGrafanaStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str = STACK_NAME, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.workspace_role = iam.Role(
            self,
            "WorkspaceRole",
            assumed_by=iam.ServicePrincipal(
                "grafana.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "StringLike": {"aws:SourceArn": workspace_arn(self, "*")},
                },
            ),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(SITEWISE_READ_ONLY)],
            description="Managed Grafana workspace role: SiteWise read-only, nothing else (AD-11)",
        )

        self.workspace = grafana.CfnWorkspace(
            self,
            "Workspace",
            name=WORKSPACE_NAME,
            description="Pipe Integrity Streaming POC: live SiteWise trends per stage",
            grafana_version=GRAFANA_VERSION,
            account_access_type="CURRENT_ACCOUNT",
            authentication_providers=["AWS_SSO"],
            permission_type="SERVICE_MANAGED",
            data_sources=["SITEWISE"],
            plugin_admin_enabled=True,
            role_arn=self.workspace_role.role_arn,
        )

        self.workspace_id = self.workspace.attr_id
        self.workspace_arn = workspace_arn(self, self.workspace.attr_id)
        self.grafana_url = cdk.Fn.join("", ["https://", self.workspace.attr_endpoint])

        users = [{"id": u, "type": "SSO_USER"} for u in ADMIN_USER_IDS]

        def call(action: str) -> cr.AwsSdkCall:
            return cr.AwsSdkCall(
                service="grafana",
                action="UpdatePermissions",
                parameters={
                    "workspaceId": self.workspace.attr_id,
                    "updateInstructionBatch": [{"action": action, "role": "ADMIN", "users": users}],
                },
                physical_resource_id=cr.PhysicalResourceId.of(f"grafana-admins-{WORKSPACE_NAME}"),
            )

        self.admin_assignment = cr.AwsCustomResource(
            self,
            "AdminAssignment",
            resource_type="Custom::GrafanaAdminAssignment",
            on_create=call("ADD"),
            on_update=call("ADD"),
            on_delete=call("REVOKE"),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(actions=["grafana:UpdatePermissions"], resources=[self.workspace_arn]),
                    iam.PolicyStatement(actions=list(SSO_ACTIONS), resources=["*"]),
                ]
            ),
            install_latest_aws_sdk=False,
        )

        cdk.CfnOutput(self, "WorkspaceId", value=self.workspace.attr_id)
        cdk.CfnOutput(self, "GrafanaUrl", value=self.grafana_url)
