"""CDK app entry point: one app, one region, one stack set per stage (AD-10, AD-16).

    npx aws-cdk@2 synth -c stage=poc-dj
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from facility.model import DEFAULT_PATH, load
from infra.stacks.sitewise_assets import SiteWiseAssetsStack

REGION = "ap-southeast-1"


def build_app(app: cdk.App | None = None) -> cdk.App:
    app = app or cdk.App()
    stage = app.node.try_get_context("stage")
    if stage is None:
        stage = "poc"
    facility = load(DEFAULT_PATH, stage=stage)  # validates the stage name too
    env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=REGION)
    SiteWiseAssetsStack(app, f"SiteWiseAssetsStack-{stage}", facility=facility, env=env)
    return app


if __name__ == "__main__":
    build_app().synth()
