"""One-time GitHub Actions OIDC trust for deploys (AD-16).

- A native IAM OIDC provider for GitHub Actions tokens (audience ``sts.amazonaws.com``).
- Role ``ongiiot-github-deploy``: trusted only for jobs of ``schuaschua/ongiiot`` that run
  in the GitHub environments ``poc`` or ``dev``. Its only permissions are assuming the CDK
  bootstrap roles and reading the bootstrap version; all resource changes go through
  those roles.
- Role ``ongiiot-github-diff``: trusted only for ``pull_request`` jobs of the same repo.
  It may assume only the CDK lookup role (read-only), for ``cdk diff --method template``.

Deployed once, locally: ``cdk deploy GitHubOidcStack -c oidc=true --profile ongiiot-poc``.
Stage deploys target ``*-<stage>`` and never include it (it has no stage suffix).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

ACCOUNT = "123456789012"
REGION = "ap-southeast-1"
STACK_NAME = "GitHubOidcStack"
GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
AUDIENCE = "sts.amazonaws.com"
REPO = "schuaschua/ongiiot"
DEPLOY_SUBS = [f"repo:{REPO}:environment:poc", f"repo:{REPO}:environment:dev"]
DIFF_SUBS = [f"repo:{REPO}:pull_request"]
DEPLOY_ROLE_NAME = "ongiiot-github-deploy"
DIFF_ROLE_NAME = "ongiiot-github-diff"
BOOTSTRAP_QUALIFIER = "hnb659fds"
BOOTSTRAP_ROLES_ARN = f"arn:aws:iam::{ACCOUNT}:role/cdk-{BOOTSTRAP_QUALIFIER}-*-{ACCOUNT}-{REGION}"
LOOKUP_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/cdk-{BOOTSTRAP_QUALIFIER}-lookup-role-{ACCOUNT}-{REGION}"
BOOTSTRAP_VERSION_PARAM_ARN = (
    f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/{BOOTSTRAP_QUALIFIER}/version"
)


class GitHubOidcStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str = STACK_NAME, **kwargs) -> None:
        kwargs.setdefault("env", cdk.Environment(account=ACCOUNT, region=REGION))
        super().__init__(scope, construct_id, **kwargs)

        self.provider = iam.OidcProviderNative(
            self, "GitHubOidcProvider", url=GITHUB_OIDC_URL, client_ids=[AUDIENCE]
        )

        self.deploy_role = self._role(
            "DeployRole",
            role_name=DEPLOY_ROLE_NAME,
            subs=DEPLOY_SUBS,
            assumable_roles=BOOTSTRAP_ROLES_ARN,
            description=f"GitHub Actions deploy role for {REPO} (environments poc, dev); assumes CDK bootstrap roles only",
        )
        self.diff_role = self._role(
            "DiffRole",
            role_name=DIFF_ROLE_NAME,
            subs=DIFF_SUBS,
            assumable_roles=LOOKUP_ROLE_ARN,
            description=f"GitHub Actions PR diff role for {REPO}; assumes the CDK lookup role only",
        )

        cdk.CfnOutput(self, "DeployRoleArn", value=self.deploy_role.role_arn)
        cdk.CfnOutput(self, "DiffRoleArn", value=self.diff_role.role_arn)

    def _role(
        self, construct_id: str, *, role_name: str, subs: list[str], assumable_roles: str, description: str
    ) -> iam.Role:
        principal = iam.WebIdentityPrincipal(
            self.provider.oidc_provider_arn,
            conditions={
                "StringEquals": {
                    f"{GITHUB_OIDC_HOST}:aud": AUDIENCE,
                    f"{GITHUB_OIDC_HOST}:sub": list(subs),
                },
            },
        )
        return iam.Role(
            self,
            construct_id,
            role_name=role_name,
            assumed_by=principal,
            description=description,
            max_session_duration=cdk.Duration.hours(1),
            inline_policies={
                "AssumeCdkRoles": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="AssumeCdkRoles",
                            actions=["sts:AssumeRole", "sts:TagSession"],
                            resources=[assumable_roles],
                        ),
                        iam.PolicyStatement(
                            sid="ReadBootstrapVersion",
                            actions=["ssm:GetParameter"],
                            resources=[BOOTSTRAP_VERSION_PARAM_ARN],
                        ),
                    ]
                )
            },
        )
