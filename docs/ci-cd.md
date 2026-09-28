# CI/CD runbook: GitHub Actions with AWS OIDC

All deploys run in GitHub Actions for `schuaschua/ongiiot` into AWS account `123456789012`, region `ap-southeast-1` (AD-16). GitHub holds no AWS keys. Workflows get short-lived credentials (1 h at most) through OIDC from two roles:

| Role | Trusted for (OIDC `sub`, exact match) | May assume |
| --- | --- | --- |
| `ongiiot-github-deploy` | `repo:schuaschua@83538399/ongiiot@1392670803:environment:poc`, `repo:schuaschua@83538399/ongiiot@1392670803:environment:dev` | the CDK bootstrap roles `cdk-hnb659fds-*-123456789012-ap-southeast-1` |
| `ongiiot-github-diff` | `repo:schuaschua@83538399/ongiiot@1392670803:pull_request` | only the CDK lookup role `cdk-hnb659fds-lookup-role-123456789012-ap-southeast-1` (no write access, but it carries the account-wide `ReadOnlyAccess` managed policy) |

Both roles may also read `/cdk-bootstrap/hnb659fds/version`. Neither has any direct resource permission. Tokens from forks or other repos cannot assume either role. The deploy role refuses any job that does not declare the `poc` or `dev` environment. The diff role refuses any job that is not a `pull_request` run without an environment.

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `.github/workflows/ci.yml` | pull request to `main`, push to `main` | `test` (pytest), `synth` (`cdk synth -c stage=poc`, no AWS), `image` (builds and smoke-imports the simulator and detection images, no AWS), `diff` (PRs only, diff role: `cdk diff "*-poc" --method template`). Never deploys. |
| `.github/workflows/deploy.yml` | manual `workflow_dispatch` with `stage=poc`, from `main` only | `test`, then deploys `*-poc` in the `poc` environment (input `simulator`: `on` or `off`, see section 5) |
| `.github/workflows/deploy.yml` | manual `workflow_dispatch` with `stage=poc-<initials>`, any branch | `test`, then deploys `*-poc-<initials>` in the `dev` environment |

Nothing deploys automatically. A push or merge to `main` only runs `ci.yml`. Starting the `deploy` run is the approval: GitHub Team does not support required reviewers on private repos, so there is no separate approval step.

In `deploy.yml`, the `deploy` job needs both `test` and `resolve`. If the tests fail, AWS is never touched.

The CDK CLI is pinned to `aws-cdk@2.1143.0` (Node 22). Python deps come from `uv.lock` (`uv sync --locked`). The deploy builds the simulator image (`sim/Dockerfile`) and the detection image (`detect/Dockerfile`), both linux/amd64, with the runner's Docker and publishes them through the CDK image-publishing bootstrap role; `ci.yml`'s `cdk synth` does not build them. Each image asset excludes the other's code, so a detection-only change does not rebuild or restart the simulator.

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

## 5. Stopping and starting the simulator (and detection)

Each stage runs one simulator task on ECS Fargate (`SimulatorStack-<stage>`), which is expected to write every raw alias at 1 Hz. This is confirmed only once the first `poc-<initials>` deploy has run. It runs only while testing. The `deploy` workflow has a required `simulator` input, `on` (default) or `off`, passed to CDK as `-c simulator=<value>`:

- `on` sets the service's desired count to 1 and the detection schedule (`detection-<stage>`, section 7) to `ENABLED`.
- `off` sets the count to 0 and the schedule to `DISABLED`, so detection costs nothing while testing is off. Nothing else changes: the `sim-control-<stage>` and `injection-log-<stage>` tables and the SiteWise history stay.

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

## 6. Grafana

All stages share one Amazon Managed Grafana workspace (Grafana 12.4) in `SharedGrafanaStack` (fixed name, no stage suffix, like `SharedNetworkStack`). The first stage deploy creates it, because every `GrafanaStack-<stage>` depends on it; later deploys reuse it. Deleting a stage leaves it in place. Its workspace role holds only `AWSIoTSiteWiseReadOnlyAccess` (AD-11).

**URL.** The stack outputs `SharedGrafanaStack.GrafanaUrl` and `GrafanaStack-<stage>.GrafanaUrl` carry the workspace URL (`https://g-….grafana-workspace.ap-southeast-1.amazonaws.com`). `GrafanaStack-<stage>` also outputs `PipeDetailUid`, `FacilityOverviewUid`, `FolderUid`, `DataSourceUid` and `SiteWisePluginVersion` (the SiteWise plugin version actually running; 2.6.3 is requested, and if Managed Grafana refuses the install, the shipped version stays and is reported here). The plugin is workspace-wide: a deploy of any stage can change it for every stage.

Example for the stage `poc-dj`:

```bash
aws cloudformation describe-stacks --region ap-southeast-1 --stack-name GrafanaStack-poc-dj \
  --query "Stacks[0].Outputs" --output table
```

**Sign-in.** Open the URL and choose "Sign in with AWS IAM Identity Center". Identity Center is the organization instance in `us-east-1`; the workspace is in `ap-southeast-1`. Only assigned users get in; anyone else is refused.

**Adding a user.** Access is code, not clicks. Add the user's Identity Center user ID (Identity Center console → Users → the user → "User ID") to `ADMIN_USER_IDS` in `infra/stacks/grafana.py` and deploy any stage. A custom resource calls `grafana:UpdatePermissions` to add them as Admin (and revokes them if the stack is deleted). Today only the workspace owner is assigned. `UpdatePermissions` reports per-user failures in its response without failing the deploy, so check that the new user can sign in.

**Removing a user.** Taking an ID out of `ADMIN_USER_IDS` does **not** revoke it: on update the custom resource only adds. Remove it from the list, then revoke by hand:

```bash
WS=$(aws cloudformation describe-stacks --region ap-southeast-1 --stack-name SharedGrafanaStack \
  --query "Stacks[0].Outputs[?OutputKey=='WorkspaceId'].OutputValue" --output text)
aws grafana update-permissions --region ap-southeast-1 --workspace-id "$WS" \
  --update-instruction-batch '[{"action":"REVOKE","role":"ADMIN","users":[{"id":"<identity-center-user-id>","type":"SSO_USER"}]}]'
```

The output's `errors` list must be empty.

**Per-stage content.** Each deploy of `GrafanaStack-<stage>` provisions, through the Grafana HTTP API, during `cdk deploy`:

| Item | `poc` | `poc-<initials>` |
| --- | --- | --- |
| Folder | `ongiiot-poc` | `ongiiot-poc-<initials>` |
| SiteWise data source (workspace IAM role, `ap-southeast-1`) | `sitewise-poc` | `sitewise-poc-<initials>` |
| Pipe detail dashboard uid | `pipe-detail` | `pipe-detail-<initials>` |
| Facility overview dashboard uid | `facility-overview` | `facility-overview-<initials>` |

The dashboards come from `dashboards/pipe-detail.json` and `dashboards/facility-overview.json`, filled with the stage's `alias_root` and pipe list from `facility/model.py`. A re-run updates everything in place by uid. Edits made in the Grafana UI are overwritten on the next deploy: change the JSON instead. Open a pipe directly with, for example for the stage `poc-dj`, `<GrafanaUrl>/d/pipe-detail-dj/pipe-detail?var-pipe_id=P-007&var-area=train1`.

**Reading trust on the overview.** `facility-overview` (`facility-overview-<initials>` on a developer stage) has one row per area and, per pipe, a block of three tiles stacked in one column (three pipes per grid row):

- `P-xxx (<area>)`: the latest raw value of each of the six sensors.
- `P-xxx trust`: the latest `derived/<measurement>_dq` of each sensor (section 7), 0 to 1. Red means below `dq_min`, green means at or above it. The threshold is `tunables.dq_min` from `facility/facility.yaml`, filled in when the stage deploys (the custom resource property `DqMin`), so changing `dq_min` needs a deploy of `GrafanaStack-<stage>` to move the colour boundary. The text panel at the top shows the value in use.
- `P-xxx failing checks`: the latest `derived/<measurement>_dq_fail` of each sensor. A sensor with no failing check (or a null value) shows a green `none`; otherwise its failing `dq_*` IDs show in red next to the property name (for example `dq_flatline` next to `flow_in_dq_fail`).

A bad sensor therefore shows as a red trust value and a red failing-checks entry on its pipe. Clicking any of the three tiles opens `pipe-detail` for that pipe and area with the current time range. Each tile reads the latest value only (`PropertyValue`), so each open copy of the overview runs 432 queries per 30 s refresh (24 pipes x 18 aliases); two viewers make 864. The latest value is shown however old it is: if detection stops, the trust and failing-checks tiles keep their last colour (green stays green) and nothing on the overview says the scores are stale. Check the pipe-detail time range or the detection Lambda's logs, and rely on the health alarms (AD-15; not built yet) for a stopped detection run. On a stage where detection has never run, the trust and failing-checks tiles show "No data".

The provisioning Lambda (`grafana-provision-<stage>`) holds only the Grafana service-account actions on this workspace. Its per-stage service account (`ongiiot-provision-<stage>`) has the Grafana ADMIN role, which is workspace-wide: while its token lives it could change any stage's folders, data sources and dashboards, not only its own. On every run it mints a service-account token that lives at most 15 minutes and deletes it again. No API key is stored anywhere. Deleting `GrafanaStack-<stage>` removes the stage's folder, dashboards, data source and service account. The CI deploy role and its trust are unchanged: provisioning runs as a CloudFormation custom resource under the CDK execution role.

If sign-in through Identity Center does not work across regions, record it in the spine's Open Questions and fall back to self-hosted Grafana with the same dashboards (see the spine's Open Questions).

## 7. Detection

Each stage runs the detection Lambda `detection-<stage>` (`DetectionStack-<stage>`, image `detect/Dockerfile` built from `uv.lock`). EventBridge Scheduler `detection-<stage>` invokes it every minute with `{"scheduled_time": "<aws.scheduler.scheduled-time>"}`, which is the run ID (AD-12). It is single-flight: reserved concurrency 1, timeout 50 s, no async retries, events older than 60 s dropped, schedule retries 0 and no flexible window. The next scheduled run is the only retry.

Each run reads the window `[floor_minute(scheduled_time) - ingest_lag_s - window_len_s, floor_minute(scheduled_time) - ingest_lag_s)` (tunables from the bundled `facility/facility.yaml`) and writes, for every sensor, `derived/<measurement>_dq` (1.0 = trustworthy) and `derived/<measurement>_dq_fail` (the failing `dq_*` checks, comma-separated, empty if none), timestamped at the window end. `_dq` is `1 -` the highest confidence among the sensor's failing checks (`detect/core/dq_score.py`). A sensor becomes untrusted (`_dq` below `dq_min`, 0.5, set in `tunables.dq_min` in `facility/facility.yaml`) once any check is more than half sure. A sensor with no samples at all scores `0.0` with `dq_gap`. So after a simulator start, expect `0.0` / `dq_gap` until data is older than the ingest lag (about 2 minutes).

The facility overview shows both values for every sensor, coloured by `dq_min` (section 6, "Reading trust on the overview").

Pass 1 runs these checks (`detect/core/detectors/`, tunables under `tunables.detectors.<id>` in `facility/facility.yaml`; a missing or invalid key fails the Lambda's cold start):

| Check | Fires when | Confidence |
| --- | --- | --- |
| `dq_flatline` | the sensor is railed: its latest GOOD samples all sit at or beyond the same contract limit, within `rel_tol` (1e-6) x contract span of `min` or `max` (over- and under-range values count) | `0` below `min_run_s` (30 s), then `run / full_s` (60 s), capped at 1 |
| `dq_stuck` | the sensor is frozen: its latest GOOD samples repeat exactly the same value, and that value is not railed (a railed run is only `dq_flatline`) | same rule, `min_run_s` 30 s, `full_s` 60 s |
| `dq_drift` | the sensor walks away from its peers: its residual (value minus the median of the same measurement on the other pipes of its `peer_group`, second by second) follows a straight line over the last `fit_s` (300 s) whose slope is at least `min_rate` (5e-4 of contract span per minute, 0.175 K/min for a 50–400 K temperature) and that explains at least `min_r2` (0.97) of the residual's variance. Needs at least 2 peers with data and `min_points` (150) residual points | `0` below `min_rate` or `min_r2`, then `rate / full_rate` (1e-3 of span per minute), capped at 1 |
| `dq_gap` | the sensor has gone silent: no GOOD sample for the last `silence_s` of the window (the whole window when it has none, which covers the "no samples" rule) | `0` below `min_gap_s` (30 s), then `silence_s / full_gap_s` (60 s), capped at 1 |

`dq_flatline` and `dq_stuck` look only at the run of GOOD samples ending at the sensor's last GOOD sample in the window, so a sensor that starts varying again clears on the next run. A BAD, UNCERTAIN or valueless sample ends the run, and so does a gap longer than two sample periods (from the contract `rate_hz`, 2 s at 1 Hz). A run of 45 s scores confidence 0.75, so `_dq = 0.25`; a run of 60 s or more scores `_dq = 0.0`.

`dq_gap` looks only at the trailing silence, so an earlier gap with data since gives no flag, and a sensor whose samples are all BAD or UNCERTAIN scores `0.0` / `dq_gap`. A sensor with no samples at all gets exactly one `dq_gap` finding: from the detector, or from the no-sample rule in `dq_score.py` if the detector failed. `dq_drift` compares a sensor with its peers, so a change shared by the whole peer group (a process change) is not drift, and neither is a step such as a leak (a step never fits a line that well).

**A `dq_flatline`, `dq_stuck` or `dq_gap` flag appears 2 to 3 minutes after a fault starts**, because each window ends `ingest_lag_s` (60 s) behind real time, the fault then needs more than 30 s inside the window to push `_dq` below 0.5 (60 s for `_dq = 0.0`), and runs start on the minute. It clears the same way, 1 to 2 minutes after the sensor recovers. Normal ingest lag never raises `dq_gap`: at 1 Hz the last sample sits 1 s before the window end, and data can arrive up to about 85 s late (60 s lag plus 30 s `min_gap_s`, less the simulator's 5 s flush) before any flag.

**A `dq_drift` flag at 0.5 K/min on a temperature appears 5 to 6 minutes after the drift starts** (4 to 5 minutes of drift in the window, plus the 60 s lag) and then stays at `_dq = 0.0` while the drift lasts. It clears 1 to 2 minutes after the drift stops (in the simulator the offset disappears when the injection ends, and a step is not a line).

Known limits:

- A sensor legitimately sitting at a contract limit, such as a shut-in pipe reading flow `0`, is flagged `dq_flatline`. The check cannot tell a real zero from a railed transmitter.
- `dq_stuck` assumes non-quantized readings. A sensor that reports in coarse steps (for example whole degrees) and holds steady can repeat an exact value for a minute and be flagged.
- `dq_drift` only sees drift that is clearly steadier than normal process wander. The simulator's flows and pressures wander by up to about 8e-3 of span per minute over 5 minutes (each pipe has its own process disturbance), so the check relies on `min_r2` to ignore them, and a slow drift on those sensors is found only when it is a clean line. On a 50–400 K temperature, drift below about 0.3 K/min over 5 minutes does not reach `min_r2` (0.25 K/min fits with R² 0.965) and is not flagged.
- `dq_drift` needs at least 2 peers with data. In a peer group of 3 pipes (AD-14 requires at least 4) the median of 2 peers includes the drifting sensor, so its peers could be flagged too.
- `dq_drift` flags drift while it is happening, not a standing offset: a sensor that drifted and then stopped keeps its offset but clears once the fit no longer holds a line. A check for a standing offset from peers is future work.
- `dq_drift` matches peers by whole second, which assumes in-phase 1 Hz sampling across pipes (true for the simulator). Sensors sampled out of phase or at other rates would share fewer points and may fall under `min_points`.
- A peer that is itself faulty still feeds the median. With the current 6-pipe groups the median of 5 peers absorbs one bad peer; smaller groups are less robust.
- A sensor whose samples are all BAD or UNCERTAIN scores `0.0` / `dq_gap` only through the `dq_gap` detector. If that detector fails in a run, the fallback rule in `dq_score.py` covers only sensors with no samples at all, so an all-BAD sensor would score `1.0` for that run.

To try the checks on a developer stage with the simulator on (section 5), inject a fault on each sensor:

```bash
export STAGE=poc-dj SIM_CONTROL_TABLE=sim-control-poc-dj
uv run python -m ops.inject dq_flatline --pipe P-003 --measurement flow_in --minutes 10    # prints the item; note its "pk"
uv run python -m ops.inject dq_stuck --pipe P-015 --measurement temperature --minutes 10   # note this "pk" too
```

Wait 2 to 3 minutes, then read both sensors back. Use each pipe's own area from `facility/facility.yaml` (P-003 is in `train1`, P-015 in `train2`):

```bash
for a in /poc-dj/lng1/train1/P-003/derived/flow_in /poc-dj/lng1/train2/P-015/derived/temperature; do
  for p in _dq _dq_fail; do
    echo "$a$p"; aws iotsitewise get-asset-property-value --region ap-southeast-1 --property-alias "$a$p" \
      --query 'propertyValue.value' --output json
  done
done
# expect flow_in_dq 0.0 / "dq_flatline" and temperature_dq 0.0 / "dq_stuck"; other sensors stay 1.0 / ""
```

Then cancel both injections and confirm recovery: after 1 to 2 minutes the same loop shows `1.0` and an empty `_dq_fail` for both sensors.

```bash
uv run python -m ops.inject cancel <pk of the dq_flatline item>
uv run python -m ops.inject cancel <pk of the dq_stuck item>
```

The simulator's `dq_flatline` holds the contract minimum; `dq_stuck` repeats the last value with GOOD quality.

To try `dq_drift` and `dq_gap`, inject a drift and a silence, then read the sensors back with the same loop (P-010 is in `train1`, P-020 in `train2`):

```bash
uv run python -m ops.inject dq_drift --pipe P-010 --measurement temperature --minutes 15 --per-min 0.5   # note its "pk"
uv run python -m ops.inject dq_gap --pipe P-020 --measurement flow_out --minutes 3                        # note this "pk" too
```

`flow_out_dq` on P-020 reads `0.0` / `dq_gap` from about 2 minutes after the gap starts until 1 to 2 minutes after it ends (it ends by itself after 3 minutes). `temperature_dq` on P-010 reads `0.0` / `dq_drift` from about 5 to 6 minutes after the drift starts. Other sensors stay `1.0` / `""`. Cancel the drift when done; it clears 1 to 2 minutes later.

### Leak checks (pass 2)

After pass 1 has scored every sensor, pass 2 runs the leak checks on the same window (`detect/core/detectors/leak_*.py`, tunables under `tunables.detectors.leak_*`). They use only trusted sensors: any sensor with `_dq` below `dq_min` (0.5) is left out. If a sensor a check needs is untrusted, that check makes no finding for the pipe; if a supporting sensor is untrusted, the finding is still made and its explanation names the excluded sensor. Each finding names one pipe and carries a confidence (0 to 1), the raw aliases that drove it and a one-sentence explanation with the key numbers.

| Check | Fires when | Confidence | Needs |
| --- | --- | --- | --- |
| `leak_mass_balance` | mass goes missing: `sum(flow_in - flow_out) / sum(flow_in)` over the paired 1 Hz samples of the last `avg_s` (60 s, at least `min_points` 30 paired seconds) is at least `min_frac` (2%). No finding when the mean `flow_in` is below `min_flow_frac` (1%) of the `flow_in` contract span (a pipe barely flowing), or when the `flow_in` and `flow_out` units differ. A matching drop of `pressure_out` (against `pressure_in`, the last 60 s vs the rest of the window, at least `pressure_drop_frac` 0.6%) is cited as corroboration and adds both pressures to the evidence; it never changes the confidence | `frac / full_frac` (8%), capped at 1 | `flow_in`, `flow_out` trusted; `pressure_out` and `pressure_in` optional |
| `leak_methane_rise` | methane around the pipe is well above its peers': the mean of `methane_ppm` over the last `avg_s` (60 s) minus the median of the same mean on the other pipes of its `peer_group` is at least `min_ppm` (30 ppmv). Untrusted peers are left out of the median and named; peers with too little data (fewer than `min_points` samples) are left out and named too; at least 2 usable peers are needed, and all must share the pipe's unit | `excess / full_ppm` (300 ppmv), capped at 1 | `methane_ppm` trusted |

Calibration on the simulator (`tests/test_leak_detectors.py`): normal output never comes close. Over 4 simulated days (24 pipes at 1 Hz, with the trailing 60 s evaluated once a minute, as the runs do) the mean loss fraction peaked at 0.16% (min 2%), methane at 0.6 ppmv above the peer median (min 30 ppmv) and the relative `pressure_out` drop at 0.4% (corroboration 0.6%). No leak finding appears in 4 x 30 consecutive one-minute runs at different times of day and seeds. The default injected leak (10% of the pipe's mean flow, methane rising toward 600 ppmv with a 20 s time constant) scores confidence 1.0 on both checks once it has been in the window for 60 s, and at least 0.8 by the second run after it enters the window.

**Where to see findings.** Every finding joins its pipe's incident (see "Incidents and alert state" below). The run's summary log line also lists pass 2's findings, as `"findings":[{"detector":…,"pipe":…,"confidence":…,"explanation":…,"evidence_aliases":[…]}]` (confidence to 3 decimals; empty when there are none). `"suppressed":[{"detector":…,"pipe":…,"reason":…,"scope":…}]` lists each pipe where a check could not run because a sensor it needs is untrusted (`"scope":"detector"`) or where only some of its features were skipped (`"scope":"feature"`, `outlier_peer`) (for example `flow_out untrusted (dq 0.00 < dq_min 0.5)`), so a silenced check is visible. `detectors` and `ran_ok` list the leak checks too; a leak check that failed (or returned a malformed finding) is logged on its own error line and left out of `ran_ok`.

**If any pass-1 (`dq_*`) check fails in a run, pass 2 is skipped for that run**: the failed check's sensors would score 1.0 and could make a railed sensor look like a leak. The run logs an ERROR line `pass 2 skipped: pass-1 detector(s) failed` naming the failed checks, and the summary shows `"pass2_skipped":true` with no findings. The next run tries again.

```bash
export STAGE=poc-dj   # your stage
aws logs tail /aws/lambda/detection-$STAGE --region ap-southeast-1 --since 10m --format short | grep '"findings":\[{'
```

To try the checks on a developer stage with the simulator on (section 5):

```bash
export STAGE=poc-dj SIM_CONTROL_TABLE=sim-control-poc-dj
uv run python -m ops.inject leak --pipe P-007 --minutes 10     # default rate: 10% of the pipe's mean flow; note its "pk"
```

**Both P-007 findings appear about 2 to 3 minutes after the injection** (the 60 s ingest lag, then about 45 s of leak in the window for confidence 0.8, and runs start on the minute), and no other pipe shows a finding. They clear 2 to 4 minutes after the leak ends (methane decays over about a minute). Cancel with `uv run python -m ops.inject cancel <pk>`. To see the exclusion rule, add `dq_flatline` on P-007 `flow_out` during the leak: after 2 to 3 minutes only `leak_methane_rise` remains.

Known limits:

- The pressure corroboration compares the last 60 s with the rest of the 15-minute window, so a leak older than the window is no longer cited as corroborated (both checks still fire).
- A flow meter drifting low looks like a loss until `dq_drift` marks it untrusted (about 5 minutes). A drift of 2% of flow within that time would raise `leak_mass_balance`.
- A methane rise shared by the whole peer group (weather, a flare) moves the median too and is not reported. For the same reason, leaks on up to 3 pipes of one 6-pipe group are each found, but on 4 or more they hide each other.
- The checks only report; incidents and `alert_state` are described below, and email comes with Story 3.4.

### Outlier check (pass 2)

`outlier_peer` (`detect/core/detectors/outlier_peer.py`, tunables under `tunables.detectors.outlier_peer`) flags a pipe that behaves unlike the other pipes of its `peer_group` (same service, material and age band, 6 pipes per group today), even when no single threshold is crossed. It runs in pass 2 after the leak checks, on the same dq-annotated window, and uses only trusted sensors.

For every pipe it computes four features over the last `avg_s` (60 s, at least `min_points` 30 GOOD seconds; ratios use seconds where both sensors have a value). Temperature and methane are in their sensors' contract units, and the floors below are in those units; ratios are dimensionless:

| Feature | From | Noise floor |
| --- | --- | --- |
| `pressure_ratio` | mean `pressure_out` / mean `pressure_in` | `floor_pressure_ratio` 0.003 |
| `flow_ratio` | mean `flow_out` / mean `flow_in` | `floor_flow_ratio` 0.005 |
| `temperature` | mean `temperature` | `floor_temperature` 2.0 (K today) |
| `methane_ppm` | mean `methane_ppm` | `floor_methane_ppm` 1.0 (ppmv today) |

Each feature is compared with the same feature on the pipe's peers (the other pipes of the group) as a robust z-score: `z = (value - median(peers)) / (1.4826 x MAD(peers) + floor)`. The median and MAD (median absolute deviation) ignore one odd peer, and the floor stops near-identical peers (MAD close to 0) from turning a tiny difference into a huge z. A ratio has no value when the pipe's inlet mean is below `min_den_frac` (1%) of the inlet sensor's contract span (a shut-in or idling pipe, where a tiny inlet would make the ratio explode), when the outlet mean is negative, or when the pipe's own in/out units differ. Ratios are compared across peers whatever their units; temperature and methane only with peers whose contract unit matches. A feature is scored only when at least 4 pipes of the group, the pipe itself included, have a trusted, comparable value for it (AD-14); a pipe without a `peer_group` is not scored. A feature is flagged when `|z|` is above `min_z` (4); the worst flagged feature decides the confidence, `(|z| - min_z) / (full_z - min_z)` (`full_z` 8), capped at 1. A pipe gets at most one finding, and its evidence is the raw aliases behind every flagged feature.

**Reading a finding.** For example, a default leak on P-007 that has been in the window for 2 minutes (simulator physics):

```text
Unlike its peers in fg-cs-10to15 over the last 60 s: methane_ppm 589.1 vs median 1.96 of peers P-008, P-009, P-019, P-020, P-021, z +576.0; flow_ratio (flow_out/flow_in) 0.9011 vs median 1 of peers P-008, P-009, P-019, P-020, P-021, z -19.4.
```

The flagged features are listed worst first, each with the sensors behind it (for a ratio), the pipe's value, the median of its peers, the peers compared and z (the sign says above or below the peers). A peer left out of a feature is named after its peer list with the reason (`P-008 left out: methane_ppm untrusted`, `too little data`, `unit mismatch`, `flow_in too low`). If the pipe's own sensors are untrusted, the features that need them are skipped and the explanation ends with, for example, `pressure_ratio was not scored because pressure_out was excluded as untrusted (dq 0.20 < dq_min 0.5)`, naming every untrusted sensor of the feature; the other features are still scored. Every feature skipped on a pipe also appears in the summary's `suppressed` list, even when no finding is made: `{"detector":"outlier_peer","pipe":"P-007","reason":"pressure_ratio skipped: pressure_out untrusted (dq 0.00 < dq_min 0.5)"}`, or `flow_ratio skipped: flow_in too low`, `methane_ppm skipped: only 3 pipes with data`, `flow_ratio skipped: unit mismatch`, `temperature skipped: too little data` (several reasons are joined with `; `).

Findings go to the run summary line with the leak findings (`"findings":[{"detector":"outlier_peer",…}]`) and into the pipe's incident. On a leak, the outlier finding names the same pipe as the leak findings, so it joins the same incident (one OPEN incident per pipe, AD-6) as another contributing detector rather than raising a second alert.

Calibration on the simulator (`tests/test_outlier_peer.py`): over 6 x 30 consecutive one-minute runs of normal output (seeds 0, 1, 2, 3, 5, different times of day, all 24 pipes) the largest |z| was 2.3 (`pressure_ratio`), 1.5 (`temperature`), 0.5 (`methane_ppm`) and 0.3 (`flow_ratio`), against `min_z` 4. The static steady-state spread between peers dominates `pressure_ratio` and `temperature`; across 300 seeds (steady state plus the daily temperature cycle, without noise) it stays below |z| 2.6 and 1.9 with these floors. A default leak on P-007 scores 0.7 after 5 s in the window and 1.0 from about 10 s, so always by the second run after it enters the window (by then `methane_ppm` is at z 330 or more and `flow_ratio` at |z| 17 or more), and no other pipe of the group is flagged (their largest |z| was 1.9). The leak's `pressure_ratio` shift (about 0.01) is within the normal spread between flare-header peers, so it is rarely one of the flagged features.

On a developer stage, the leak injection in "Leak checks" above also raises `outlier_peer` on P-007 in the same run as the leak findings; no other pipe should show an outlier.

Known limits:

- A peer group with fewer than 4 pipes with trusted data for a feature is not scored on it. With the current 6-pipe groups, 3 or more untrusted pipes stop the check on that feature for that group.
- A change shared by the whole group moves the median too and is not reported (by design: it is not one pipe behaving differently). Masking starts at 4 of 6 pipes: with 3 changed the same way, each changed pipe's 5 peers are 3 normal and 2 changed, so its median is normal and it is flagged (the group is split evenly, so the 3 unchanged pipes are flagged too); with 4 changed, the changed pipes hide each other and the 2 unchanged ones are flagged instead.
- A sensor fault the `dq_*` checks have not yet caught (for example the first minutes of a drift) can make its pipe an outlier until the sensor becomes untrusted.
- Peers differ by design (each pipe has its own steady state), so the floors are set above that spread. A real pipe that has always run a little differently from its peers is not flagged; the check finds changes large compared with normal peer spread, not small standing offsets.

### Incidents and alert state

After both passes, every finding of the run, `dq_*` included, joins its pipe's incident in the DynamoDB table `incidents-<stage>` (`DetectionStack-<stage>`, output `IncidentsTableName`; retained, with point-in-time recovery, for `poc`; deleted with a developer stage). Only `detect/adapters/incidents.py` writes it; the rules are in `detect/core/incident_rules.py` (AD-6, AD-12).

- **One OPEN incident per pipe.** Its slot is the item `pk=<pipe_id>, sk=OPEN`, created only if absent, so two runs can never make two. A later finding on the pipe updates the same incident: `detectors.<id>` gets the latest confidence, explanation and version, `quiet_runs` goes back to 0, the evidence aliases are merged, `evidence_to` moves to the latest window end (`evidence_from` stays at the first). The incident's `confidence` is the highest of its detectors'.
- **Closing.** A detector counts one quiet run when it ran OK in that run (it is in `ran_ok`), was not suppressed as a whole on the pipe (a `suppressed` entry with `"scope":"detector"`; a skipped `outlier_peer` feature, `"scope":"feature"`, does not block it) and made no finding there. When every detector of the incident has `quiet_runs_to_close` (5) quiet runs, one transaction deletes the slot and writes the history item `sk=C#<incident_id>` with `status=CLOSED`, `closed_reason=quiet` and `closed_at`. So a leak's incident closes about 5 minutes after its last finding. A detector that is suppressed on the pipe or fails keeps its count, so the incident stays open while it cannot be checked. When pass 2 is skipped, the `dq_*` checks that ran OK still count; the pass-2 checks do not.
- **Source outage.** When at least half of the facility's raw aliases have no samples in the window (simulator start or stop, an ingest outage), the run logs `{"msg":"source outage",…}` and the summary shows `"source_outage":true`. In that run `dq_gap` findings do not feed incidents and no detector counts a quiet run; the `_dq` / `_dq_fail` values are still written. So a simulator start does not open 24 incidents. Other findings still join as usual.
- **Reopening.** A new finding within `reopen_grace_s` (600 s) of the pipe's latest quiet close restores that incident (same `incident_id`, `notified_at` kept) instead of opening a new one; the `C#` item becomes the `OPEN` slot again. The restored incident keeps its previous detectors, their confidences and its evidence: its `confidence` is still the highest over all its detectors (AD-6, no decay), so it can read higher than the finding that reopened it. Incidents closed by a reset (`closed_reason=reset`, Story 4.2) never reopen.
- **Replays.** Every item carries `last_run` (the run ID) and `config_hash`, and every write is conditional on `last_run` being older than the run. A run whose ID is at or before any stored `last_run` of a pipe writes nothing to that pipe's incidents and does not write that pipe's `alert_state` / `alert_summary` either (they would overwrite the history at that window end with the current state). If a write loses a race twice, the pipe is listed in the summary's `incident_conflicts` and left to the next run.
- **Run order and failures.** Each run writes `_dq` / `_dq_fail`, then the incidents (pipe by pipe), then `alert_state` / `alert_summary`. A DynamoDB error other than a lost condition fails the invocation part way: pipes already updated stay updated, that run's alert values are not written, and the next run (a new run ID) completes the rest.

After the incident step, every run writes two derived properties for every pipe at the window end (AD-13): `derived/alert_state` (INTEGER: the number of detectors in the OPEN incident, `0` if none) and `derived/alert_summary` (STRING, at most 1024 bytes: `{"incident_id":…,"confidence":…,"detectors":[{"id":…,"c":…}],"evidence":[…]}` with confidences to 2 decimals, or `{}`; `evidence` is dropped first if it would not fit). The summary line adds `source_outage`, `incident_ops` (`opened`, `updated`, `closed`, `reopened`: each a list of `{"pipe","incident_id"}`), `incident_conflicts` and `alert_state` (only pipes with an OPEN incident).

A sensor fault opens an incident too (for example `dq_flatline` on one sensor, or `dq_gap` on a sensor that goes silent while most others report), and closes 5 quiet runs after the sensor recovers.

To read the table and a pipe's alert state on a developer stage:

```bash
export STAGE=poc-dj
# The OPEN incidents (one per pipe at most):
aws dynamodb scan --region ap-southeast-1 --table-name incidents-$STAGE \
  --filter-expression 'sk = :o' --expression-attribute-values '{":o":{"S":"OPEN"}}' \
  --query 'Items[].{pipe:pk.S,id:incident_id.S,conf:confidence.N,detectors:detectors.M,last_run:last_run.S}'
# Everything for one pipe (the OPEN slot and its closed history, C#<incident_id>):
aws dynamodb query --region ap-southeast-1 --table-name incidents-$STAGE \
  --key-condition-expression 'pk = :p' --expression-attribute-values '{":p":{"S":"P-007"}}'
# Alert state and summary of P-007 (P-007 is in train1):
for p in alert_state alert_summary; do
  aws iotsitewise get-asset-property-value --region ap-southeast-1 \
    --property-alias "/$STAGE/lng1/train1/P-007/derived/$p" --query 'propertyValue.value' --output json
done
# The run's incident changes:
aws logs tail /aws/lambda/detection-$STAGE --region ap-southeast-1 --since 10m --format short | cut -d' ' -f2- \
  | jq -Rr 'fromjson? | select(.incident_ops) | [.run_id, (.incident_ops|tojson), (.alert_state|tojson), .source_outage] | @tsv'
```

With the leak injection above, P-007 gets one OPEN incident with `leak_mass_balance`, `leak_methane_rise` and `outlier_peer` about 2 to 3 minutes after the injection, and `alert_state` 3. After cancelling, the findings stop within 2 to 4 minutes and the incident closes 5 runs later with `closed_reason` `quiet`; `alert_state` goes back to `0` and `alert_summary` to `{}`.

Known limits:

- There is no reset yet (Story 4.2); to clear incidents on a developer stage, wait for them to close or delete the items by hand.
- If a detector is suppressed on a pipe for a long time (for example a sensor stays untrusted), its entry in that pipe's incident never counts quiet runs, so the incident stays open until the detector can check the pipe again.
- Each run queries every pipe's items (one `Query` per pipe, with its closed history), which is fine for the POC's 24 pipes and short history.

### Operator email

Each incident sends exactly one email (AD-7, Story 3.4). At the end of every run, after the incident and alert writes, `detect/adapters/notifier.py` looks at each pipe's OPEN incident. If it has no `notified_at`, it publishes one message to the SNS topic `operators-<stage>` (`DetectionStack-<stage>`, output `OperatorTopicArn`), then sets `notified_at` on the incident. That write only succeeds if the OPEN slot still holds the same `incident_id` and has no `notified_at` yet (the outbox).

- **No duplicates.** A later run, a detector joining the incident, or a reopen within the grace period (the reopened incident keeps its `notified_at`) sends nothing.
- **Failures.** A failed publish is logged (`"msg":"notify failed"`, `"step":"publish"`), listed in the summary's `notify_failures`, and retried by the next run; it never fails the run. If the publish succeeded but the `notified_at` write lost its condition (someone else set it, or the slot changed), it is listed with `"step":"mark"` and no second email follows. Any other error on that write is listed the same way; that is the one case where the next run can send the email a second time (at least once, never lost).
- **Summary.** The run's summary line adds `notified` (`[{"pipe","incident_id","message_id"}]`, `message_id` from SNS) and `notify_failures` (`[{"pipe","incident_id","step","error"}]`), and `notify_duration_s`.

The email (Email v1): subject `[POC] Pipe <pipe_id> incident <incident_id>`. The body's first line is always the banner `POC - simulated data - evidence only, not an operational alert or official record`, then the pipe and area, the incident confidence (2 decimals), one line per detector (id, confidence, explanation), the contributing sensors (aliases), and the evidence link.

The link (Deep link v1, built only by `detect/adapters/deeplink.py`) opens the stage's pipe-detail dashboard on the incident, in one line: `<GRAFANA_URL>/d/<DASHBOARD_UID>/pipe-detail?orgId=1&var-pipe_id=P-007&var-area=train1&var-evidence=<alias, URL-encoded>&var-evidence=…&from=<evidence_from − 5 min, epoch ms>&to=<evidence_to + 5 min>`. `GRAFANA_URL` is `SharedGrafanaStack`'s workspace URL (a cross-stack reference, so `DetectionStack-<stage>` depends on `SharedGrafanaStack`), and `DASHBOARD_UID` is `pipe-detail` for `poc` or `pipe-detail-<initials>` otherwise, both set by CDK as env vars. `tests/test_deeplink_contract.py` fails if the link and `dashboards/pipe-detail.json` (uid, `pipe_id`, `area`, `alias_root`, multi-value `evidence`) drift apart.

**Recipients.** Only addresses in the CDK context `operator_emails` (a comma-separated list, consultants only, never client operators) are subscribed; it is empty by default, so no one receives email and the published messages are dropped by SNS. The topic policy denies `sns:Subscribe` to every principal except the CDK CloudFormation execution role (`cdk-hnb659fds-cfn-exec-role-<account>-ap-southeast-1`), so nobody can add a subscription by hand or from the console. To add a recipient:

1. Put the list in `cdk.json`, so every deploy (CI included) keeps it: `"context": {"operator_emails": "alice@example.com,bob@example.com"}`. It applies to every stage. For a one-off local deploy of a developer stage you can pass `-c operator_emails=alice@example.com` instead, but the next CI deploy (which does not pass it) removes that subscription again.
2. Deploy the stage. SNS sends each new address a confirmation email ("AWS Notification - Subscription Confirmation"); nothing is delivered until the recipient clicks **Confirm subscription**. Check with:

```bash
aws sns list-subscriptions-by-topic --region ap-southeast-1 \
  --topic-arn "$(aws cloudformation describe-stacks --region ap-southeast-1 --stack-name DetectionStack-$STAGE \
    --query "Stacks[0].Outputs[?OutputKey=='OperatorTopicArn'].OutputValue" --output text)" \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output text   # PendingConfirmation until clicked
```

To remove a recipient, delete the address from the list and deploy. To see what was sent:

```bash
aws logs tail /aws/lambda/detection-$STAGE --region ap-southeast-1 --since 30m --format short | cut -d' ' -f2- \
  | jq -Rr 'fromjson? | select(.notified) | select((.notified|length) + (.notify_failures|length) > 0) | [.run_id, (.notified|tojson), (.notify_failures|tojson)] | @tsv'
```

The reader strategy is the env var `READER_STRATEGY` (`batch_get` or `execute_query`), set by `infra/stacks/detection.py` from `DEFAULT_STRATEGY` in `detect/adapters/reader.py` (`batch_get`, from the AD-18 spike). The function role can read SiteWise, write only to this stage's pipe assets (plus `time-series/*`, as for the simulator), and has no access to `sim_control` or `injection_log` (AD-4, AD-17). On DynamoDB it holds only `Query`, `PutItem`, `DeleteItem`, `TransactWriteItems` and `UpdateItem` (the `notified_at` marker) on `incidents-<stage>` (no `Scan`; the table name reaches the Lambda as `INCIDENTS_TABLE`). IAM cannot restrict writes to `derived/` aliases, and the `time-series/*` grant covers every time series in the account, so IAM does not isolate stages either. Both boundaries are the code guard: `detect/adapters/derived_writer.py` refuses any alias that is not one of this stage's `derived/` aliases before calling SiteWise.

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
