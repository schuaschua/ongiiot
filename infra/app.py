"""CDK app entry point: one app, one region, one stack set per stage (AD-10, AD-16).

    npx aws-cdk@2 synth -c stage=poc-dj [-c simulator=on|off]

``SharedNetworkStack`` (fixed name, one VPC for every stage) is always in the app; each
``SimulatorStack-<stage>`` depends on it, so ``cdk deploy "*-<stage>"`` creates or reuses it.
``SharedGrafanaStack`` (fixed name, one Managed Grafana workspace) works the same way through
``GrafanaStack-<stage>``, which provisions the stage's folder, data source and dashboards.
``DetectionStack-<stage>`` runs the 1-minute detection Lambda; its schedule follows the same
``simulator`` switch (``off`` disables it).

``-c oidc=true`` also adds the one-time ``GitHubOidcStack`` (fixed name, no stage
suffix). Workflows never pass it and deploy only ``*-<stage>``, so they do not target it;
see docs/ci-cd.md for what that does and does not protect.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from facility.model import DEFAULT_PATH, load
from infra.stacks.detection import DetectionStack
from infra.stacks.github_oidc import STACK_NAME as OIDC_STACK_NAME
from infra.stacks.github_oidc import GitHubOidcStack
from infra.stacks.grafana import SharedGrafanaStack
from infra.stacks.grafana_provision import GrafanaStack
from infra.stacks.network import SharedNetworkStack
from infra.stacks.simulator import SimulatorStack
from infra.stacks.sitewise_assets import SiteWiseAssetsStack

REGION = "ap-southeast-1"


def build_app(app: cdk.App | None = None) -> cdk.App:
    app = app or cdk.App()
    stage = app.node.try_get_context("stage")
    if stage is None:
        stage = "poc"
    facility = load(DEFAULT_PATH, stage=stage)  # validates the stage name too
    env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION)
    network = SharedNetworkStack(app, env=env)  # one VPC for every stage
    sitewise = SiteWiseAssetsStack(app, f"SiteWiseAssetsStack-{stage}", facility=facility, env=env)
    simulator = SimulatorStack(
        app,
        f"SimulatorStack-{stage}",
        facility=facility,
        pipe_asset_arns=[a.attr_asset_arn for a in sitewise.pipe_assets.values()],
        vpc=network.vpc,
        env=env,
    )
    simulator.add_stack_dependency(sitewise)  # AD-10: the simulator stack depends on the asset stack
    simulator.add_stack_dependency(network)  # `cdk deploy "*-<stage>"` creates or reuses it
    detection = DetectionStack(
        app,
        f"DetectionStack-{stage}",
        facility=facility,
        pipe_asset_arns=[a.attr_asset_arn for a in sitewise.pipe_assets.values()],
        env=env,
    )
    detection.add_stack_dependency(sitewise)  # derived writes go to this stage's pipe assets
    shared_grafana = SharedGrafanaStack(app, env=env)  # one workspace for every stage
    GrafanaStack(app, f"GrafanaStack-{stage}", facility=facility, shared=shared_grafana, env=env)
    if str(app.node.try_get_context("oidc")).lower() == "true":
        GitHubOidcStack(app, OIDC_STACK_NAME)
    return app


if __name__ == "__main__":
    build_app().synth()
