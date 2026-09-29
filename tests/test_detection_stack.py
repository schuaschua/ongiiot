"""CDK Template assertions for the detection stack (Story 2.1; AD-12, AD-15, AD-17, AD-18)."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.app import build_app
from infra.stacks.detection import INCIDENT_ACTIONS, SITEWISE_READ_ACTIONS


def synth(stage: str, **context: str):
    app = build_app(cdk.App(context={"stage": stage, **context}))
    sitewise = app.node.find_child(f"SiteWiseAssetsStack-{stage}")
    stack = app.node.find_child(f"DetectionStack-{stage}")
    return sitewise, stack, Template.from_stack(stack).to_json()


@pytest.fixture(scope="module", params=["poc-dj"])
def synthesized(request):
    return (request.param, *synth(request.param))


def of_type(tpl: dict, type_: str) -> dict[str, dict]:
    return {k: r for k, r in tpl["Resources"].items() if r["Type"] == type_}


def props(tpl: dict, type_: str) -> list[dict]:
    return [r["Properties"] for r in of_type(tpl, type_).values()]


def as_list(v):
    return v if isinstance(v, list) else [v]


def policy_statements(tpl: dict, role_id: str) -> list[dict]:
    out = []
    for r in of_type(tpl, "AWS::IAM::Policy").values():
        if {"Ref": role_id} in r["Properties"]["Roles"]:
            out += r["Properties"]["PolicyDocument"]["Statement"]
    return out


def function_id(tpl: dict) -> str:
    (fid,) = of_type(tpl, "AWS::Lambda::Function")
    return fid


def table_id(tpl: dict) -> str:
    (tid,) = of_type(tpl, "AWS::DynamoDB::Table")
    return tid


def topic_id(tpl: dict, prefix: str = "operators-") -> str:
    (tid,) = [k for k, r in of_type(tpl, "AWS::SNS::Topic").items() if r["Properties"]["TopicName"].startswith(prefix)]
    return tid


def topic_policy(tpl: dict, tid: str) -> list[dict]:
    (policy,) = [p for p in props(tpl, "AWS::SNS::TopicPolicy") if p["Topics"] == [{"Ref": tid}]]
    return policy["PolicyDocument"]["Statement"]


def alarms(tpl: dict) -> dict[str, dict]:
    return {p["AlarmName"]: p for p in props(tpl, "AWS::CloudWatch::Alarm")}


def test_incidents_table(synthesized):
    stage, _, _, tpl = synthesized
    tid = table_id(tpl)
    t = tpl["Resources"][tid]
    p = t["Properties"]
    assert p["TableName"] == f"incidents-{stage}"
    assert p["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]
    assert p["AttributeDefinitions"] == [{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}]
    assert p["BillingMode"] == "PAY_PER_REQUEST"
    assert "StreamSpecification" not in p  # AD-7: no stream on incidents
    pitr = p.get("PointInTimeRecoverySpecification", {}).get("PointInTimeRecoveryEnabled", False)
    assert pitr is (stage == "poc")
    policy = "Retain" if stage == "poc" else "Delete"
    assert t["DeletionPolicy"] == t["UpdateReplacePolicy"] == policy
    assert tpl["Outputs"]["IncidentsTableName"]["Value"] == {"Ref": tid}


def test_function_role_is_exactly_ad17(synthesized):
    stage, sitewise, stack, tpl = synthesized
    (fn,) = props(tpl, "AWS::Lambda::Function")
    role_id = fn["Role"]["Fn::GetAtt"][0]
    role = tpl["Resources"][role_id]["Properties"]
    assert role["RoleName"] == f"detection-{stage}"
    assert role["AssumeRolePolicyDocument"]["Statement"] == [
        {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}}
    ]
    assert "ManagedPolicyArns" not in role and "Policies" not in role

    statements = policy_statements(tpl, role_id)
    assert all(s["Effect"] == "Allow" for s in statements)
    assert [s["Condition"] for s in statements if "Condition" in s] == [
        {"ForAllValues:StringEquals": {"dynamodb:Attributes": ["pk", "sk", "incident_id", "notified_at"]}}
    ]
    grants = {tuple(sorted(as_list(s["Action"]))): as_list(s["Resource"]) for s in statements}
    assert len(grants) == len(statements) == 6

    assert grants[tuple(sorted(SITEWISE_READ_ACTIONS))] == ["*"]
    assert set(SITEWISE_READ_ACTIONS) == {
        "iotsitewise:ExecuteQuery",
        "iotsitewise:BatchGetAssetPropertyValueHistory",
        "iotsitewise:Describe*",
        "iotsitewise:List*",
    }

    put = grants[("iotsitewise:BatchPutAssetPropertyValue",)]
    expected_exports = {
        sitewise.resolve(sitewise.export_value(a.attr_asset_arn))["Fn::ImportValue"]
        for a in sitewise.pipe_assets.values()
    }
    time_series = [r for r in put if "Fn::ImportValue" not in r]
    assert time_series == [stack.resolve(stack.format_arn(service="iotsitewise", resource="time-series", resource_name="*"))]
    assert len(put) == len(sitewise.pipe_assets) + 1
    assert {r["Fn::ImportValue"] for r in put if "Fn::ImportValue" in r} == expected_exports

    (lg_id,) = of_type(tpl, "AWS::Logs::LogGroup")
    assert grants[("logs:CreateLogStream", "logs:PutLogEvents")] == [{"Fn::GetAtt": [lg_id, "Arn"]}]

    # AD-6/AD-17: the incidents table only, exactly these actions (no Scan, no BatchWrite, no other table).
    table = [{"Fn::GetAtt": [table_id(tpl), "Arn"]}]
    assert grants[tuple(sorted(INCIDENT_ACTIONS))] == table
    assert sorted(INCIDENT_ACTIONS) == sorted(
        ["dynamodb:Query", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:TransactWriteItems"]
    )
    # AD-7: UpdateItem only for the notified_at marker, in its own conditioned statement.
    (upd,) = [s for s in statements if s["Action"] == "dynamodb:UpdateItem"]
    assert as_list(upd["Resource"]) == table

    # AD-7: publish on this stage's operator topic only.
    assert grants[("sns:Publish",)] == [{"Ref": topic_id(tpl)}]

    # AD-4: nothing on sim_control, injection_log or any other service.
    actions = {a for s in statements for a in as_list(s["Action"])}
    dynamo = [s for s in statements if any(a.startswith("dynamodb:") for a in as_list(s["Action"]))]
    assert all(as_list(s["Resource"]) == table for s in dynamo) and len(dynamo) == 2
    assert {a.split(":")[0] for a in actions} == {"iotsitewise", "logs", "dynamodb", "sns"}


def test_scheduler_role_only_invokes_the_function(synthesized):
    stage, _, stack, tpl = synthesized
    (s,) = props(tpl, "AWS::Scheduler::Schedule")
    role_id = s["Target"]["RoleArn"]["Fn::GetAtt"][0]
    role = tpl["Resources"][role_id]["Properties"]
    assert role["RoleName"] == f"detection-scheduler-{stage}"
    (trust,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert trust["Condition"] == {"StringEquals": {"aws:SourceAccount": stack.resolve(stack.account)}}
    assert "ManagedPolicyArns" not in role
    (stmt,) = policy_statements(tpl, role_id)
    assert stmt["Action"] == "lambda:InvokeFunction"
    assert as_list(stmt["Resource"]) == [{"Fn::GetAtt": [function_id(tpl), "Arn"]}]
    # The invoke grant exists before the schedule can fire.
    (sid,) = of_type(tpl, "AWS::Scheduler::Schedule")
    policy_ids = [k for k, r in of_type(tpl, "AWS::IAM::Policy").items() if {"Ref": role_id} in r["Properties"]["Roles"]]
    assert set(policy_ids) <= set(tpl["Resources"][sid]["DependsOn"])


def test_switch_off_disables_only_the_schedule():
    _, _, on = synth("poc-dj", simulator="on")
    _, _, off = synth("poc-dj", simulator="off")
    (s_off,) = props(off, "AWS::Scheduler::Schedule")
    assert s_off["State"] == "DISABLED"
    (s_on,) = props(on, "AWS::Scheduler::Schedule")
    s_on["State"] = "DISABLED"
    on["Outputs"]["ScheduleState"]["Value"] = "DISABLED"
    # AD-15: a deliberate stop does not page the team; the errors alarm stays enabled.
    for name in ("simulator-down-poc-dj", "detection-stalled-poc-dj"):
        assert alarms(off)[name]["ActionsEnabled"] is False
        assert alarms(on)[name]["ActionsEnabled"] is True
        alarms(on)[name]["ActionsEnabled"] = False
    assert "ActionsEnabled" not in alarms(off)["detection-errors-poc-dj"]
    assert on == off


# -- Story 3.4: operator topic (AD-7) ---------------------------------------------------------------


def deny_subscribe_ok(stack, stmt: dict, tid: str) -> bool:
    assert stmt["Effect"] == "Deny" and stmt["Action"] == "sns:Subscribe" and stmt["Principal"] == {"AWS": "*"}
    assert stmt["Resource"] == {"Ref": tid}
    assert stmt["Condition"]["ArnNotLike"] == {
        "aws:PrincipalArn": stack.resolve(
            f"arn:{stack.partition}:iam::{stack.account}:role/cdk-hnb659fds-cfn-exec-role-{stack.account}-ap-southeast-1"
        )
    }
    return True


def test_operator_topic_without_subscriptions_by_default(synthesized):
    stage, _, stack, tpl = synthesized
    assert sorted(p["TopicName"] for p in props(tpl, "AWS::SNS::Topic")) == [f"operators-{stage}", f"team-alarms-{stage}"]
    assert of_type(tpl, "AWS::SNS::Subscription") == {}
    assert tpl["Outputs"]["OperatorTopicArn"]["Value"] == {"Ref": topic_id(tpl)}
    (stmt,) = topic_policy(tpl, topic_id(tpl))
    assert deny_subscribe_ok(stack, stmt, topic_id(tpl))


def test_operator_topic_two_addresses():
    _, _, tpl = synth("poc-dj", operator_emails=" a@example.com, b@example.org ,a@example.com,")
    subs = sorted(props(tpl, "AWS::SNS::Subscription"), key=lambda p: p["Endpoint"])
    assert [(s["Protocol"], s["Endpoint"]) for s in subs] == [("email", "a@example.com"), ("email", "b@example.org")]
    assert all(s["TopicArn"] == {"Ref": topic_id(tpl)} for s in subs)
    assert len(props(tpl, "AWS::SNS::TopicPolicy")) == 2


# -- Story 4.4: health alarms (AD-15) ---------------------------------------------------------------


def test_health_alarms_page_the_team_topic_only(synthesized):
    stage, _, stack, tpl = synthesized
    team = {"Ref": topic_id(tpl, "team-alarms-")}
    common = {"Threshold": 1, "AlarmActions": [team], "OKActions": [team]}
    assert alarms(tpl) == {
        f"detection-errors-{stage}": {
            **common,
            "AlarmName": f"detection-errors-{stage}",
            "AlarmDescription": f"Detection Lambda detection-{stage} reported at least one error in 5 minutes",
            "Namespace": "AWS/Lambda",
            "MetricName": "Errors",
            "Dimensions": [{"Name": "FunctionName", "Value": f"detection-{stage}"}],
            "Statistic": "Sum",
            "Period": 300,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "EvaluationPeriods": 1,
            "DatapointsToAlarm": 1,
            "TreatMissingData": "notBreaching",
        },
        f"simulator-down-{stage}": {
            **common,
            "AlarmName": f"simulator-down-{stage}",
            "AlarmDescription": f"Simulator service simulator-{stage} has had no running task for 3 minutes",
            "ActionsEnabled": True,
            "Namespace": "ECS/ContainerInsights",
            "MetricName": "RunningTaskCount",
            "Dimensions": [
                {"Name": "ClusterName", "Value": f"simulator-{stage}"},
                {"Name": "ServiceName", "Value": f"simulator-{stage}"},
            ],
            "Statistic": "Minimum",
            "Period": 60,
            "ComparisonOperator": "LessThanThreshold",
            "EvaluationPeriods": 3,
            "DatapointsToAlarm": 3,
            "TreatMissingData": "breaching",
        },
        f"detection-stalled-{stage}": {
            **common,
            "AlarmName": f"detection-stalled-{stage}",
            "AlarmDescription": f"Detection Lambda detection-{stage} was not invoked in 5 minutes (schedule stopped)",
            "ActionsEnabled": True,
            "Namespace": "AWS/Lambda",
            "MetricName": "Invocations",
            "Dimensions": [{"Name": "FunctionName", "Value": f"detection-{stage}"}],
            "Statistic": "Sum",
            "Period": 300,
            "ComparisonOperator": "LessThanThreshold",
            "EvaluationPeriods": 1,
            "DatapointsToAlarm": 1,
            "TreatMissingData": "breaching",
        },
    }
    # The dimensions are the names SimulatorStack actually synthesizes.
    sim_tpl = Template.from_stack(stack.node.scope.node.find_child(f"SimulatorStack-{stage}")).to_json()
    (cluster,) = props(sim_tpl, "AWS::ECS::Cluster")
    (service,) = props(sim_tpl, "AWS::ECS::Service")
    assert alarms(tpl)[f"simulator-down-{stage}"]["Dimensions"] == [
        {"Name": "ClusterName", "Value": cluster["ClusterName"]},
        {"Name": "ServiceName", "Value": service["ServiceName"]},
    ]
    # Never the operator topic.
    assert f'"{topic_id(tpl)}"' not in json.dumps(alarms(tpl))
    assert tpl["Outputs"]["TeamAlarmsTopicArn"]["Value"] == team

    deny, allow = topic_policy(tpl, team["Ref"])
    assert deny_subscribe_ok(stack, deny, team["Ref"])
    assert allow["Effect"] == "Allow" and allow["Action"] == "sns:Publish" and allow["Resource"] == team
    assert allow["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert allow["Condition"] == {
        "ArnLike": {"aws:SourceArn": stack.resolve(f"arn:{stack.partition}:cloudwatch:ap-southeast-1:{stack.account}:alarm:*")},
        "StringEquals": {"aws:SourceAccount": stack.resolve(stack.account)},
    }


def test_team_emails_subscribe_the_team_topic_only():
    _, _, tpl = synth("poc-dj", team_emails="a@x.io, A@x.io,b@y.io")
    subs = sorted(props(tpl, "AWS::SNS::Subscription"), key=lambda p: p["Endpoint"])
    assert [(s["Protocol"], s["Endpoint"]) for s in subs] == [("email", "a@x.io"), ("email", "b@y.io")]
    assert all(s["TopicArn"] == {"Ref": topic_id(tpl, "team-alarms-")} for s in subs)
    with pytest.raises(ValueError, match="team_emails: not an email address"):
        synth("poc-dj", team_emails="a@x.io,not-an-address")
