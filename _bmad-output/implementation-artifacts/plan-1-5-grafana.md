---
title: 'Story 1.5: Watch the live stream in Grafana'
type: 'feature'
ticket: 'OG-9'
created: '2026-09-28'
status: 'in-progress'
route: 'full'
route_source: 'auto'
review: ''
review_source: ''
lenses_ran: []
review_loop_iteration: 0
baseline_revision: 'fcf5a1c344fca26b81a2ec3111632e58742149c9'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
  - '{project-root}/docs/ci-cd.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The live stream is in SiteWise, but nobody can see it without the AWS CLI. Integrity engineers need a Grafana view of any pipe's raw trends now. Later, alert emails will link straight to the evidence (Story 1.5, OG-9; UX-DR1, UX-DR2; AD-8, AD-16).

**Approach:** An Amazon Managed Grafana workspace (Grafana 12.4) with IAM Identity Center sign-in and read-only SiteWise access. Each stage provisions its own folder, SiteWise data source and two dashboards from JSON in `dashboards/`: `pipe-detail` (AD-8 variables) and `facility-overview` (base layer). Provisioning is code, not clicks, and runs as part of the stage deploy.

## Boundaries & Constraints

**Always:**
- Workspace: Grafana 12.4, authentication `AWS_SSO`, service-managed permissions with data source `SITEWISE` only. The workspace role holds `AWSIoTSiteWiseReadOnlyAccess` and nothing else (AD-17). IAM Identity Center is the existing organization instance in `us-east-1`; the workspace is in `ap-southeast-1`.
- Per stage, provisioned through the Grafana HTTP API with a short-lived service-account token:
  - folder `ongiiot-<stage>`;
  - SiteWise data source `sitewise-<stage>` (region `ap-southeast-1`, workspace IAM role);
  - dashboards with uid `pipe-detail` for `poc` or `pipe-detail-<initials>`, and `facility-overview` / `facility-overview-<initials>`.

  Re-running is idempotent: it updates in place and never duplicates.
- `dashboards/pipe-detail.json` (AD-8, UX-DR2):
  - textbox variables `pipe_id` (value `P-xxx`) and `area`;
  - constant `alias_root`, set per stage at provision time from `facility/model.py`;
  - multi-value `evidence`;
  - six raw trend panels queried by alias `${alias_root}/${area}/${pipe_id}/<measurement>`, with evidence sensors highlighted.
- `dashboards/facility-overview.json` (UX-DR1 base layer): lists all pipes with their latest raw values; clicking a pipe opens `pipe-detail` for it.
- The SiteWise plugin version is pinned (2.6.3 per the spine, or the version the workspace ships if pinning is not possible; record which).
- `GRAFANA_URL` and the dashboard uid are exposed as stack outputs for the detection Lambda (Epic 3).
- Decisions (human, 2026-09-28):
  - **Workspace:** one shared workspace in `SharedGrafanaStack` (fixed name, like `SharedNetworkStack`). Any stage deploy creates or reuses it, and deleting a stage leaves it in place. AD-16's "owned by the `poc` stage" is amended.
  - **Provisioning:** per-stage provisioning runs inside `cdk deploy` as a custom resource. A Lambda mints a short-lived service-account token and calls the Grafana HTTP API. The CI deploy role and trust are unchanged. AD-16's "`grafana_provision.py` after `cdk deploy`" is amended to "during `cdk deploy`".
  - **Access:** only `owner@example.com` (Identity Center user `00000000-0000-0000-0000-000000000000`) is assigned, as Admin. Others are added later.

**Never:**
- No detection, alert-state panels beyond placeholders, email or deep-link code (Epic 3).
- No hand-built Grafana state, and no long-lived API keys stored anywhere.
- No change to the simulator, SiteWise asset stack behaviour or CI trust.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Sign in | a consultant assigned in Identity Center | reaches the workspace with the assigned role | unassigned user is refused |
| Pipe detail | `pipe_id=P-007`, `area=train1` on stage `poc-dj` | six trends from `/poc-dj/lng1/train1/P-007/*` | unknown pipe shows empty panels, no error |
| Evidence | `evidence=flow_in,methane_ppm` | those two panels highlighted | none |
| Overview | open `facility-overview-dj` | 24 pipes with latest values; click opens `pipe-detail-dj` for that pipe | none |
| Re-provision | deploy the same stage twice | same folder, data source and dashboards, updated in place | none |
| Two stages | `poc` and `poc-dj` both provisioned | separate folders, data sources and uids; no cross-stage aliases | none |
| Cross-region SSO | workspace in `ap-southeast-1`, Identity Center in `us-east-1` | sign-in works | if not, record it and fall back to self-hosted Grafana with the same dashboards |

</frozen-after-approval>

## Code Map

- `facility/model.py` -- `load(stage=)`, `Facility.alias_root` (104), `alias(pipe_id, m)` (126) → `<alias_root>/<area>/<pipe_id>/<m>`; pipes carry `area`. Use these for the `alias_root` constant and the overview's pipe list. Do not change.
- `infra/app.py` -- per-stage wiring; shared stacks (`SharedNetworkStack`) are always added once. Follow that pattern if Open Question 1 → (a).
- `infra/stacks/simulator.py`, `infra/stacks/network.py` -- style for stacks, outputs and exact-policy tests (`tests/test_simulator_stack.py`).
- `infra/lambdas/seed_attributes/` + `infra/stacks/seed_attributes.py` -- the existing custom-resource pattern (provider, stage-named log groups, 4 KB response limit). Reuse it if Open Question 2 → (a).
- `.github/workflows/deploy.yml` -- deploys `"*-$STAGE"`; CDK deploys dependencies automatically.
- Account facts (2026-09-28): Identity Center organization instance `ssoins-7223813109c6d7dd` in `us-east-1`, identity store `d-9067c9384e`; no Grafana workspaces yet; versions offered 9.4, 10.4, 12.4.
- Deferred item carried in: Grafana provisioning on the CI deploy path (`deferred-work.md`, from Story 1.3).

## Tasks & Acceptance

**Execution:**
- [x] `infra/stacks/grafana.py` -- `SharedGrafanaStack`:
  - `AWS::Grafana::Workspace` (Grafana 12.4, `AWS_SSO`, account access, `SERVICE_MANAGED` permissions, data source `SITEWISE`, plugin admin enabled);
  - a workspace role holding only `AWSIoTSiteWiseReadOnlyAccess`;
  - admin assignment for the one user through a small custom resource calling `grafana:UpdatePermissions` (CloudFormation has no property for it);
  - outputs: workspace ID and URL.

  -- shared workspace.
- [x] `infra/lambdas/grafana_provision/handler.py` + `infra/stacks/grafana_provision.py` -- a per-stage custom resource, in the style of `SeedAttributes`:
  - on create and update:
    - mint a service account and a token with a TTL of 15 minutes or less;
    - install or pin the SiteWise plugin;
    - upsert the folder `ongiiot-<stage>`, the data source `sitewise-<stage>` and both dashboards, with uids suffixed per stage and `alias_root` set from `facility/model.py`;
    - delete the token.
  - on delete: remove this stage's folder, dashboards and data source.
  - Keep responses under 4 KB. The Lambda role gets only the Grafana service-account actions on this workspace.

  Wire it into a per-stage stack (its own `GrafanaStack-<stage>`, or `SiteWiseAssetsStack-<stage>`, whichever keeps dependencies simplest), depending on `SharedGrafanaStack`. -- provisioning as code.
- [x] `dashboards/pipe-detail.json`, `dashboards/facility-overview.json` -- as in Boundaries. Queries use the SiteWise data source by alias. Store them as templates (placeholders for stage suffix, data source uid and `alias_root`) that the handler fills. -- UX-DR1, UX-DR2.
- [x] `infra/app.py` -- add `SharedGrafanaStack` once and the per-stage provisioning with its dependencies. -- wiring.
- [x] `tests/test_grafana_stack.py`, `tests/test_grafana_provision.py`, `tests/test_dashboards.py`:
  - template assertions: workspace settings, the role's single managed policy, the Lambda role scoped to the workspace;
  - handler unit tests with a fake Grafana API: upsert idempotency, per-stage uids and folder, delete path, token always deleted, response size;
  - dashboard JSON: uid and variable names per AD-8 (`pipe_id`, `area`, `alias_root`, multi-value `evidence`), six panels over the six measurements, overview links to `pipe-detail`.

  Update the exact stack-set assertions. -- matrix.
- [x] `docs/ci-cd.md` -- a Grafana section: URL output, sign-in through Identity Center, how to add a user, the per-stage folders. -- operability.
- [x] `ARCHITECTURE-SPINE.md` -- amend AD-16 (shared workspace, provisioning during deploy); resolve the Identity Center open question; mark the Grafana provisioning entry in `deferred-work.md` as addressed by appending a note (do not edit existing entries). -- keep the spine true.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given `cdk synth -c stage=poc-dj`, then it exits 0 with `SharedGrafanaStack` and the `poc-dj` provisioning resource.
- Given a live `poc-dj` deploy, then the matrix rows "Sign in", "Pipe detail", "Overview" and "Re-provision" hold (checked by hand with the human's go-ahead).

## Implementation Notes

- **Stacks:** `SharedGrafanaStack` (`infra/stacks/grafana.py`) and a separate per-stage `GrafanaStack-<stage>` (`infra/stacks/grafana_provision.py`) that depends only on it. `deploy.yml`'s `"*-<stage>"` matches `GrafanaStack-<stage>` and CDK pulls in `SharedGrafanaStack` as a dependency, as with `SharedNetworkStack`.
- **Admin assignment:** an `AwsCustomResource` calling `grafana:UpdatePermissions` (ADD on create/update, REVOKE on delete) for `ADMIN_USER_IDS`. Its role also holds the Identity Center actions `UpdatePermissions` needs for SSO users (the `AWSGrafanaWorkspacePermissionManagementV2` set, `Resource: *` since they have no resource-level scoping). `UpdatePermissions` reports per-user failures in its `errors` field with HTTP 200, so a failed assignment does not fail the deploy; check sign-in by hand.
- **Plugin pin:** the handler requests 2.6.3 via `POST /api/plugins/grafana-iot-sitewise-datasource/install` when a different version runs. If Managed Grafana refuses, the shipped version stays; the version actually running is the `SiteWisePluginVersion` stack output (`PluginPinned` in the resource data). Which one applies is recorded at the first live deploy.
- **Data source:** `authType: ec2_iam_role` (Managed Grafana's "Workspace IAM role"), `defaultRegion: ap-southeast-1`.
- **Templates:** placeholders `__DS_UID__`, `__ALIAS_ROOT__`, `__UID_SUFFIX__`, `__STAGE__`; each file keeps its base uid (`pipe-detail`, `facility-overview`, so AD-8's contract test can read it) and the handler appends the stage suffix. Templates and the pipe list travel as JSON-string custom resource properties, so any change to them triggers an Update.
- **Evidence (AD-8):** `evidence` is a custom multi-value variable whose options show the measurement name and carry the pipe's full raw alias (`flow_in : ${alias_root}/${area}/${pipe_id}/flow_in`), so Deep link v1 (aliases) matches by value and a URL with measurement names (`var-evidence=flow_in`) matches by text. Highlighting is an "Evidence" row at the top: one red, thick raw trend repeated per selected alias. Grafana has no per-panel conditional styling on a variable, so the six fixed trend panels themselves are not restyled.
- **Overview:** the template holds a row prototype (`__AREA__`) and a pipe prototype (`__PIPE_ID__`); the handler clones one row per area and one stat panel per pipe (latest value of the six aliases, `PropertyValue`), each linking to `pipe-detail[-<initials>]` with `var-pipe_id` and `var-area`. Refresh 30 s (144 latest-value queries per refresh).
- **Service account:** one per stage (`ongiiot-provision-<stage>`, Admin), reused across runs; tokens (900 s) are deleted in a `finally`. Delete removes the stage's dashboards, data source, folder and then the service account.

## Plan Change Log

- 2026-09-28: Open Questions 1 and 2 were already decided by the human (Boundaries, Decisions): shared `SharedGrafanaStack`, provisioning as a custom resource during `cdk deploy`.

## Review Triage Log

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.

**Manual checks (with the human's go-ahead):**
- Deploy `poc-dj` with the simulator on. Sign in through Identity Center, open `pipe-detail-dj` for P-007 train1 and `facility-overview-dj`, and see live trends. Then switch the simulator off.
