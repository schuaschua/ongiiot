"""Detection stack: the 1-minute detection run, per stage (Story 2.1; AD-12, AD-13, AD-17, AD-18).

- ``detection-<stage>`` Lambda from ``detect/Dockerfile`` (container image built from ``uv.lock``,
  linux/amd64): timeout 50 s, reserved concurrency 1, async retries 0, max event age 60 s (AD-12).
- EventBridge Scheduler ``detection-<stage>``: ``rate(1 minute)``, flexible window off, retries 0,
  input ``{"scheduled_time": "<aws.scheduler.scheduled-time>"}``, with its own invoke role.
  CDK context ``simulator`` (``on`` default, or ``off``) sets it ENABLED or DISABLED, parsed exactly
  like the simulator's desired count, so the switch pauses detection too.
- The function role holds exactly the AD-17 detection grants for this story: SiteWise read, and
  ``BatchPutAssetPropertyValue`` on the stage's pipe assets plus ``time-series/*`` (alias-addressed
  writes are authorised against the time series; IAM cannot scope by alias, so ``derived_writer.py``
  is the raw/derived boundary). Nothing on ``sim_control`` or ``injection_log`` (AD-4).
- ``incidents-<stage>`` table (Story 3.3; AD-6): keys ``pk``/``sk`` (S), on demand, retained for ``poc``,
  destroyed for developer stages, point-in-time recovery on ``poc`` only. The function role gets exactly
  ``INCIDENT_ACTIONS`` (Query, PutItem, DeleteItem, TransactWriteItems) in one statement on that table; its
  name reaches the Lambda as ``INCIDENTS_TABLE``.
  The simulator role gets nothing on it (AD-17). ``UpdateItem`` (the ``notified_at`` outbox marker, Story 3.4) is a separate statement whose
  ``dynamodb:Attributes`` condition allows only ``pk``, ``sk``, ``incident_id`` and ``notified_at``.
- ``operators-<stage>`` SNS topic (Story 3.4; AD-7): one email subscription per address in the CDK context
  ``operator_emails`` (comma-separated, empty by default, so no subscription). The topic policy denies
  ``sns:Subscribe`` to every principal except the CDK CloudFormation execution role (the deploy path). The
  function role gets ``sns:Publish`` on this topic only; ``OPERATOR_TOPIC_ARN`` reaches the Lambda.
- ``GRAFANA_URL`` (``SharedGrafanaStack``'s workspace URL, a cross-stack reference) and ``DASHBOARD_UID``
  (``pipe-detail`` for ``poc``, ``pipe-detail-<initials>`` otherwise, the same derivation as
  ``grafana_provision``) reach the Lambda for the Deep link v1 (AD-8).
- Health (Story 4.4; AD-15): ``team-alarms-<stage>`` SNS topic, email subscriptions only from the CDK context
  ``team_emails`` (parsed like ``operator_emails``), the same deny-Subscribe policy. Two alarms send ALARM and
  OK to it, never to the operator topic: ``detection-errors-<stage>`` (Lambda ``Errors`` Sum >= 1 over 5 min,
  missing data not breaching) and ``simulator-down-<stage>`` (Container Insights ``RunningTaskCount`` Minimum < 1 for
  3 x 60 s on the deterministic ``simulator-<stage>`` cluster and service, missing data breaching). With
  ``-c simulator=off`` the simulator alarm's actions are disabled.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from detect.adapters.reader import DEFAULT_STRATEGY
from facility.model import Facility
from infra.stacks.github_oidc import BOOTSTRAP_QUALIFIER
from infra.stacks.grafana_provision import dashboard_uid
from infra.stacks.simulator import SWITCH_CONTEXT, desired_count

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = "detect/Dockerfile"
# Build-context paths only the other image needs: kept out of this asset's fingerprint.
IMAGE_EXCLUDE = ["sim"]
TIMEOUT_S = 50
MEMORY_MB = 1024
MAX_EVENT_AGE_S = 60
SCHEDULE = "rate(1 minute)"
SCHEDULER_INPUT = json.dumps({"scheduled_time": "<aws.scheduler.scheduled-time>"})
SITEWISE_READ_ACTIONS = [
    "iotsitewise:ExecuteQuery",
    "iotsitewise:BatchGetAssetPropertyValueHistory",
    "iotsitewise:Describe*",
    "iotsitewise:List*",
]

# Exactly what detect/adapters/incidents.py calls (one statement, the incidents table only).
INCIDENT_ACTIONS = [
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:DeleteItem",
    "dynamodb:TransactWriteItems",
]
# The notified_at outbox marker (AD-7): its own statement, limited to these attributes.
NOTIFY_ACTIONS = ["dynamodb:UpdateItem"]
NOTIFY_ATTRIBUTES = ["pk", "sk", "incident_id", "notified_at"]

EMAILS_CONTEXT = "operator_emails"
TEAM_EMAILS_CONTEXT = "team_emails"
_EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
# The CDK bootstrap's CloudFormation execution role, the only principal allowed to subscribe (the deploy path).
DEPLOY_ROLE_PREFIX = f"cdk-{BOOTSTRAP_QUALIFIER}-cfn-exec-role-"


def emails(value: object, context_name: str) -> list[str]:
    """An email-list context (``context_name``): a comma-separated list (absent or empty: none). Duplicates
    dropped, order kept (case-insensitive: the first spelling wins); a malformed address fails synth."""
    if value is None:
        return []
    if not isinstance(value, str):
        raise ValueError(f"{context_name} must be a comma-separated string, got {value!r}")
    out: list[str] = []
    for raw in value.split(","):
        addr = raw.strip()
        if not addr:
            continue
        if not _EMAIL.match(addr):
            raise ValueError(f"{context_name}: not an email address: {addr!r}")
        if addr.lower() not in {a.lower() for a in out}:
            out.append(addr)
    return out


def operator_emails(value: object) -> list[str]:
    """The ``operator_emails`` context (see ``emails``)."""
    return emails(value, EMAILS_CONTEXT)


def schedule_state(value: object) -> str:
    """``ENABLED`` or ``DISABLED`` from the ``simulator`` context value (absent means ``on``)."""
    return "ENABLED" if desired_count(value) else "DISABLED"


class DetectionStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        facility: Facility,
        pipe_asset_arns: Sequence[str],
        grafana_url: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        stage = facility.stage
        state = schedule_state(self.node.try_get_context(SWITCH_CONTEXT))
        name = f"detection-{stage}"

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/lambda/{name}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        removal = cdk.RemovalPolicy.RETAIN if stage == "poc" else cdk.RemovalPolicy.DESTROY
        self.incidents_table = dynamodb.Table(
            self,
            "IncidentsTable",
            table_name=f"incidents-{stage}",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=stage == "poc"
            ),
        )

        # AD-7: the operator topic; subscriptions only from the allowlist, Subscribe denied to all but the deploy role.
        self.operator_topic = self._email_topic(
            "OperatorTopic", f"operators-{stage}", f"POC pipe incidents ({stage})", EMAILS_CONTEXT
        )

        # AD-17: exactly these grants, nothing else.
        self.role = iam.Role(
            self,
            "FunctionRole",
            role_name=name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"Detection run ({stage}): SiteWise read, derived writes, incidents table, operator topic, own logs",
        )
        self.role.add_to_policy(iam.PolicyStatement(actions=SITEWISE_READ_ACTIONS, resources=["*"]))
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["iotsitewise:BatchPutAssetPropertyValue"],
                resources=list(pipe_asset_arns)
                + [self.format_arn(service="iotsitewise", resource="time-series", resource_name="*")],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(actions=INCIDENT_ACTIONS, resources=[self.incidents_table.table_arn])
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=NOTIFY_ACTIONS,
                resources=[self.incidents_table.table_arn],
                conditions={"ForAllValues:StringEquals": {"dynamodb:Attributes": NOTIFY_ATTRIBUTES}},
            )
        )
        self.role.add_to_policy(iam.PolicyStatement(actions=["sns:Publish"], resources=[self.operator_topic.topic_arn]))
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[self.log_group.log_group_arn],
            )
        )

        self.function = lambda_.DockerImageFunction(
            self,
            "Function",
            function_name=name,
            description=f"Detection run every minute ({stage})",
            code=lambda_.DockerImageCode.from_image_asset(
                str(REPO_ROOT),
                file=DOCKERFILE,
                platform=ecr_assets.Platform.LINUX_AMD64,
                exclude=IMAGE_EXCLUDE,
            ),
            architecture=lambda_.Architecture.X86_64,
            memory_size=MEMORY_MB,
            timeout=cdk.Duration.seconds(TIMEOUT_S),
            reserved_concurrent_executions=1,
            retry_attempts=0,
            max_event_age=cdk.Duration.seconds(MAX_EVENT_AGE_S),
            role=self.role,
            log_group=self.log_group,
            environment={
                "STAGE": stage,
                "READER_STRATEGY": DEFAULT_STRATEGY,
                "INCIDENTS_TABLE": self.incidents_table.table_name,
                "OPERATOR_TOPIC_ARN": self.operator_topic.topic_arn,
                "GRAFANA_URL": grafana_url,
                "DASHBOARD_UID": dashboard_uid("pipe-detail", stage),
            },
        )

        self.invoke_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"detection-scheduler-{stage}",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": self.account}}
            ),
            description=f"EventBridge Scheduler ({stage}): invoke the detection Lambda only",
        )
        self.invoke_role.add_to_policy(
            iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[self.function.function_arn])
        )

        self.schedule = scheduler.CfnSchedule(
            self,
            "Schedule",
            name=name,
            description=f"Detection run every minute ({stage}); follows the simulator switch",
            schedule_expression=SCHEDULE,
            schedule_expression_timezone="UTC",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            state=state,
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.function.function_arn,
                role_arn=self.invoke_role.role_arn,
                input=SCHEDULER_INPUT,
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_retry_attempts=0, maximum_event_age_in_seconds=MAX_EVENT_AGE_S
                ),
            ),
        )
        # The invoke grant must exist before the first scheduled call.
        self.schedule.node.add_dependency(self.invoke_role)

        cdk.CfnOutput(self, "FunctionName", value=self.function.function_name)
        cdk.CfnOutput(self, "ScheduleName", value=name)
        cdk.CfnOutput(self, "IncidentsTableName", value=self.incidents_table.table_name)
        cdk.CfnOutput(self, "ScheduleState", value=state)
        cdk.CfnOutput(self, "OperatorTopicArn", value=self.operator_topic.topic_arn)

        # AD-15 Health: two alarms, ALARM and OK to the team topic only (never the operator topic).
        self.team_topic = self._email_topic(
            "TeamAlarmsTopic", f"team-alarms-{stage}", f"POC platform health ({stage})", TEAM_EMAILS_CONTEXT
        )
        # The Deny-only topic policy replaces SNS's default one, so CloudWatch needs an explicit publish grant,
        # limited to this account's alarms.
        self.team_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmsPublish",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.team_topic.topic_arn],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": self.format_arn(
                            service="cloudwatch",
                            resource="alarm",
                            resource_name="*",
                            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            )
        )
        team_action = cw_actions.SnsAction(self.team_topic)
        self.errors_alarm = cloudwatch.Alarm(
            self,
            "DetectionErrorsAlarm",
            alarm_name=f"detection-errors-{stage}",
            alarm_description=f"Detection Lambda {name} reported at least one error in 5 minutes",
            metric=cloudwatch.Metric(
                namespace="AWS/Lambda",
                metric_name="Errors",
                dimensions_map={"FunctionName": name},
                statistic="Sum",
                period=cdk.Duration.seconds(300),
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        # Container Insights reports an explicit 0 with no task; missing data also breaches. Three minutes ride over
        # a stop-first redeploy.
        simulator = f"simulator-{stage}"
        self.simulator_alarm = cloudwatch.Alarm(
            self,
            "SimulatorDownAlarm",
            alarm_name=f"simulator-down-{stage}",
            alarm_description=f"Simulator service {simulator} has had no running task for 3 minutes",
            metric=cloudwatch.Metric(
                namespace="ECS/ContainerInsights",
                metric_name="RunningTaskCount",
                dimensions_map={"ClusterName": simulator, "ServiceName": simulator},
                statistic="Minimum",
                period=cdk.Duration.seconds(60),
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=3,
            datapoints_to_alarm=3,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            # A deliberate stop (-c simulator=off) must not page the team.
            actions_enabled=state == "ENABLED",
        )
        for alarm in (self.errors_alarm, self.simulator_alarm):
            alarm.add_alarm_action(team_action)
            alarm.add_ok_action(team_action)

        cdk.CfnOutput(self, "TeamAlarmsTopicArn", value=self.team_topic.topic_arn)

    def _email_topic(self, construct_id: str, topic_name: str, display_name: str, context_name: str) -> sns.Topic:
        """A topic whose email subscriptions come only from the ``context_name`` allowlist; ``sns:Subscribe`` is
        denied to every principal except the deploy role."""
        topic = sns.Topic(self, construct_id, topic_name=topic_name, display_name=display_name)
        for addr in emails(self.node.try_get_context(context_name), context_name):
            topic.add_subscription(subs.EmailSubscription(addr))
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySubscribeExceptDeployRole",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["sns:Subscribe"],
                resources=[topic.topic_arn],
                conditions={
                    "ArnNotLike": {
                        "aws:PrincipalArn": self.format_arn(
                            service="iam",
                            region="",
                            resource="role",
                            resource_name=f"{DEPLOY_ROLE_PREFIX}{self.account}-{self.region}",
                        )
                    }
                },
            )
        )
        return topic
