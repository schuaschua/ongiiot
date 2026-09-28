# CI/CD runbook: GitHub Actions with AWS OIDC

All deploys run in GitHub Actions for `schuaschua/ongiiot` into AWS account `123456789012`, region `ap-southeast-1` (AD-16). GitHub holds no AWS keys. Workflows get short-lived credentials (1 h at most) through OIDC from two roles:

| Role | Trusted for (OIDC `sub`, exact match) | May assume |
| --- | --- | --- |
| `ongiiot-github-deploy` | `repo:schuaschua@83538399/ongiiot@1392670803:environment:poc`, `repo:schuaschua@83538399/ongiiot@1392670803:environment:dev` | the CDK bootstrap roles `cdk-hnb659fds-*-123456789012-ap-southeast-1` |
| `ongiiot-github-diff` | `repo:schuaschua@83538399/ongiiot@1392670803:pull_request` | only the CDK lookup role `cdk-hnb659fds-lookup-role-123456789012-ap-southeast-1` (no write access, but it carries the account-wide `ReadOnlyAccess` managed policy) |

Both roles may also read `/cdk-bootstrap/hnb659fds/version`. Neither has any direct resource permission. Tokens from forks or other repos cannot assume either role. The deploy role refuses any job that does not declare the `poc` or `dev` environment. The diff role refuses any job that is not a `pull_request` run without an environment.

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `.github/workflows/ci.yml` | pull request to `main`, push to `main` | `test` (pytest), `synth` (`cdk synth -c stage=poc`, no AWS), `diff` (PRs only, diff role: `cdk diff "*-poc" --method template`). Never deploys. |
| `.github/workflows/deploy.yml` | manual `workflow_dispatch` with `stage=poc`, from `main` only | `test`, then deploys `*-poc` in the `poc` environment (input `simulator`: `on` or `off`, see section 5) |
| `.github/workflows/deploy.yml` | manual `workflow_dispatch` with `stage=poc-<initials>`, any branch | `test`, then deploys `*-poc-<initials>` in the `dev` environment |

Nothing deploys automatically. A push or merge to `main` only runs `ci.yml`. Starting the `deploy` run is the approval: GitHub Team does not support required reviewers on private repos, so there is no separate approval step.

In `deploy.yml`, the `deploy` job needs both `test` and `resolve`. If the tests fail, AWS is never touched.

The CDK CLI is pinned to `aws-cdk@2.1143.0` (Node 22). Python deps come from `uv.lock` (`uv sync --locked`). The deploy builds the simulator image (`sim/Dockerfile`, linux/amd64) with the runner's Docker and publishes it through the CDK image-publishing bootstrap role; `ci.yml`'s `cdk synth` does not build it.

## 1. One-time AWS setup

Run this once from a laptop with the admin profile. The account must already be CDK-bootstrapped (qualifier `hnb659fds`).

```bash
npx --yes aws-cdk@2.1143.0 deploy GitHubOidcStack -c oidc=true --profile ongiiot-poc
```

Copy both outputs:

- `GitHubOidcStack.DeployRoleArn`: should be `arn:aws:iam::123456789012:role/ongiiot-github-deploy`
- `GitHubOidcStack.DiffRoleArn`: should be `arn:aws:iam::123456789012:role/ongiiot-github-diff`

`GitHubOidcStack` is only in the app when `-c oidc=true` is set. The workflows never pass that flag and deploy only `*-<stage>` stacks, so they do not target it. To change the trust, edit `infra/stacks/github_oidc.py` and run the command above again. (A workflow could still change it; see "What this does and does not protect".)

## 2. One-time GitHub setup

**Finish this step before the first `poc` deploy.** GitHub creates a missing environment automatically, with no branch rule, the first time a job refers to it.

In the repo, go to **Settings → Environments** and create:

- **`poc`**: under deployment branches and tags, choose "Selected branches and tags" and add the rule `main`. (Required reviewers need GitHub Enterprise for private repos; this org is on Team, so leave them off.)
- **`dev`**: no branch rule.

In **Settings → Secrets and variables → Actions → Variables** (repository variables, not secrets), add:

| Name | Value |
| --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | the `DeployRoleArn` output from step 1 |
| `AWS_DIFF_ROLE_ARN` | the `DiffRoleArn` output from step 1 |
| `AWS_REGION` | `ap-southeast-1` |

Do not add AWS access keys or other AWS secrets.

## 3. Deploying `poc`

Merge to `main`, then open **Actions → deploy → Run workflow**, choose branch `main`, and enter `stage` = `poc` (or `gh workflow run deploy.yml --ref main -f stage=poc`). The run tests first, then deploys `*-poc`. `poc` only deploys from `main`: the `resolve` job refuses it from any other ref, and the `poc` environment's branch rule is a second guard. Runs for the same stage never overlap (`concurrency: deploy-<stage>`). A running deploy is never cancelled, but a newer queued run can replace an older queued one.

## 4. Deploying a developer stage

In GitHub, open **Actions → deploy → Run workflow**, pick your branch, and enter `stage` = `poc-<initials>` (for example `poc-dj`). Or use the CLI:

```bash
gh workflow run deploy.yml --ref <branch> -f stage=poc-dj
```

The stage must be `poc-` followed by 1 to 12 lowercase letters or digits (`facility/model.py` `STAGE_RE`, checked with `fullmatch`). The `resolve` job refuses `poc` from a branch other than `main`, `prod`, `poc-DJ!` and anything else before any AWS call. The tests must pass, then the deploy targets only `*-poc-<initials>`.

## 5. Stopping and starting the simulator

Each stage runs one simulator task on ECS Fargate (`SimulatorStack-<stage>`), which is expected to write every raw alias at 1 Hz. This is confirmed only once the first `poc-<initials>` deploy has run. It runs only while testing. The `deploy` workflow has a required `simulator` input, `on` (default) or `off`, passed to CDK as `-c simulator=<value>`:

- `on` sets the service's desired count to 1.
- `off` sets it to 0. Nothing else changes: the `sim-control-<stage>` and `injection-log-<stage>` tables and the SiteWise history stay.

To stop or start it, dispatch the deploy for the same stage with the other value:

```bash
gh workflow run deploy.yml --ref <branch> -f stage=poc-dj -f simulator=off   # stop
gh workflow run deploy.yml --ref <branch> -f stage=poc-dj -f simulator=on    # start
```

For `poc` use `--ref main`. Any deploy without `-f simulator=off` starts the simulator again, because the default is `on`.

After a start, fresh values should appear within about 2 minutes. Check a raw alias (`/<stage>/lng1/<area>/<pipe_id>/<measurement>` for developer stages, `/lng1/<area>/<pipe_id>/<measurement>` for `poc`), and the task log group `/ecs/simulator-<stage>`:

```bash
aws iotsitewise get-asset-property-value --region ap-southeast-1 \
  --property-alias /poc-dj/lng1/train1/P-007/flow_in
```

Allow at least 30 minutes of warm-up before a demo or a detection test (AD-15: two detection windows of history).

- **AccessDenied:** the task role grants `iotsitewise:DescribeTimeSeries` and `iotsitewise:BatchPutAssetPropertyValue` on the stage's pipe asset ARNs. If the task stops with `AccessDenied` on either action, the fallback is to add the stage's `arn:aws:iotsitewise:ap-southeast-1:<account>:time-series/*` resource to that grant in `infra/stacks/simulator.py` (AD-17).
- **Shared network:** all stages run in one VPC, `SharedNetworkStack` (fixed name, no stage suffix). The first stage deploy creates it, because every `SimulatorStack-<stage>` depends on it; later deploys reuse it. Deleting a stage leaves it in place.
- **`poc` tables are retained:** `sim-control-poc` and `injection-log-poc` have fixed names and `RETAIN`. After deleting `SimulatorStack-poc`, delete those tables (or import them into the stack) before redeploying, or the deploy fails because the names already exist.

## What this does and does not protect

Protected:

- No long-lived AWS keys exist in the repo or in GitHub.
- Only this repo can get AWS credentials. Forks and other repos are denied by AWS.
- A job that declares no environment (on a pull request or any branch) cannot assume the deploy role. AWS denies it.
- The PR `diff` job holds only the diff role, which can reach only the lookup role. It has no write access.
- A `poc` deploy needs someone to start it deliberately, passing tests, and the `main` branch.

Not protected (residual risk):

- **All stages share one account and one set of CDK bootstrap roles.** The deploy role can assume the same `cfn-exec` role (admin in this account) for every stage. A run that holds `dev` credentials can therefore technically reach `poc`, or any other resource in the account, including `GitHubOidcStack` itself.
- **There is no second-person approval for `poc`.** Anyone with write access can start a `poc` deploy from `main`.
- **The `main`-only rule for `poc` is enforced by AWS against PRs and branch runs without an environment, not against an edited workflow on the `dev` path.** Anyone who can push a branch and dispatch `deploy.yml` can edit the workflow on that branch to run arbitrary commands in the `dev` environment, with no further check. The stage check and the `*-<stage>` target are workflow code, not an AWS boundary.
- **An edited workflow that declares `dev` gets the deploy role.** A same-repo pull request or branch can change its workflow to set `environment: dev`. Its token then has the `sub` `repo:schuaschua@83538399/ongiiot@1392670803:environment:dev`, which the deploy role trusts. Combined with the first point, that reaches `poc` too.
- **The diff role can read the whole account.** The lookup role carries the account-wide `ReadOnlyAccess` managed policy. A same-repo pull request that edits `ci.yml` can read data across the account, such as S3 objects, logs, Lambda environment variables and SSM parameters.
- The environments, reviewers and variables live in GitHub settings, not in the repo. Deleting the `poc` environment or its branch rule removes the `main`-only guard.
- Anyone with the admin profile can still deploy from a laptop. "Every deploy runs in GitHub Actions" is a team rule, not an enforced control.

Per-stage bootstrap isolation (separate qualifiers or accounts per stage) would close the gaps above that come from the shared bootstrap roles. It is deferred (spine, Deferred).

## Troubleshooting

- **`Resource of type 'AWS::Logs::LogGroup' with identifier '...' already exists`** after a failed first deploy: the stack rolled back, but a Lambda had already logged and recreated its named log group. Delete the listed log groups (`aws logs delete-log-group --log-group-name <name> --region ap-southeast-1`), then deploy again.
- **Simulator task exits at startup**: on a fresh stage SiteWise has no time series until the first write, so the simulator accepts "not found" aliases at startup and checks them strictly after its first write. It stops (exit 3, "refusing to continue") if an alias is still missing or is not attached to an asset property.

- **Subject format**: this repo uses GitHub's immutable OIDC subject, so tokens carry `repo:schuaschua@83538399/ongiiot@1392670803:…` (numeric owner and repo IDs), not `repo:schuaschua/ongiiot:…`. Check with `gh api repos/schuaschua/ongiiot/actions/oidc/customization/sub`.

- **`Not authorized to perform sts:AssumeRoleWithWebIdentity`**: the run is from a fork or another repo, the job lacks `id-token: write`, the job's environment is not `poc` or `dev` (deploy role), the job is not a `pull_request` run (diff role), or the role variable is wrong.
- **`cdk diff` / `deploy` fails assuming `cdk-hnb659fds-*`**: the account or region is not bootstrapped, or the qualifier changed. Re-bootstrap, or update `BOOTSTRAP_QUALIFIER` in `infra/stacks/github_oidc.py` and redeploy step 1.
- **The `deploy` job never starts**: the `test` job failed, or the `resolve` job refused the stage. Check those jobs first.
