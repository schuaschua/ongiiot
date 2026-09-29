"""CDK Template assertions for the simulator stack (Story 1.4; AD-10, AD-15, AD-17)."""

from __future__ import annotations


import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.app import build_app

ASSUME_TASKS = {
    "Statement": [
        {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}}
    ],
    "Version": "2012-10-17",
}


def synth(stage: str, **context: str):
    app = build_app(cdk.App(context={"stage": stage, **context}))
    sitewise = app.node.find_child(f"SiteWiseAssetsStack-{stage}")
    stack = app.node.find_child(f"SimulatorStack-{stage}")
    return sitewise, stack, Template.from_stack(stack).to_json()


@pytest.fixture(scope="module", params=["poc-dj"])
def synthesized(request):
    return (request.param, *synth(request.param))


def of_type(tpl: dict, type_: str) -> dict[str, dict]:
    return {k: r for k, r in tpl["Resources"].items() if r["Type"] == type_}


def props(tpl: dict, type_: str) -> list[dict]:
    return [r["Properties"] for r in of_type(tpl, type_).values()]


def policy_statements(tpl: dict, role_id: str) -> list[dict]:
    out = []
    for r in of_type(tpl, "AWS::IAM::Policy").values():
        if {"Ref": role_id} in r["Properties"]["Roles"]:
            out += r["Properties"]["PolicyDocument"]["Statement"]
    return out


def as_list(v):
    return v if isinstance(v, list) else [v]


def test_stack_name_and_dependency(synthesized):
    stage, sitewise, stack, _ = synthesized
    assert stack.stack_name == f"SimulatorStack-{stage}"
    assert stack.region == "ap-southeast-1"
    assert sitewise in stack.dependencies


def test_tables(synthesized):
    stage, _, _, tpl = synthesized
    tables = of_type(tpl, "AWS::DynamoDB::Table")
    by_name = {r["Properties"]["TableName"]: r for r in tables.values()}
    assert set(by_name) == {f"sim-control-{stage}", f"injection-log-{stage}"}
    policy = "Retain" if stage == "poc" else "Delete"
    for r in by_name.values():
        p = r["Properties"]
        assert p["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
        assert p["AttributeDefinitions"] == [{"AttributeName": "pk", "AttributeType": "S"}]
        assert p["BillingMode"] == "PAY_PER_REQUEST"
        assert r["DeletionPolicy"] == policy
        assert r["UpdateReplacePolicy"] == policy


def table_id(tpl: dict, name: str) -> str:
    (tid,) = [k for k, r in of_type(tpl, "AWS::DynamoDB::Table").items() if r["Properties"]["TableName"] == name]
    return tid


def test_task_role_is_exactly_ad17(synthesized):
    stage, sitewise, sim, tpl = synthesized
    (td,) = props(tpl, "AWS::ECS::TaskDefinition")
    task_role = td["TaskRoleArn"]["Fn::GetAtt"][0]
    exec_role = td["ExecutionRoleArn"]["Fn::GetAtt"][0]
    assert task_role != exec_role
    role = tpl["Resources"][task_role]["Properties"]
    assert role["AssumeRolePolicyDocument"] == ASSUME_TASKS
    assert "ManagedPolicyArns" not in role and "Policies" not in role

    statements = policy_statements(tpl, task_role)
    assert all(s["Effect"] == "Allow" for s in statements)
    conditioned = [s for s in statements if "Condition" in s]
    assert [as_list(s["Action"]) for s in conditioned] == [["dynamodb:UpdateItem"]]
    assert conditioned[0]["Condition"] == {
        "ForAllValues:StringEquals": {"dynamodb:Attributes": ["pk", "effective_end"]},
        "StringEqualsIfExists": {"dynamodb:ReturnValues": "NONE"},
        "Null": {"dynamodb:Attributes": "false"},
    }
    grants = {tuple(sorted(as_list(s["Action"]))): as_list(s["Resource"]) for s in statements}
    assert len(grants) == len(statements) == 4

    sw = grants[("iotsitewise:BatchPutAssetPropertyValue", "iotsitewise:DescribeTimeSeries")]
    # Cross-stack references to exactly this stage's pipe asset ARNs.
    expected_exports = {
        sitewise.resolve(sitewise.export_value(a.attr_asset_arn))["Fn::ImportValue"]
        for a in sitewise.pipe_assets.values()
    }
    # Plus the time-series resource: alias-addressed calls are authorised against it.
    time_series = [r for r in sw if "Fn::ImportValue" not in r]
    assert time_series == [sim.resolve(sim.format_arn(service="iotsitewise", resource="time-series", resource_name="*"))]
    assert len(sw) == len(sitewise.pipe_assets) + 1
    assert {r["Fn::ImportValue"] for r in sw if "Fn::ImportValue" in r} == expected_exports
    assert all(e.startswith(f"SiteWiseAssetsStack-{stage}:") and "AssetArn" in e for e in expected_exports)

    control = table_id(tpl, f"sim-control-{stage}")
    log = table_id(tpl, f"injection-log-{stage}")
    assert grants[("dynamodb:Scan",)] == [{"Fn::GetAtt": [control, "Arn"]}]
    assert grants[("dynamodb:PutItem",)] == [{"Fn::GetAtt": [log, "Arn"]}]
    assert grants[("dynamodb:UpdateItem",)] == [{"Fn::GetAtt": [log, "Arn"]}]


def test_execution_role_only_pulls_and_logs(synthesized):
    _, _, _, tpl = synthesized
    (td,) = props(tpl, "AWS::ECS::TaskDefinition")
    exec_role = td["ExecutionRoleArn"]["Fn::GetAtt"][0]
    actions = {a for s in policy_statements(tpl, exec_role) for a in as_list(s["Action"])}
    assert actions == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:GetAuthorizationToken",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }


def test_uses_shared_vpc_no_ingress(synthesized):
    _, _, stack, tpl = synthesized
    network = stack.node.scope.node.find_child("SharedNetworkStack")
    assert stack.vpc is network.vpc
    assert network in stack.dependencies
    for type_ in ("AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::NatGateway", "AWS::EC2::InternetGateway"):
        assert not of_type(tpl, type_), type_
    (sg,) = props(tpl, "AWS::EC2::SecurityGroup")
    assert "SecurityGroupIngress" not in sg
    assert not of_type(tpl, "AWS::EC2::SecurityGroupIngress")
    assert "Fn::ImportValue" in sg["VpcId"] and sg["VpcId"]["Fn::ImportValue"].startswith("SharedNetworkStack:")
    (svc,) = props(tpl, "AWS::ECS::Service")
    net = svc["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert net["AssignPublicIp"] == "ENABLED"
    assert len(net["Subnets"]) == 2
    assert all(s["Fn::ImportValue"].startswith("SharedNetworkStack:") for s in net["Subnets"])


def test_service_one_task_with_rollback(synthesized):
    stage, _, _, tpl = synthesized
    (svc,) = props(tpl, "AWS::ECS::Service")
    assert svc["ServiceName"] == f"simulator-{stage}"
    assert svc["LaunchType"] == "FARGATE"
    assert svc["DesiredCount"] == 1
    dc = svc["DeploymentConfiguration"]
    assert dc["DeploymentCircuitBreaker"] == {"Enable": True, "Rollback": True}
    assert dc["MaximumPercent"] == 100 and dc["MinimumHealthyPercent"] == 0
    (cluster,) = props(tpl, "AWS::ECS::Cluster")
    assert cluster["ClusterName"] == f"simulator-{stage}"


def test_simulator_off_only_changes_desired_count():
    _, _, on = synth("poc-dj", simulator="on")
    _, _, off = synth("poc-dj", simulator="off")
    (svc_off,) = props(off, "AWS::ECS::Service")
    assert svc_off["DesiredCount"] == 0
    (svc_on,) = props(on, "AWS::ECS::Service")
    svc_on["DesiredCount"] = 0
    assert on == off


def test_two_stages_do_not_collide():
    _, _, a = synth("poc")
    _, _, b = synth("poc-dj")

    def names(tpl):
        out = {p["TableName"] for p in props(tpl, "AWS::DynamoDB::Table")}
        out |= {p["ClusterName"] for p in props(tpl, "AWS::ECS::Cluster")}
        out |= {p["ServiceName"] for p in props(tpl, "AWS::ECS::Service")}
        out |= {p["Family"] for p in props(tpl, "AWS::ECS::TaskDefinition")}
        out |= {p["LogGroupName"] for p in props(tpl, "AWS::Logs::LogGroup")}
        out |= {p["RoleName"] for p in props(tpl, "AWS::IAM::Role") if "RoleName" in p}
        return out

    assert not names(a) & names(b)
