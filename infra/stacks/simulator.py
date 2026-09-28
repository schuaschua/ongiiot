"""Simulator stack: the 1 Hz raw stream on ECS Fargate, per stage (Story 1.4; AD-10, AD-15, AD-17).

- ``sim-control-<stage>`` and ``injection-log-<stage>`` tables (key ``pk`` S, on demand),
  retained for ``poc``, destroyed for developer stages.
- Runs in the shared VPC (``SharedNetworkStack``: public subnets only, no NAT); the task
  gets a public IP and a per-stage security group with no ingress.
- One Fargate service running ``sim/Dockerfile`` (built from ``uv.lock``, linux/amd64).
  CDK context ``simulator`` (``on`` default, or ``off``) sets the desired count to 1 or 0;
  nothing else changes, so tables and history survive a stop.
- The task role holds exactly the AD-17 simulator grants. IAM cannot scope
  ``BatchPutAssetPropertyValue`` by alias (no action supports ``iotsitewise:propertyAlias``),
  so the raw/derived boundary is the guard in ``sim/writer.py`` (see the spine, AD-17).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from facility.model import Facility

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = "sim/Dockerfile"
# Build-context paths only the other image needs: kept out of this asset's fingerprint, so a
# detection change never rebuilds or restarts the simulator.
IMAGE_EXCLUDE = ["detect"]
SWITCH_CONTEXT = "simulator"
SWITCH_VALUES = {"on": 1, "off": 0}


def desired_count(value: object) -> int:
    """Desired task count from the ``simulator`` context value (absent means ``on``)."""
    key = "on" if value is None else str(value)
    if key not in SWITCH_VALUES:
        raise ValueError(f"context {SWITCH_CONTEXT}={value!r} is invalid: use 'on' or 'off'")
    return SWITCH_VALUES[key]


class SimulatorStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        facility: Facility,
        pipe_asset_arns: Sequence[str],
        vpc: ec2.IVpc,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        stage = facility.stage
        count = desired_count(self.node.try_get_context(SWITCH_CONTEXT))
        removal = cdk.RemovalPolicy.RETAIN if stage == "poc" else cdk.RemovalPolicy.DESTROY

        self.control_table = self._table("SimControlTable", f"sim-control-{stage}", removal)
        self.log_table = self._table("InjectionLogTable", f"injection-log-{stage}", removal)

        self.vpc = vpc
        self.security_group = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=self.vpc,
            description=f"Simulator task ({stage}): egress only, no ingress",
            allow_all_outbound=True,
        )
        self.cluster = ecs.Cluster(self, "Cluster", cluster_name=f"simulator-{stage}", vpc=self.vpc)

        image = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory=str(REPO_ROOT),
            file=DOCKERFILE,
            platform=ecr_assets.Platform.LINUX_AMD64,
            exclude=IMAGE_EXCLUDE,
        )

        # AD-17: exactly these grants, nothing else.
        self.task_role = iam.Role(
            self,
            "TaskRole",
            role_name=f"simulator-task-{stage}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=f"Simulator task ({stage}): raw SiteWise writes, sim_control read, injection_log write",
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iotsitewise:BatchPutAssetPropertyValue", "iotsitewise:DescribeTimeSeries"],
                # Alias-addressed calls are authorised against the time series (AccessDenied on the
                # first real deploy with asset ARNs only); IAM cannot narrow it by alias (AD-17).
                resources=list(pipe_asset_arns)
                + [self.format_arn(service="iotsitewise", resource="time-series", resource_name="*")],
            )
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:Scan"], resources=[self.control_table.table_arn])
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[self.log_table.table_arn])
        )

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/ecs/simulator-{stage}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # The execution role (created by CDK) gets only ECR pull and log writes.
        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            family=f"simulator-{stage}",
            cpu=256,
            memory_limit_mib=512,
            task_role=self.task_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        self.task_definition.add_container(
            "sim",
            image=ecs.ContainerImage.from_docker_image_asset(image),
            essential=True,
            environment={
                "STAGE": stage,
                "SIM_CONTROL_TABLE": self.control_table.table_name,
                "INJECTION_LOG_TABLE": self.log_table.table_name,
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="sim", log_group=self.log_group),
        )

        # At most one task: the old task stops before a new one starts (AD-3, no
        # overlapping writers per alias).
        self.service = ecs.FargateService(
            self,
            "Service",
            service_name=f"simulator-{stage}",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=count,
            min_healthy_percent=0,
            max_healthy_percent=100,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[self.security_group],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        cdk.CfnOutput(self, "SimControlTableName", value=self.control_table.table_name)
        cdk.CfnOutput(self, "InjectionLogTableName", value=self.log_table.table_name)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)

    def _table(self, construct_id: str, name: str, removal: cdk.RemovalPolicy) -> dynamodb.Table:
        return dynamodb.Table(
            self,
            construct_id,
            table_name=name,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal,
        )
