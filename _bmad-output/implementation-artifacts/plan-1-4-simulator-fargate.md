---
title: 'Story 1.4: Run the simulator on ECS Fargate'
type: 'feature'
ticket: 'OG-8'
created: '2026-09-28'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: 'ee4936f206c7cae94124a48f71d099200154183c'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
  - '{project-root}/docs/ci-cd.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The simulator runtime (Story 1.2) only runs on a laptop. The demo needs a live 1 Hz stream in AWS for each stage, with nobody's laptop involved (Story 1.4, OG-8).

**Approach:** Package the simulator as a Docker image built from `uv.lock`. Add a per-stage CDK `SimulatorStack-<stage>` with the `sim_control` and `injection_log` tables, a public-subnet VPC with no NAT, and one ECS Fargate task using the AD-17 task role. It deploys through the existing GitHub Actions `deploy` workflow. The first real deploy of a developer stage confirms the stream and settles the two IAM questions.

## Boundaries & Constraints

**Always:**
- The stack is added for every stage by `infra/app.py`, named `SimulatorStack-<stage>`, and depends on `SiteWiseAssetsStack-<stage>`. Deploys still target `*-<stage>`, so the existing deploy target picks it up.
- Tables `sim-control-<stage>` and `injection-log-<stage>`: partition key `pk` (string), no sort key, on-demand billing. `RETAIN` for `poc`, `DESTROY` for `poc-<initials>`.
- Task role, AD-17 as far as IAM allows:
  - `iotsitewise:BatchPutAssetPropertyValue` and `iotsitewise:DescribeTimeSeries` on this stage's pipe asset ARNs only;
  - `dynamodb:Scan` and `dynamodb:GetItem` on `sim_control`;
  - `dynamodb:PutItem` on `injection_log`;
  - nothing else. The execution role is separate: ECR pull and logs only.
- Decision (2026-09-28, AWS service reference): IAM cannot scope `BatchPutAssetPropertyValue` by alias. No action supports the `iotsitewise:propertyAlias` condition key. The code guard in `sim/writer.py` stays the raw/derived boundary, and the spine records this against AD-17.
- The image is `python:3.14-slim` with deps from `uv.lock`: runtime only, no `dev` or `infra` groups. It is built for `linux/amd64`, and the task runs on X86_64. It runs `python -m sim.main` with `STAGE`, `SIM_CONTROL_TABLE` and `INJECTION_LOG_TABLE` set by CDK.
- Networking: a VPC with public subnets only and no NAT gateway; the task gets a public IP; the security group allows no ingress.
- Decision (human, 2026-09-28): the simulator runs only while testing. CDK context `simulator` (`on` default, or `off`) sets the service's desired count to 1 or 0; nothing else changes, so tables and history survive. `deploy.yml` gets a required `simulator` choice input (`on`/`off`, default `on`), passed as `-c "simulator=$SIMULATOR"` through `env:`. Any other value fails synth.
- One task at most (desired count 1 when on); a circuit breaker with rollback. Logs go to a stage-named log group with 1-week retention.

**Never:**
- No change to the simulator's behaviour, the facility model, or the SiteWise asset stack's resources.
- No detection, Grafana, reset, smoke or alarm work.
- No deploy of `poc` in this story. Verification deploys a developer stage only.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Synth dev stage | `cdk synth -c stage=poc-dj` | `SiteWiseAssetsStack-poc-dj` and `SimulatorStack-poc-dj` | no error |
| Table names | any stage | `sim-control-<stage>`, `injection-log-<stage>`, key `pk` (S) | no error |
| Task env | any stage | the container gets `STAGE` and both table names | no error |
| Task grants | template | only the actions above, only on this stage's pipe assets and tables | no error |
| Removal policy | `poc` vs `poc-dj` | tables `Retain` vs `Delete` | no error |
| Simulator off | `-c simulator=off` | service desired count 0; tables unchanged | no error |
| Bad switch | `-c simulator=maybe` | synth fails with a message | non-zero exit |
| Image contents | `docker build` | no `pytest` or `aws-cdk-lib` installed; `python -m sim.main` with no env exits 2 | no error |
| Live stream | dispatch deploy `poc-<initials>` | within 2 minutes, fresh values on every raw alias | a failed deploy fails the run |
| Seed and alias writes | first real deploy | the seed Lambda and the task both write without `AccessDenied` | if denied, record it and add the `time-series` resource grant |

</frozen-after-approval>

## Code Map

- `sim/main.py` -- entry `python -m sim.main`. `settings_from_env` (57-79) requires `STAGE`, `SIM_CONTROL_TABLE`, `INJECTION_LOG_TABLE`; optional `FLUSH_S`, `POLL_S`, `SIM_SEED`, `FACILITY_PATH` (defaults to bundled `facility/facility.yaml`). Exit 2 = config error, 3 = startup refusal. It calls `DescribeTimeSeries` for every raw alias at startup (86-102) and handles SIGTERM with a final flush. Do not change.
- `sim/control.py` -- `sim_control` read by paginated `scan(ConsistentRead=True)`; `injection_log` `put_item` with `attribute_not_exists(pk)`. Key `pk` (S) for both.
- `sim/writer.py` -- `batch_put_asset_property_value` by `propertyAlias`; refuses `derived`/`attr` (34, 50-57). This is the AD-17 guard.
- `infra/stacks/sitewise_assets.py` -- `SiteWiseAssetsStack` exposes `facility` and `pipe_assets: dict[str, CfnAsset]` (92-103); use `attr_asset_arn` for grants, as `infra/stacks/seed_attributes.py:80-85` does.
- `infra/app.py` -- `build_app` adds stacks per stage (24-34); `GitHubOidcStack` only with `oidc=true`. Add the simulator stack after the SiteWise stack.
- `tests/test_sitewise_stack.py` -- `synth(stage)` idiom and the `resources(tpl, type_)` helper. `tests/test_github_oidc_stack.py:127` asserts the exact stack set for `poc`, so update it to include `SimulatorStack-poc`.
- `pyproject.toml` -- runtime deps boto3, python-ulid, pyyaml; groups `infra` and `dev` are default groups, so the image must pass `--no-default-groups`; `package = false`.
- `.github/workflows/deploy.yml` -- deploys `"*-$STAGE"`; the runners have Docker, and the deploy role can assume the image-publishing bootstrap role. Only the `simulator` input is added.
- Versions: base `python:3.14-slim`; uv from `ghcr.io/astral-sh/uv:0.12.19` (latest release, checked 2026-09-28).

## Tasks & Acceptance

**Execution:**
- [x] `sim/Dockerfile` and `.dockerignore` -- multi-stage or single stage; copy uv from the pinned image; `uv sync --locked --no-default-groups --no-install-project`; copy only `sim/`, `facility/` and the lock files; non-root user; `CMD ["python", "-m", "sim.main"]`. `.dockerignore` excludes `.venv`, `.git`, `cdk.out`, `_bmad*`, `.work`, `tests`. -- reproducible image.
- [x] `infra/stacks/simulator.py` -- `SimulatorStack(scope, id, *, facility, pipe_asset_arns, **kwargs)`: the two tables, VPC (2 AZs, public subnets only, `nat_gateways=0`), cluster, `DockerImageAsset` (platform `LINUX_AMD64`), Fargate task definition (0.25 vCPU / 0.5 GB, X86_64), service (desired 1, public IP, circuit breaker with rollback), log group (1 week), the task role grants above. Outputs the table names and the cluster and service names. -- the stack.
- [x] `infra/app.py` -- add `SimulatorStack-<stage>`, passing the SiteWise stack's pipe asset ARNs; add an explicit dependency. -- per-stage wiring.
- [x] `.github/workflows/deploy.yml`, `tests/test_workflows.py`, `docs/ci-cd.md` -- add the `simulator` choice input (on/off, default on) and pass it to `cdk deploy` via `env:`; test the input and the command; document how to stop and start the simulator (and the 30-minute warm-up). -- on/off switch.
- [x] `tests/test_simulator_stack.py` -- template assertions for every matrix row that synth can show: table names, keys, billing and removal policy per stage; container env; the task role policy exactly as listed, resources limited to this stage's pipe assets and tables; the execution role separate; no NAT gateway; security group with no ingress; desired count 1 by default, 0 with `simulator=off`, and synth failing on any other value. -- matrix.
- [x] `tests/test_github_oidc_stack.py` -- update the exact stack-set assertions to include `SimulatorStack-<stage>`. -- keep existing tests true.
- [x] `tests/test_sim_image.py` -- static checks on `sim/Dockerfile`: pinned uv image, `--locked --no-default-groups`, the `CMD`, non-root `USER`. -- guards the image recipe without Docker.
- [x] `_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md` -- AD-17: replace the `[ASSUMPTION]` with the finding; Stack: drop `[ASSUMPTION]` on Fargate and add the uv image version; Open Questions: resolve the alias-scoping and Fargate rows. -- keep the spine true.
- [x] `_bmad-output/implementation-artifacts/deferred-work.md` -- no edits to existing entries; the implementer only appends new deferrals if any arise.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass, including the new ones.
- Given `cdk synth -c stage=poc-dj`, then it exits 0 and lists `SiteWiseAssetsStack-poc-dj` and `SimulatorStack-poc-dj`.
- Given `docker build --platform linux/amd64 -f sim/Dockerfile .`, then the image builds, `pip list` inside it shows no `pytest` or `aws-cdk-lib`, and `python -m sim.main` with no env exits 2.

## Implementation Notes

- Image: uv syncs into the image's own Python (`UV_PROJECT_ENVIRONMENT=/usr/local`, `--inexact` so the base image's `pip` stays), so `pip list` shows exactly the runtime set: boto3, botocore, jmespath, python-dateutil, python-ulid, PyYAML, s3transfer, six, urllib3, pip. User `sim` (uid 10001).
- `.dockerignore` is an allowlist (`*`, then `!pyproject.toml !uv.lock !facility !sim`) and also lists the plan's exclusions. The CDK asset fingerprint uses it, so the staged context is only those files and infra-only changes do not rebuild the image.
- `simulator` context is read inside `SimulatorStack` (`desired_count`); any value other than absent/`on`/`off` raises `ValueError` and synth exits 1.
- Service: `MaximumPercent=100`, `MinimumHealthyPercent=0`, so a redeploy stops the old task before starting the new one and two tasks never write the same aliases (AD-3).
- Log group `/ecs/simulator-<stage>` is `DESTROY` for every stage (the plan fixes removal policy only for tables). Task role is named `simulator-task-<stage>`.
- The pipe asset ARNs cross stacks as CloudFormation exports (CDK strong references; synth warns that no reference strength is configured).
- Spine: besides the listed AD-17, Stack and Open Questions edits, AD-3's "IAM enforces both (AD-17)" was reworded so it no longer contradicts the AD-17 finding.
- Stack uses `add_stack_dependency` (`add_dependency` is deprecated in aws-cdk-lib 2.271.0).

## Plan Change Log

- 2026-09-28 (review): the task role grants only `dynamodb:Scan` on `sim_control`, not `GetItem`; the simulator only scans (`sim/control.py`). The frozen Boundaries line still lists `GetItem`; it is left as written for the human to renegotiate.
- 2026-09-28 (review): `ci.yml` gains an `image` job (no AWS) that builds `sim/Dockerfile` and smoke-imports `sim.main` and the facility model.

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 6, low 13, false 2, maybe-false 1. Outcome: 7 patches, 2 deferrals, no loopback.

| Id | Finding | Verdict | Route | Evidence |
|----|---------|---------|-------|----------|
| VG-1 | Image never built or run before deploy | medium | patch | `test_sim_image.py` reads text only; `ci.yml` synth does not build the image. Fix: CI `image` job with build and import smoke. |
| BH-7 | Same | medium | patch | Duplicate of VG-1. |
| IA-4 | Image-contents row checked only statically | medium | patch | Duplicate of VG-1 (the implementer's manual docker check is not repeatable). |
| BH-1 | AD-17 table still lists `derived/*` under "Denied explicitly" | medium | patch | Contradicts the finding that IAM cannot express it. |
| BH-5 | 1 Hz open question marked resolved before any deploy | medium | patch | The row's own text defers confirmation to the first `poc-<initials>` deploy. |
| IA-1 | Runbook states unobserved stream behaviour | medium | patch | Same root cause as BH-5. |
| BH-9 | One VPC per stage; region quota 5 | medium | patch | Verified 2026-09-28: quota 5.0, 1 default VPC; `poc` + 4 developer stages would fail. Doc fix: quota note. |
| VG-2 | Task-role grants never checked against real calls | maybe-false | patch (doc) | Only a real deploy settles it (plan manual check). Fix: runbook AccessDenied fallback (`time-series/*`). |
| BH-6 / IA-3 / ECH-4 | Same IAM runtime question | maybe-false | patch (doc) | Duplicates of VG-2. |
| VG-o1 | `dynamodb:GetItem` granted but unused | low | patch | `grep` finds no `get_item` in `sim/`; deletion. |
| VG-o2 / BH-8 | `.dockerignore` test ignores the allowlist | low | patch | Test checks only denylist entries. |
| BH-12 | Runbook check command lacks region and alias example | low | patch | Direct doc fix. |
| BH-10 / ECH-2 | `poc` tables retained under fixed names block redeploy after a stack delete | low | patch (doc) | Real only after a manual delete; runbook recovery note. |
| BH-2 / ECH-3 | No simulator health alarm; `off` would trip "running tasks < 1" | medium | defer | Alarms are Story 4.4 (OG-21); noted there. |
| ECH-1 | Cross-stack exports block pipe replacement | medium | defer | carried: already appended to `deferred-work.md` by the implementer. |
| BH-3 | `.memlog.md` CI/CD entries now wrong | low | defer | File was modified before this story (session start); belongs to the architecture memlog owner. |
| BH-4 | CI `poc` diff and default deploys ignore the switch | low | reject | The default `on` is the human's design; a false diff is only noise. |
| BH-11 | uv binary left in the runtime image | low | reject | Harmless; the fix changes the build structure. |
| BH-11b | Redeploy gap not documented | low | reject | One task at a time is intended (AD-3 no duplicate writers). |
| ECH-5 | Dockerfile test parser edge cases | low | reject | Test-helper robustness only. |
| IA-2 | Alias-scoping settled by documentation, not deploy | false | reject | The AWS service reference is authoritative; the deploy cannot show otherwise. |
| IA-5 | "Deploys through the existing workflow" unexercised | false | reject | Covered by the plan's manual deploy check, which is pending approval. |

## Design Notes

- The task role gets no `derived/*` deny, because IAM cannot express one (see Boundaries). A reviewer should not flag the missing deny. The code guard and its unit tests are the control.
- `DescribeTimeSeries` by alias is authorised against the associated asset, which is why the grants use pipe asset ARNs. If the first deploy shows `AccessDenied` on either action, add `arn:aws:iotsitewise:ap-southeast-1:<account>:time-series/*` to that action and record it. That is the matrix row "Seed and alias writes".
- Only the image-publishing bootstrap role and Docker are needed in CI. `cdk synth` in `ci.yml` does not build the image.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0; both stacks in `cdk.out`.
- `docker build --platform linux/amd64 -f sim/Dockerfile -t ongiiot-sim:test . && docker run --rm --platform linux/amd64 ongiiot-sim:test pip list && docker run --rm --platform linux/amd64 ongiiot-sim:test; echo $?` -- expected: no pytest or aws-cdk-lib; last exit code 2.

**Manual checks (before merging, with the human's go-ahead):**
- Dispatch `deploy.yml` with `stage=poc-<initials>` from the branch. Within 2 minutes, `aws iotsitewise get-asset-property-value --property-alias <raw alias>` shows a fresh timestamp for sampled aliases on both areas, and the task log shows no `AccessDenied`. Record the outcome in the plan and on OG-8.
