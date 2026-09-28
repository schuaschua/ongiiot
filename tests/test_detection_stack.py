"""CDK Template assertions for the detection stack (Story 2.1; AD-12, AD-17, AD-18)."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.app import build_app
from infra.stacks import detection, simulator
from infra.stacks.detection import INCIDENT_ACTIONS, SITEWISE_READ_ACTIONS, operator_emails, schedule_state


def synth(stage: str, **context: str):
    app = build_app(cdk.App(context={"stage": stage, **context}))
    sitewise = app.node.find_child(f"SiteWiseAssetsStack-{stage}")
    stack = app.node.find_child(f"DetectionStack-{stage}")
    return sitewise, stack, Template.from_stack(stack).to_json()


@pytest.fixture(scope="module", params=["poc", "poc-dj"])
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


def topic_id(tpl: dict) -> str:
    (tid,) = of_type(tpl, "AWS::SNS::Topic")
    return tid


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


def test_stack_name_and_dependency(synthesized):
    stage, sitewise, stack, _ = synthesized
    assert stack.stack_name == f"DetectionStack-{stage}"
    assert stack.region == "ap-southeast-1"
    assert sitewise in stack.dependencies
    assert stack.node.scope.node.find_child("SharedGrafanaStack") in stack.dependencies


def test_function_single_flight(synthesized):
    stage, _, _, tpl = synthesized
    (fn,) = props(tpl, "AWS::Lambda::Function")
    assert fn["FunctionName"] == f"detection-{stage}"
    assert fn["PackageType"] == "Image" and fn["Architectures"] == ["x86_64"]
    assert fn["Timeout"] == 50
    assert fn["ReservedConcurrentExecutions"] == 1
    assert fn["Environment"]["Variables"] == {
        "STAGE": stage,
        "READER_STRATEGY": "batch_get",
        "INCIDENTS_TABLE": {"Ref": table_id(tpl)},
        "OPERATOR_TOPIC_ARN": {"Ref": topic_id(tpl)},
        "GRAFANA_URL": fn["Environment"]["Variables"]["GRAFANA_URL"],
        "DASHBOARD_UID": "pipe-detail" if stage == "poc" else "pipe-detail-" + stage.removeprefix("poc-"),
    }
    url = fn["Environment"]["Variables"]["GRAFANA_URL"]  # SharedGrafanaStack's workspace URL
    assert url["Fn::Join"][1][0] == "https://" and "SharedGrafanaStack" in url["Fn::Join"][1][1]["Fn::ImportValue"]
    (cfg,) = props(tpl, "AWS::Lambda::EventInvokeConfig")
    assert cfg == {
        "FunctionName": {"Ref": function_id(tpl)},
        "MaximumEventAgeInSeconds": 60,
        "MaximumRetryAttempts": 0,
        "Qualifier": "$LATEST",
    }


def test_log_group(synthesized):
    stage, _, _, tpl = synthesized
    (lg,) = props(tpl, "AWS::Logs::LogGroup")
    assert lg == {"LogGroupName": f"/aws/lambda/detection-{stage}", "RetentionInDays": 7}
    (fn,) = props(tpl, "AWS::Lambda::Function")
    (lg_id,) = of_type(tpl, "AWS::Logs::LogGroup")
    assert fn["LoggingConfig"] == {"LogGroup": {"Ref": lg_id}}


def test_schedule_single_flight(synthesized):
    stage, _, _, tpl = synthesized
    (s,) = props(tpl, "AWS::Scheduler::Schedule")
    assert s["Name"] == f"detection-{stage}"
    assert s["ScheduleExpression"] == "rate(1 minute)"
    assert s["ScheduleExpressionTimezone"] == "UTC"
    assert s["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert s["State"] == "ENABLED"
    target = s["Target"]
    assert target["Arn"] == {"Fn::GetAtt": [function_id(tpl), "Arn"]}
    assert json.loads(target["Input"]) == {"scheduled_time": "<aws.scheduler.scheduled-time>"}
    assert target["RetryPolicy"] == {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60}


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


def test_only_two_roles(synthesized):
    _, _, _, tpl = synthesized
    assert len(of_type(tpl, "AWS::IAM::Role")) == 2


def test_switch_off_disables_only_the_schedule():
    _, _, on = synth("poc-dj", simulator="on")
    _, _, off = synth("poc-dj", simulator="off")
    (s_off,) = props(off, "AWS::Scheduler::Schedule")
    assert s_off["State"] == "DISABLED"
    (s_on,) = props(on, "AWS::Scheduler::Schedule")
    s_on["State"] = "DISABLED"
    on["Outputs"]["ScheduleState"]["Value"] = "DISABLED"
    assert on == off


def test_switch_default_is_on():
    _, _, default = synth("poc-dj")
    _, _, on = synth("poc-dj", simulator="on")
    assert default == on


@pytest.mark.parametrize("value", ["maybe", "ON", "true", "", "1"])
def test_bad_switch_fails_synth(value):
    with pytest.raises(ValueError, match="simulator"):
        build_app(cdk.App(context={"stage": "poc-dj", "simulator": value}))


@pytest.mark.parametrize(("value", "state"), [(None, "ENABLED"), ("on", "ENABLED"), ("off", "DISABLED")])
def test_schedule_state(value, state):
    assert schedule_state(value) == state


def test_images_exclude_each_other():
    # A detection change must never rebuild (and restart) the simulator, and vice versa.
    assert simulator.IMAGE_EXCLUDE == ["detect"]
    assert detection.IMAGE_EXCLUDE == ["sim"]
    assert detection.DOCKERFILE == "detect/Dockerfile"


def test_two_stages_do_not_collide():
    _, _, a = synth("poc")
    _, _, b = synth("poc-dj")

    def names(tpl):
        out = {p["FunctionName"] for p in props(tpl, "AWS::Lambda::Function")}
        out |= {p["Name"] for p in props(tpl, "AWS::Scheduler::Schedule")}
        out |= {p["LogGroupName"] for p in props(tpl, "AWS::Logs::LogGroup")}
        out |= {p["RoleName"] for p in props(tpl, "AWS::IAM::Role")}
        out |= {p["TableName"] for p in props(tpl, "AWS::DynamoDB::Table")}
        out |= {p["TopicName"] for p in props(tpl, "AWS::SNS::Topic")}
        return out

    assert len(names(a)) == 5 and not names(a) & names(b)


def _image_hashes(monkeypatch, root) -> tuple[str, str]:
    """(simulator image hash, detection image hash) for an app built from ``root``."""
    import re

    monkeypatch.setattr(simulator, "REPO_ROOT", root)
    monkeypatch.setattr(detection, "REPO_ROOT", root)
    app = build_app(cdk.App(context={"stage": "poc-dj"}))

    def image_hash(stack_name: str) -> str:
        tpl = json.dumps(Template.from_stack(app.node.find_child(stack_name)).to_json())
        (h,) = set(re.findall(r":([0-9a-f]{64})", tpl))
        return h

    return image_hash("SimulatorStack-poc-dj"), image_hash("DetectionStack-poc-dj")


def test_image_hashes_are_independent(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    src = Path(detection.__file__).resolve().parent.parent.parent
    root = tmp_path / "repo"
    root.mkdir()
    for f in ("pyproject.toml", "uv.lock", ".dockerignore"):
        shutil.copy(src / f, root / f)
    for d in ("facility", "sim", "detect"):
        shutil.copytree(src / d, root / d, ignore=shutil.ignore_patterns("__pycache__"))

    sim0, det0 = _image_hashes(monkeypatch, root)
    (root / "detect" / "handler.py").write_text((root / "detect" / "handler.py").read_text() + "\n# change\n")
    sim1, det1 = _image_hashes(monkeypatch, root)
    assert sim1 == sim0 and det1 != det0
    (root / "sim" / "main.py").write_text((root / "sim" / "main.py").read_text() + "\n# change\n")
    sim2, det2 = _image_hashes(monkeypatch, root)
    assert sim2 != sim1 and det2 == det1


# -- Story 3.4: operator topic (AD-7) ---------------------------------------------------------------


def test_operator_topic_without_subscriptions_by_default(synthesized):
    stage, _, stack, tpl = synthesized
    (topic,) = props(tpl, "AWS::SNS::Topic")
    assert topic["TopicName"] == f"operators-{stage}"
    assert of_type(tpl, "AWS::SNS::Subscription") == {}
    assert tpl["Outputs"]["OperatorTopicArn"]["Value"] == {"Ref": topic_id(tpl)}
    (policy,) = props(tpl, "AWS::SNS::TopicPolicy")
    assert policy["Topics"] == [{"Ref": topic_id(tpl)}]
    (stmt,) = policy["PolicyDocument"]["Statement"]
    assert stmt["Effect"] == "Deny" and stmt["Action"] == "sns:Subscribe" and stmt["Principal"] == {"AWS": "*"}
    assert stmt["Resource"] == {"Ref": topic_id(tpl)}
    assert stmt["Condition"]["ArnNotLike"] == {
        "aws:PrincipalArn": stack.resolve(
            f"arn:{stack.partition}:iam::{stack.account}:role/cdk-hnb659fds-cfn-exec-role-{stack.account}-ap-southeast-1"
        )
    }


def test_operator_topic_two_addresses():
    _, _, tpl = synth("poc-dj", operator_emails=" a@example.com, b@example.org ,a@example.com,")
    subs = sorted(props(tpl, "AWS::SNS::Subscription"), key=lambda p: p["Endpoint"])
    assert [(s["Protocol"], s["Endpoint"]) for s in subs] == [("email", "a@example.com"), ("email", "b@example.org")]
    assert all(s["TopicArn"] == {"Ref": topic_id(tpl)} for s in subs)
    assert len(props(tpl, "AWS::SNS::TopicPolicy")) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, []), ("", []), (" , ", []), ("a@x.io", ["a@x.io"]), ("a@x.io,b@y.io,a@x.io", ["a@x.io", "b@y.io"]), ("A@X.io, a@x.IO", ["A@X.io"])],
)
def test_operator_emails_parsing(value, expected):
    assert operator_emails(value) == expected


@pytest.mark.parametrize("value", ["not-an-email", "a@b", "a b@x.io", ["a@x.io"]])
def test_bad_operator_emails_fail_synth(value):
    with pytest.raises(ValueError, match="operator_emails"):
        operator_emails(value)
