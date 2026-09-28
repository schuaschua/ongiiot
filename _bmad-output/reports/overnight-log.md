# Overnight run log (2026-09-28 night → 2026-09-29 07:00)

Running log for the 7am report. Each entry records one decision made on my own and its reason, or one state change.

## Mandate (user-confirmed 2026-09-28 ~23:20)

- Work autonomously until 7am and make the decisions myself; if blocked, switch to other epics or stories.
- Limits:
  - AWS: deploy `poc-dj` only, never `poc`; the simulator stays off except during tests; no deleting data, tables or shared stacks; no new paid services beyond Grafana's single Admin seat.
  - Git: one branch and PR per story, merged only after green CI and a triaged review; no force-push; no change to CI trust or Identity Center users.
  - Plan checkpoints: take the recommended option and log it here; skip and defer anything that conflicts with the architecture or can't be undone.
  - Jira: update at every stage, per `CLAUDE.md`.
- Report: `_bmad-output/reports/overnight-2026-09-29.md`, committed.
- One story at a time (user, ~23:30): no parallel story work; start another story only when the current one is completely blocked.

## State at start

- Done and merged: 1.1 (OG-5), 1.2 (OG-6), 1.3 (OG-7, PR #1), 1.4 (OG-8, PR #2).
- In progress: 1.5 Grafana (OG-9) on `feat/grafana`. The plan was approved by the user, with three decisions of theirs: a shared workspace stack, provisioning inside the CDK deploy, the owner as the only Admin. The build was delegated at ~23:20.
- AWS:
  - deployed: `GitHubOidcStack`, `SharedNetworkStack`, `SiteWiseAssetsStack-poc-dj`, `SimulatorStack-poc-dj` (simulator off);
  - `poc` has never been deployed.

## Log
- 23:25 **Story 2.1 (OG-10) prep, decision:** the story's acceptance criteria say "write `_dq = 1.0` for every sensor", but AD-13 says `dq_score` scores an alias with no samples in the window as `0.0` with `dq_gap`. I follow AD-13, because the architecture binds and the scoring rule lives in `dq_score.py`, which 2.1 creates. Story 2.3 then only adds the `dq_gap` injection test and the ingest-lag guard. Branch and plan are written after 1.5 merges, to avoid conflicts in `infra/app.py`.
- 23:50 **1.5 built** (`0763396`, 307 tests). **Decision:** run the live `poc-dj` deploy *before* the four-lens review. The plugin field names, the cross-region Identity Center sign-in and the plugin install can only be checked live, so reviewing first would likely mean a second review round, costing tokens.
- **1.5 implementer decisions accepted:**
  - Evidence is shown as a red "Evidence" row, because Grafana cannot restyle panels by variable. Logged as a deviation from the wording "panels highlighted".
  - The overview has one stat panel per pipe, 24 panels, refreshing every 30 s.
- 00:05 **1.5 live on `poc-dj`** (run 36443979003):
  - Workspace `g-f2edb8f41a` is ACTIVE on Grafana 12.4 with `AWS_SSO`. Cross-region Identity Center (`us-east-1`) is accepted, so the fallback isn't needed. the owner is assigned as ADMIN.
  - Plugin 2.6.3 is installed.
  - Folder, data source and both dashboards are provisioned.
  - A Grafana query for P-007 `flow_in` returned 299 points in 5 min.
  - Browser sign-in by the user is not verified (it needs them).
  - Simulator switched off afterwards.
- 00:07 **1.5 review decision:** the review diff leaves out `dashboards/*.json` (generated, about 40 KB), because `tests/test_dashboards.py` checks them structurally. This keeps the four-lens review cheaper.
- 00:20 **1.5 review triaged:** 6 patch groups, 2 deferrals (admin revoke on update; SA pagination test). Rejected edge cases are listed in the plan's Review Triage Log.
