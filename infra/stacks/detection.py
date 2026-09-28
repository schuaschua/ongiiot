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
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from detect.adapters.reader import DEFAULT_STRATEGY
from facility.model import Facility
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

        # AD-17: exactly these grants, nothing else.
        self.role = iam.Role(
            self,
            "FunctionRole",
            role_name=name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"Detection run ({stage}): SiteWise read, derived writes, own logs",
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
            environment={"STAGE": stage, "READER_STRATEGY": DEFAULT_STRATEGY},
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
        cdk.CfnOutput(self, "ScheduleState", value=state)
