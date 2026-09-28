"""Static checks on the GitHub Actions workflows (AD-16; docs/ci-cd.md)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CDK_CLI = "aws-cdk@2.1143.0"
ACTION_MAJORS = {
    "actions/checkout": "v7",
    "astral-sh/setup-uv": "v10.2.0",  # publishes exact tags only (no floating v10)
    "actions/setup-node": "v7",
    "aws-actions/configure-aws-credentials": "v6",
}
# The only jobs that touch AWS, and the role variable each assumes.
ROLE_VARS = {
    "ci.yml": {"diff": "AWS_DIFF_ROLE_ARN"},
    "deploy.yml": {"deploy": "AWS_DEPLOY_ROLE_ARN"},
}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(wf: dict) -> dict:
    # PyYAML reads the bare key `on` as boolean True.
    return wf.get("on", wf.get(True))


@pytest.fixture(scope="module")
def ci() -> dict:
    return load("ci.yml")


@pytest.fixture(scope="module")
def deploy() -> dict:
    return load("deploy.yml")


def steps(wf: dict):
    for job_name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, step


def uses_aws(job: dict) -> bool:
    return any(s.get("uses", "").startswith("aws-actions/") for s in job.get("steps", []))


@pytest.mark.parametrize("name", ["ci.yml", "deploy.yml"])
def test_common_hygiene(name):
    text = (WORKFLOWS / name).read_text()
    wf = load(name)
    assert "secrets." not in text
    assert wf["permissions"] == {"contents": "read"}
    for job_name, job in wf["jobs"].items():
        perms = job.get("permissions", wf["permissions"])
        if uses_aws(job):
            assert perms == {"contents": "read", "id-token": "write"}, job_name
        else:
            assert perms.get("id-token") is None, job_name
            assert perms == {"contents": "read"}, job_name
    for job_name, step in steps(wf):
        if "uses" in step:
            action, _, ref = step["uses"].partition("@")
            assert ACTION_MAJORS[action] == ref, (job_name, step["uses"])
        run = step.get("run", "")
        assert not re.search(r"\$\{\{\s*(github\.event\.)?inputs\.", run), (job_name, run)
        if "cdk" in run:
            assert f"npx --yes {CDK_CLI} " in run, run
        if "uv sync" in run:
            assert "--locked" in run
    aws_jobs = {job_name for job_name, job in wf["jobs"].items() if uses_aws(job)}
    assert aws_jobs == set(ROLE_VARS[name])
    for job_name in aws_jobs:
        job = wf["jobs"][job_name]
        (creds,) = [s for s in job["steps"] if s.get("uses", "").startswith("aws-actions/")]
        assert creds["with"] == {
            "role-to-assume": "${{ vars.%s }}" % ROLE_VARS[name][job_name],
            "aws-region": "${{ vars.AWS_REGION }}",
        }


def test_ci_triggers_and_jobs(ci):
    assert triggers(ci) == {"pull_request": {"branches": ["main"]}, "push": {"branches": ["main"]}}
    assert set(ci["jobs"]) == {"test", "synth", "image", "diff"}
    runs = {j: [s.get("run", "") for s in job["steps"]] for j, job in ci["jobs"].items()}
    assert "uv run pytest -q" in runs["test"]
    assert f"npx --yes {CDK_CLI} synth -c stage=poc --quiet" in runs["synth"]
    assert f'npx --yes {CDK_CLI} diff "*-poc" -c stage=poc --method template' in runs["diff"]
    assert ci["jobs"]["diff"]["if"] == "github.event_name == 'pull_request'"
    assert runs["image"] == [
        "",
        "docker build --platform linux/amd64 -f sim/Dockerfile -t ongiiot-sim:ci .",
        'docker run --rm --platform linux/amd64 ongiiot-sim:ci python -c'
        ' "import sim.main, boto3, yaml; from facility.model import load; load()"',
    ]
    assert not uses_aws(ci["jobs"]["test"]) and not uses_aws(ci["jobs"]["synth"])
    assert not uses_aws(ci["jobs"]["image"]) and "permissions" not in ci["jobs"]["image"]
    for job in ci["jobs"].values():
        assert "environment" not in job
        assert not any(" deploy" in s.get("run", "") for s in job["steps"])


def test_deploy_triggers(deploy):
    # Deploys are manual only: starting the run is the approval for `poc`.
    on = triggers(deploy)
    assert set(on) == {"workflow_dispatch"}
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"stage", "simulator"}
    assert inputs["stage"]["required"] is True


def test_deploy_simulator_input(deploy):
    # Quoted in the YAML: PyYAML (YAML 1.1) would otherwise read on/off as booleans.
    sim = triggers(deploy)["workflow_dispatch"]["inputs"]["simulator"]
    assert sim["type"] == "choice"
    assert sim["options"] == ["on", "off"]
    assert sim["default"] == "on"
    assert sim["required"] is True


def test_deploy_test_job(deploy):
    job = deploy["jobs"]["test"]
    assert not uses_aws(job)
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job and "needs" not in job
    assert [s.get("run") for s in job["steps"] if "run" in s] == ["uv sync --locked", "uv run pytest -q"]


def test_deploy_resolve_has_no_aws(deploy):
    resolve = deploy["jobs"]["resolve"]
    assert not uses_aws(resolve)
    assert resolve["permissions"] == {"contents": "read"}
    assert "environment" not in resolve
    assert resolve["outputs"] == {
        "stage": "${{ steps.out.outputs.stage }}",
        "environment": "${{ steps.out.outputs.environment }}",
    }
    (out,) = [s for s in resolve["steps"] if s.get("id") == "out"]
    assert out["env"] == {"STAGE": "${{ inputs.stage }}"}


def run_resolve(deploy: dict, stage: str, tmp_path: Path) -> dict[str, str]:
    (out,) = [s for s in deploy["jobs"]["resolve"]["steps"] if s.get("id") == "out"]
    output = tmp_path / "github_output"
    output.write_text("")
    subprocess.run(
        ["bash", "-e", "-c", out["run"]],
        env={**os.environ, "STAGE": stage, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("poc", {"stage": "poc", "environment": "poc"}),
        ("poc-dj", {"stage": "poc-dj", "environment": "dev"}),
    ],
)
def test_resolve_outputs(deploy, tmp_path, stage, expected):
    assert run_resolve(deploy, stage, tmp_path) == expected


def test_deploy_job_wiring(deploy):
    job = deploy["jobs"]["deploy"]
    assert sorted(job["needs"]) == ["resolve", "test"]
    assert job["environment"] == "${{ needs.resolve.outputs.environment }}"
    assert job["concurrency"] == {
        "group": "deploy-${{ needs.resolve.outputs.stage }}",
        "cancel-in-progress": False,
    }
    assert job["env"] == {
        "STAGE": "${{ needs.resolve.outputs.stage }}",
        "SIMULATOR": "${{ inputs.simulator }}",
    }
    (run,) = [s["run"] for s in job["steps"] if "cdk" in s.get("run", "")]
    assert run == (
        f'npx --yes {CDK_CLI} deploy "*-$STAGE" -c "stage=$STAGE" -c "simulator=$SIMULATOR" --require-approval never'
    )
    assert "GitHubOidcStack" not in (WORKFLOWS / "deploy.yml").read_text()
    assert "oidc" not in run


def stage_check_code(deploy: dict) -> str:
    (step,) = [s for s in deploy["jobs"]["resolve"]["steps"] if s.get("name") == "Check dispatch stage"]
    assert "if" not in step
    assert step["env"] == {"STAGE": "${{ inputs.stage }}", "REF": "${{ github.ref }}"}
    argv = shlex.split(step["run"])
    assert argv[:4] == ["uv", "run", "python", "-c"]
    return argv[4]


@pytest.mark.parametrize(
    "stage,ref,ok",
    [
        ("poc", "refs/heads/main", True),
        ("poc", "refs/heads/feat/cicd", False),
        ("poc", "refs/tags/main", False),
        ("prod", "refs/heads/main", False),
        ("poc\n", "refs/heads/main", False),
        ("poc-DJ!", "refs/heads/feat/x", False),
        ("poc-", "refs/heads/feat/x", False),
        ("poc-dj!", "refs/heads/feat/x", False),
        ("poc-" + "a" * 13, "refs/heads/feat/x", False),
        ("poc-dj\n", "refs/heads/feat/x", False),
        ("poc-" + "a" * 12, "refs/heads/feat/x", True),
        ("poc-dj", "refs/heads/feat/x", True),
        ("poc-dj", "refs/heads/main", True),
    ],
)
def test_dispatch_stage_check(deploy, stage, ref, ok):
    code = stage_check_code(deploy)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "STAGE": stage, "REF": ref, "PYTHONPATH": str(ROOT)},
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert (proc.returncode == 0) is ok, proc.stderr
    if not ok:
        assert "refused stage" in proc.stderr
