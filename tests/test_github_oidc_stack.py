"""CDK Template assertions for the one-time GitHub OIDC deploy trust (AD-16)."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.app import build_app
from infra.stacks.github_oidc import GitHubOidcStack

ACCOUNT = "123456789012"
REGION = "ap-southeast-1"
BOOTSTRAP_ROLES = f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-*-{ACCOUNT}-{REGION}"
BOOTSTRAP_VERSION = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/cdk-bootstrap/hnb659fds/version"


@pytest.fixture(scope="module")
def stack_and_tpl():
    stack = GitHubOidcStack(cdk.App())
    return stack, Template.from_stack(stack).to_json()


def of_type(tpl: dict, type_: str) -> list[dict]:
    return [r["Properties"] for r in tpl["Resources"].values() if r["Type"] == type_]


def test_stack_pinned_to_account_and_region(stack_and_tpl):
    stack, _ = stack_and_tpl
    assert stack.stack_name == "GitHubOidcStack"
    assert stack.account == ACCOUNT
    assert stack.region == REGION


def test_native_oidc_provider(stack_and_tpl):
    _, tpl = stack_and_tpl
    (provider,) = of_type(tpl, "AWS::IAM::OIDCProvider")
    assert provider["Url"] == "https://token.actions.githubusercontent.com"
    assert provider["ClientIdList"] == ["sts.amazonaws.com"]
    assert not [r for r in tpl["Resources"].values() if r["Type"].startswith("Custom::")]


BOOTSTRAP_LOOKUP = f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-lookup-role-{ACCOUNT}-{REGION}"
HOST = "token.actions.githubusercontent.com"
ROLES = {
    "ongiiot-github-deploy": {
        "sub": ["repo:schuaschua@83538399/ongiiot@1392670803:environment:poc", "repo:schuaschua@83538399/ongiiot@1392670803:environment:dev"],
        "assume": BOOTSTRAP_ROLES,
    },
    "ongiiot-github-diff": {
        "sub": ["repo:schuaschua@83538399/ongiiot@1392670803:pull_request"],
        "assume": BOOTSTRAP_LOOKUP,
    },
}


def roles_by_name(tpl: dict) -> dict[str, dict]:
    return {r["RoleName"]: r for r in of_type(tpl, "AWS::IAM::Role")}


def test_exactly_two_roles(stack_and_tpl):
    _, tpl = stack_and_tpl
    assert set(roles_by_name(tpl)) == set(ROLES)


@pytest.mark.parametrize("name", sorted(ROLES))
def test_trust_scoped_by_sub(stack_and_tpl, name):
    stack, tpl = stack_and_tpl
    role = roles_by_name(tpl)[name]
    assert role["MaxSessionDuration"] == 3600
    (stmt,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
    provider_id = stack.get_logical_id(stack.provider.node.default_child)
    assert stmt["Principal"] == {"Federated": {"Ref": provider_id}}
    # Exact subjects only: no wildcard, no StringLike, so branch runs, other repos and
    # forks (different sub) are denied, and a PR token cannot assume the deploy role.
    assert stmt["Condition"] == {
        "StringEquals": {f"{HOST}:aud": "sts.amazonaws.com", f"{HOST}:sub": ROLES[name]["sub"]},
    }
    assert not any("*" in sub for sub in ROLES[name]["sub"])


def grants(role: dict) -> list[tuple]:
    (policy,) = role["Policies"]
    return sorted(
        (tuple(sorted([s["Action"]] if isinstance(s["Action"], str) else s["Action"])), s["Resource"], s["Effect"])
        for s in policy["PolicyDocument"]["Statement"]
    )


@pytest.mark.parametrize("name", sorted(ROLES))
def test_policy_exact(stack_and_tpl, name):
    _, tpl = stack_and_tpl
    role = roles_by_name(tpl)[name]
    assert not role.get("ManagedPolicyArns")
    assert not of_type(tpl, "AWS::IAM::Policy")
    assert not of_type(tpl, "AWS::IAM::ManagedPolicy")
    assert grants(role) == [
        (("ssm:GetParameter",), BOOTSTRAP_VERSION, "Allow"),
        (("sts:AssumeRole", "sts:TagSession"), ROLES[name]["assume"], "Allow"),
    ]


def test_diff_role_cannot_reach_other_bootstrap_roles(stack_and_tpl):
    _, tpl = stack_and_tpl
    resources = [r for _, r, _ in grants(roles_by_name(tpl)["ongiiot-github-diff"])]
    assert BOOTSTRAP_ROLES not in resources
    assert not any("*" in r for r in resources)
    for other in ("deploy-role", "cfn-exec-role", "file-publishing-role", "image-publishing-role"):
        assert not any(other in r for r in resources)


def test_role_arn_outputs(stack_and_tpl):
    stack, tpl = stack_and_tpl
    deploy_id = stack.get_logical_id(stack.deploy_role.node.default_child)
    diff_id = stack.get_logical_id(stack.diff_role.node.default_child)
    assert tpl["Outputs"]["DeployRoleArn"]["Value"] == {"Fn::GetAtt": [deploy_id, "Arn"]}
    assert tpl["Outputs"]["DiffRoleArn"]["Value"] == {"Fn::GetAtt": [diff_id, "Arn"]}


def stack_names(app: cdk.App) -> set[str]:
    return {c.stack_name for c in app.node.children if isinstance(c, cdk.Stack)}


def test_absent_without_oidc_context():
    assert stack_names(build_app(cdk.App(context={"stage": "poc"}))) == {
        "SharedNetworkStack",
        "SiteWiseAssetsStack-poc",
        "SimulatorStack-poc",
        "SharedGrafanaStack",
        "GrafanaStack-poc",
    }
    assert "GitHubOidcStack" not in stack_names(build_app(cdk.App(context={"oidc": "false"})))


@pytest.mark.parametrize("flag", ["true", True])
def test_present_with_oidc_context(flag):
    names = stack_names(build_app(cdk.App(context={"stage": "poc-dj", "oidc": flag})))
    assert names == {
        "SharedNetworkStack",
        "SiteWiseAssetsStack-poc-dj",
        "SimulatorStack-poc-dj",
        "SharedGrafanaStack",
        "GrafanaStack-poc-dj",
        "GitHubOidcStack",
    }
