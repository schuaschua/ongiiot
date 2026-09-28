"""CDK Template assertions for the shared Grafana workspace and per-stage provisioning (Story 1.5)."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from facility.model import DEFAULT_PATH, load
from infra.app import build_app
from infra.stacks.grafana import ADMIN_USER_IDS, SITEWISE_READ_ONLY, SSO_ACTIONS
from infra.stacks.grafana_provision import PLUGIN_VERSION, SERVICE_ACCOUNT_ACTIONS


def of_type(tpl: dict, type_: str) -> dict[str, dict]:
    return {k: r for k, r in tpl["Resources"].items() if r["Type"] == type_}


def as_list(v):
    return v if isinstance(v, list) else [v]


def policy_statements(tpl: dict, role_id: str) -> list[dict]:
    out = []
    for r in of_type(tpl, "AWS::IAM::Policy").values():
        if {"Ref": role_id} in r["Properties"]["Roles"]:
            out += r["Properties"]["PolicyDocument"]["Statement"]
    return out


@pytest.fixture(scope="module", params=["poc", "poc-dj"])
def synthesized(request):
    stage = request.param
    app = build_app(cdk.App(context={"stage": stage}))
    shared = app.node.find_child("SharedGrafanaStack")
    per_stage = app.node.find_child(f"GrafanaStack-{stage}")
    return (
        stage,
        shared,
        Template.from_stack(shared).to_json(),
        per_stage,
        Template.from_stack(per_stage).to_json(),
    )


# -- SharedGrafanaStack ------------------------------------------------------------------------


def test_shared_stack_name_region(synthesized):
    _, shared, _, per_stage, _ = synthesized
    assert shared.stack_name == "SharedGrafanaStack"
    assert shared.region == "ap-southeast-1"
    assert shared in per_stage.dependencies


def test_workspace_settings(synthesized):
    _, _, tpl, _, _ = synthesized
    (ws,) = [dict(r["Properties"]) for r in of_type(tpl, "AWS::Grafana::Workspace").values()]
    role_id = ws.pop("RoleArn")["Fn::GetAtt"][0]
    assert ws == {
        "Name": "ongiiot",
        "Description": ws["Description"],
        "GrafanaVersion": "12.4",
        "AccountAccessType": "CURRENT_ACCOUNT",
        "AuthenticationProviders": ["AWS_SSO"],
        "PermissionType": "SERVICE_MANAGED",
        "DataSources": ["SITEWISE"],
        "PluginAdminEnabled": True,
    }
    assert tpl["Resources"][role_id]["Type"] == "AWS::IAM::Role"


def test_workspace_role_holds_only_sitewise_read_only(synthesized):
    _, _, tpl, _, _ = synthesized
    (ws,) = [r["Properties"] for r in of_type(tpl, "AWS::Grafana::Workspace").values()]
    role_id = ws["RoleArn"]["Fn::GetAtt"][0]
    role = tpl["Resources"][role_id]["Properties"]
    (managed,) = role["ManagedPolicyArns"]
    assert managed["Fn::Join"][1][-1] == f":iam::aws:policy/{SITEWISE_READ_ONLY}"
    assert "Policies" not in role
    assert policy_statements(tpl, role_id) == []
    (trust,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "grafana.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    assert trust["Condition"]["StringLike"] == {
        "aws:SourceArn": {
            "Fn::Join": [
                "",
                ["arn:", {"Ref": "AWS::Partition"}, ":grafana:ap-southeast-1:", {"Ref": "AWS::AccountId"}, ":/workspaces/*"],
            ]
        }
    }
    assert set(trust) == {"Action", "Condition", "Effect", "Principal"} and trust["Effect"] == "Allow"
    assert set(trust["Condition"]) == {"StringEquals", "StringLike"}


def test_admin_assignment_identity_center_actions_exact(synthesized):
    _, _, tpl, _, _ = synthesized
    (policy,) = [r["Properties"] for r in of_type(tpl, "AWS::IAM::Policy").values()]
    statements = policy["PolicyDocument"]["Statement"]
    assert len(statements) == 2
    (sso,) = [s for s in statements if not any(a.startswith("grafana:") for a in as_list(s["Action"]))]
    assert sso == {
        "Action": [
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
        ],
        "Effect": "Allow",
        "Resource": "*",
    }
    assert tuple(sso["Action"]) == SSO_ACTIONS


def test_admin_assignment_for_the_one_user(synthesized):
    _, _, tpl, _, _ = synthesized
    (res,) = of_type(tpl, "Custom::GrafanaAdminAssignment").values()
    for key, action in (("Create", "ADD"), ("Update", "ADD"), ("Delete", "REVOKE")):
        text = "".join(p if isinstance(p, str) else "<ws>" for p in res["Properties"][key]["Fn::Join"][1])
        call = json.loads(text)
        assert call["service"] == "grafana" and call["action"] == "UpdatePermissions"
        assert call["parameters"]["workspaceId"] == "<ws>"
        assert call["parameters"]["updateInstructionBatch"] == [
            {"action": action, "role": "ADMIN", "users": [{"id": u, "type": "SSO_USER"} for u in ADMIN_USER_IDS]}
        ]
    assert ADMIN_USER_IDS == ("00000000-0000-0000-0000-000000000000",)


def test_admin_assignment_grafana_grant_scoped_to_workspace(synthesized):
    _, _, tpl, _, _ = synthesized
    (policy,) = [r["Properties"] for r in of_type(tpl, "AWS::IAM::Policy").values()]
    grafana = [s for s in policy["PolicyDocument"]["Statement"] if any(a.startswith("grafana:") for a in as_list(s["Action"]))]
    (st,) = grafana
    assert as_list(st["Action"]) == ["grafana:UpdatePermissions"]
    assert st["Resource"]["Fn::Join"][1][-2:] == [":/workspaces/", {"Fn::GetAtt": ["Workspace", "Id"]}]


def test_shared_outputs(synthesized):
    _, _, tpl, _, _ = synthesized
    assert {"WorkspaceId", "GrafanaUrl"} <= set(tpl["Outputs"])


# -- GrafanaStack-<stage> ------------------------------------------------------------------------


def test_per_stage_stack_and_names(synthesized):
    stage, _, _, per_stage, tpl = synthesized
    assert per_stage.stack_name == f"GrafanaStack-{stage}"
    names = {r["Properties"]["FunctionName"] for r in of_type(tpl, "AWS::Lambda::Function").values()}
    assert names == {f"grafana-provision-{stage}", f"grafana-provision-provider-{stage}"}
    groups = {r["Properties"]["LogGroupName"] for r in of_type(tpl, "AWS::Logs::LogGroup").values()}
    assert groups == {f"/aws/lambda/grafana-provision-{stage}", f"/aws/lambda/grafana-provision-provider-{stage}"}


def test_provision_resource_properties(synthesized):
    stage, _, _, _, tpl = synthesized
    (res,) = of_type(tpl, "Custom::GrafanaProvision").values()
    p = res["Properties"]
    suffix = "" if stage == "poc" else "-dj"
    facility = load(DEFAULT_PATH, stage=stage)
    assert p["Stage"] == stage
    assert p["UidSuffix"] == suffix
    assert p["FolderUid"] == f"ongiiot-{stage}"
    assert p["DataSourceUid"] == f"sitewise-{stage}"
    assert p["Region"] == "ap-southeast-1"
    assert p["AliasRoot"] == facility.alias_root
    assert p["PluginVersion"] == PLUGIN_VERSION
    assert json.loads(p["Pipes"]) == [{"id": x.id, "area": x.area} for x in facility.pipes]
    assert set(json.loads(p["Dashboards"])) == {"pipe-detail", "facility-overview"}
    assert "Fn::ImportValue" in json.dumps(p["WorkspaceId"])


def test_provision_lambda_role_only_service_account_actions_on_workspace(synthesized):
    _, shared, _, per_stage, tpl = synthesized
    fn_id = per_stage.get_logical_id(per_stage.provision.function.node.default_child)
    role_id = tpl["Resources"][fn_id]["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = policy_statements(tpl, role_id)
    (st,) = statements
    assert st["Effect"] == "Allow" and "Condition" not in st
    assert sorted(as_list(st["Action"])) == sorted(SERVICE_ACCOUNT_ACTIONS)
    parts = st["Resource"]["Fn::Join"][1]
    assert ":/workspaces/" in parts
    assert parts[-1]["Fn::ImportValue"].startswith("SharedGrafanaStack:")
    role = tpl["Resources"][role_id]["Properties"]
    managed = json.dumps(role.get("ManagedPolicyArns", []))
    assert "AWSLambdaBasicExecutionRole" in managed and managed.count("arn:") == 1


def test_per_stage_outputs(synthesized):
    stage, _, _, _, tpl = synthesized
    suffix = "" if stage == "poc" else "-dj"
    out = tpl["Outputs"]
    assert out["PipeDetailUid"]["Value"] == f"pipe-detail{suffix}"
    assert out["FacilityOverviewUid"]["Value"] == f"facility-overview{suffix}"
    assert out["FolderUid"]["Value"] == f"ongiiot-{stage}"
    assert out["DataSourceUid"]["Value"] == f"sitewise-{stage}"
    assert "SharedGrafanaStack:" in json.dumps(out["GrafanaUrl"]["Value"])
    assert "SiteWisePluginVersion" in out


def test_two_stages_do_not_collide():
    def props(stage):
        app = build_app(cdk.App(context={"stage": stage}))
        tpl = Template.from_stack(app.node.find_child(f"GrafanaStack-{stage}")).to_json()
        (res,) = of_type(tpl, "Custom::GrafanaProvision").values()
        return res["Properties"]

    a, b = props("poc"), props("poc-dj")
    for key in ("FolderUid", "DataSourceUid", "UidSuffix", "AliasRoot"):
        assert a[key] != b[key], key
