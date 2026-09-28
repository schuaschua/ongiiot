"""CDK Template assertions for the shared network stack (one VPC for every stage)."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infra.app import build_app


def test_shared_vpc_public_only_no_nat():
    app = build_app(cdk.App(context={"stage": "poc-dj"}))
    stack = app.node.find_child("SharedNetworkStack")
    assert stack.stack_name == "SharedNetworkStack"
    assert stack.region == "ap-southeast-1"
    tpl = Template.from_stack(stack).to_json()
    types = [r["Type"] for r in tpl["Resources"].values()]
    assert types.count("AWS::EC2::VPC") == 1
    assert "AWS::EC2::NatGateway" not in types and "AWS::EC2::EIP" not in types
    subnets = [r["Properties"] for r in tpl["Resources"].values() if r["Type"] == "AWS::EC2::Subnet"]
    assert len(subnets) == 2 and all(s["MapPublicIpOnLaunch"] is True for s in subnets)
    assert "AWS::EC2::InternetGateway" in types
