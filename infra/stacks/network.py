"""Shared network: one VPC for every stage (Story 1.4, human decision 2026-09-28).

Fixed name ``SharedNetworkStack`` (no stage suffix): 2 AZs, public subnets only, no NAT
gateway. Every ``SimulatorStack-<stage>`` depends on it, so any stage deploy creates or
reuses it, and deleting a stage leaves it in place. One VPC keeps stages clear of the
region's VPC quota (5, one used by the default VPC).
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

STACK_NAME = "SharedNetworkStack"


class SharedNetworkStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str = STACK_NAME, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ],
        )
