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
- ~23:35 **1.5 built** (`0763396`, 307 tests). **Decision:** run the live `poc-dj` deploy *before* the four-lens review. The plugin field names, the cross-region Identity Center sign-in and the plugin install can only be checked live, so reviewing first would likely mean a second review round, costing tokens.
- **1.5 implementer decisions accepted:**
  - Evidence is shown as a red "Evidence" row, because Grafana cannot restyle panels by variable. Logged as a deviation from the wording "panels highlighted".
  - The overview has one stat panel per pipe, 24 panels, refreshing every 30 s.
- ~23:45 **1.5 live on `poc-dj`** (run 36443979003):
  - Workspace `g-f2edb8f41a` is ACTIVE on Grafana 12.4 with `AWS_SSO`. Cross-region Identity Center (`us-east-1`) is accepted, so the fallback isn't needed. the owner is assigned as ADMIN.
  - Plugin 2.6.3 is installed.
  - Folder, data source and both dashboards are provisioned.
  - A Grafana query for P-007 `flow_in` returned 299 points in 5 min.
  - Browser sign-in by the user is not verified (it needs them).
  - Simulator switched off afterwards.
- ~23:46 **1.5 review decision:** the review diff leaves out `dashboards/*.json` (generated, about 40 KB), because `tests/test_dashboards.py` checks them structurally. This keeps the four-lens review cheaper.
- ~23:50 **1.5 review triaged:** 6 patch groups, 2 deferrals (admin revoke on update; SA pagination test). Rejected edge cases are listed in the plan's Review Triage Log.
- 23:55 **1.5 merged** (PR #3, `7472add`); redeploy of `poc-dj` with the new handler passed (run 36446685141). OG-9 → Done. Started 2.1 on `feat/dq-refresh`.
Note: times before 23:55 are approximate; later entries use `date`.
- 23:56 **2.1 (OG-10) plan approved autonomously** (`plan-2-1-dq-refresh.md`; about 2,700 tokens, kept whole as one deliverable). Decisions:
  - no-sample alias → `0.0` / `dq_gap`, per AD-13 over the story text;
  - `alert_state` and `alert_summary` are not written until 3.3;
  - Lambda as a container image from `uv.lock` (convention plus the simulator pattern);
  - the scheduler follows the `simulator` switch, so detection costs nothing when off;
  - the reader has both strategies behind `READER_STRATEGY`, with the default chosen by the live spike.
- 00:13 **2.1 built** (`5721071`, `b7890c3`; 415 tests). The implementer's own dispatch of the `poc-dj` deploy was refused by its permission check. I run it myself under the user's direct overnight authorization, which already covered this planned spike; this is not a hand-off from the subagent. Implementer decisions: PipeMeta from the bundled `facility.yaml` (not SiteWise attributes); each image excludes the other's code, so a detect change never restarts the simulator (one-time sim image rebuild); Lambda 1024 MB; ExecuteQuery uses a prefix `LIKE`, because a 144-alias `IN` list silently dropped rows.
- 00:28 **2.1 review:** code-only diff (no tests) for Blind Hunter, Edge Case Hunter and Intent Alignment; full diff for Verification Gap. Saves tokens.
- 00:31 **2.1 review triaged:** 9 patch groups; 3 deferrals (skippedEntries test; detector protocol → 2.2; alarms → 4.4); empty `_dq_fail` finding disproved live.
- 00:40 **2.1 spike done in the Lambda:** batch_get 6.4–6.8 s vs execute_query 19.2–22.1 s per full run (129,600 samples); batch_get stays the default; AD-18 resolved. Deployed the reviewed code with simulator=off (detection paused).
- 00:46 **2.1 merged** (PR #4, `cc5ceca`). OG-10 → Done. `poc-dj`: simulator off, detection schedule DISABLED. Started 2.2 on `feat/dq-flatline-stuck`.
- 00:47 **2.2 (OG-11) plan approved autonomously.** Decisions:
  - only the trailing run counts, so recovery clears the finding on the next run;
  - flatline = railed at the contract min or max; stuck = identical values that are *not* railed, so the two never double-fire;
  - confidence = min(1, run_s / full_s), with min_run_s 30 and full_s 60, so faults cross dq_min within two runs;
  - tunables go under `facility.yaml` `detectors.<id>`;
  - the typed detector protocol deferred from 2.1 is added here.
- 00:55 **2.2 built** (`d656f89`, 468 tests). Implementer decisions: run length measured first-to-last sample; rail tolerance is `rel_tol` × contract span, because a min of 0 makes a relative tolerance zero; `dq_stuck` reuses `dq_flatline`'s `rail_of`. Known limit: a real sensor genuinely sitting at 0 (a stopped pipe) is flagged `dq_flatline` by design.
- 00:57 **2.2 decision (from intent audit):** "lose trust within two runs" can't be guaranteed counting from the injection, because each window ends `ingest_lag_s` = 60 s behind real time (AD-9), so run 2's window may not yet contain the fault. I read it as two runs after the fault enters the window; the detectors meet that. In practice a fault is flagged 2–3 minutes after it starts (documented). Not lowering ingest_lag (an architecture value).
- 00:58 **2.2 review triaged:** 9 patch groups, no deferrals; known limit (legitimate shut-in at the contract min) documented, not fixed.
- 01:12 **2.2 live check passed:** P-003 flow_in → 0.0/dq_flatline, P-015 temperature → 0.0/dq_stuck by the first window holding 80 s of fault; the other 142 sensors stayed at 1.0. Injections cancelled; off-deploy with the reviewed code started.
- 01:18 **2.2 merged** (PR #5, `1e5a6ce`). OG-11 → Done. `poc-dj` idle (simulator 0, detection DISABLED). Started 2.3 on `feat/dq-drift-gap`.
- 01:19 **2.3 (OG-12) plan approved autonomously.** Decisions:
  - drift = the slope of a sensor's residual against its peer-group median (a facility detector), so shared process changes don't trigger it;
  - gap = the trailing silence up to window_end; the empty-window rule stays a single source;
  - thresholds are calibrated on simulator output: no false positives, the ops example drift caught within 5 min, a 3-min gap caught, ingest lag never flagged.
- 01:34 **2.3 built** (`a9d7054`, 532 tests). Accepted the implementer's extra `min_r2` = 0.97 straight-line gate for `dq_drift`: rate alone couldn't tell drift from process wander. Trade-offs: drifts slower than ~0.3 K/min are missed; a leak step never counts as drift (keeps Epic 3 evidence clean); fewer false incidents. Also: a sensor with only BAD samples now scores 0.0/dq_gap (previously 1.0).
- 01:36 **2.3 review triaged:** 6 patch groups; deferred an offset-from-peers check (a completed drift clears while the offset remains).
- 01:54 **2.3 live check passed:** P-010 temperature → 0.0/dq_drift after 4.5 min of 0.5 K/min drift; P-020 flow_out → 0.0/dq_gap; the other 142 sensors stayed at 1.0. Slow deploy (ECS scaling 0→1 took ~20 min) but no errors. Injections cancelled; off-deploy started.
- 02:00 **2.3 merged** (PR #6, `e3b3fed`). OG-12 → Done. `poc-dj` idle. Started 2.4 on `feat/dq-overview`.
- 02:01 **2.4 (OG-13) plan approved autonomously.** Decisions: each pipe tile gets a Trust stat panel (six `_dq` values, red below dq_min, with the threshold filled from `tunables.dq_min` at provision time) and a Failing-checks panel (non-empty `_dq_fail`), both linking to pipe-detail. Only the template and its expansion change.
- 02:06 **2.4 built** (`e066876`, 551 tests). Layout: three tiles per pipe, 432 queries per 30 s refresh. Risk to check live: the failing-checks panel selects fields by name.
- 02:19 **2.4 live check passed** through the Grafana API: overview panels return `<m>_dq`/`<m>_dq_fail` fields; the injected P-003 flow_in shows 0 < 0.5 threshold and dq_flatline. Review: 4 patch groups; deferred stale-data indication and a visual readability check by the user.
- 02:27 **2.4 merged** (PR #7, `8ade86c`). OG-13 → Done; **Epic 2 (OG-2) done.** Started 3.1 on `feat/leak-detection`.
- 02:28 **3.1 (OG-14) plan approved autonomously.** Decisions:
  - pass 2 runs on the dq-annotated window;
  - mass balance = trailing mean of (in − out) / in, with `pressure_out` as corroboration only;
  - methane rise = excess over the peer-group median (a facility-kind detector allowed);
  - findings go into the summary log until 3.3 adds incidents;
  - tunables calibrated so there are no false positives over 30-min simulator runs.
- 02:44 **3.1 built** (`15487f6`, 647 tests). Accepted implementer decisions: `min_points` tunables; the pressure drop is measured relative to `pressure_in` (an absolute drop overlapped normal); handler test data given uniform methane. Known limits: a drifting flow meter can raise mass balance before dq_drift catches it (~5 min); 4+ leaking pipes in one peer group mask methane.
- 02:45 **3.1 decision (from review):** if any pass-1 dq check crashes, pass 2 (leak) is skipped that run with an ERROR log and `pass2_skipped`, rather than trusting unscored sensors. Reason: a false leak email is worse for the demo than a missed minute, and the ERROR is visible (and Story 4.4 alarms will catch it). Also added: a `min_flow_frac` guard, and suppressed checks listed in the summary.
- 03:02 **3.1 live check passed:** leak on P-007 → mass_balance 0.69 and methane_rise 0.58 in the first window with 35 s of leak; no other pipe; clean before the leak. Injection cancelled; off-deploy with the reviewed code started.
- 03:11 **3.1 merged** (PR #8, `75dc22c`). OG-14 → Done. Started 3.2 on `feat/peer-outlier`.
- 03:11 **3.2 (OG-15) plan approved autonomously.** Decisions:
  - features are dimensionless or peer-comparable: pressure_out/in, flow_out/in, temperature, methane;
  - robust z against the peer-group median/MAD, with a per-feature noise floor;
  - worst feature drives the confidence;
  - at least 4 trusted pipes are needed;
  - the simulator has no "outlier" fault, so the live check uses a leak (P-007 becomes an outlier).
- 03:33 **3.2 built** (`c33b1d3`, 734 tests). Accepted implementer decisions: ratios as sum/sum over paired GOOD seconds; each pipe compared against the *other* pipes; a feature is scored only with ≥ 4 same-unit values. Note: a leak barely moves pressure_ratio, so outliers come through methane/flow_ratio (within plan). Handler test data given uniform temperature (P-004/P-016 have their own contracts).
- 03:34 **3.2 review triaged:** 9 patch groups; cross-referencing outlier and leak findings deferred to 3.3 (incident grouping).
- 03:51 **3.2 live check passed:** leak on P-007 → outlier_peer P-007 1.0 naming its 5 peers (methane z +136, flow_ratio z −9.9), alongside both leak findings; no other pipe. Off-deploy with the reviewed code started.
- 04:01 **3.2 merged** (PR #9, `801214a`). OG-15 → Done. Started 3.3 on `feat/incidents`.
- 04:02 **3.3 (OG-16) plan approved autonomously.** Decisions:
  - AD-6 and AD-12 implemented exactly; every finding, including dq_*, joins the pipe's incident (per AD-6);
  - "covered pipes" = the detector ran OK and the pipe wasn't suppressed or skipped for it;
  - no detector counts as quiet in a run where pass 2 was skipped;
  - incident rules are pure (core), with I/O in the adapter;
  - `alert_state` and `alert_summary` are written every run after the incident updates.
- 04:18 **3.3 built** (`626e039`, 786 tests). **Decision:** add a source-outage guard. When ≥ 50% of raw aliases have no samples in the window (simulator start or stop, ingest outage), dq_gap findings from that run don't feed incidents (dq still written; logged "source outage"). Otherwise every simulator start would open 24 incidents, and with 3.4 send 24 emails.
- 04:21 **3.3 review triaged:** 12 patch groups plus the outage guard; TTL/archive for closed incidents deferred.
- 04:44 **3.3 live check passed:** a leak on P-007 opened one incident (3 detectors, confidence 1.0) with alert_state 3 plus the summary JSON; after cancel it closed `quiet` and alert_state returned to 0. (My first read script failed on the import path; re-read by hand.)
- 04:53 **3.3 merged** (PR #10, `af7e7a6`). OG-16 → Done. Started 3.4 on `feat/incident-email`.
- 04:54 **3.4 (OG-17) plan approved autonomously.** Decisions:
  - `operator_emails` is empty by default, so no real email or subscription confirmation goes out overnight; the live check verifies the SNS publish and the `notified_at` outbox instead;
  - `mark_notified` lives in the incidents adapter, keeping AD-6's single writer;
  - the topic policy denies Subscribe to everyone except the CDK deploy path;
  - GRAFANA_URL and DASHBOARD_UID come from the Grafana stacks.
- 05:08 **3.4 built** (831 tests). Accepted: DetectionStack now imports the Grafana URL from SharedGrafanaStack; `mark_notified` is an UpdateItem (the grant gains UpdateItem). Known: CI passes no `operator_emails`, so recipients belong in `cdk.json`; a non-condition `notified_at` write failure could resend once (logged).
- 05:10 **3.4 review triaged:** 8 patch groups; delivery documented as at-least-once. Owner to verify a real email and the link click in the morning.
- 05:26 **3.4 live check passed:** leak on P-007 → SNS publish accepted (MessageId) 2.3 min after the leak; notified_at set; no second publish next run. Inbox delivery and the link click left for the owner.
- 05:36 **3.4 merged** (PR #11, `cf72ec2`). OG-17 → Done; **Epic 3 (OG-3) done.** Started 4.1 on `feat/inject-list`. **Decision:** with ~1 h left, 4.1's live check is limited to running `ops inject list` read-only against the `poc-dj` tables; the simulator's effective-end logging is verified by tests only (no simulator redeploy, which takes ~20 min).
- 05:37 **4.1 (OG-18) plan approved autonomously.** Decisions: `list` is read-only, folds cancels into the effective end, active/recent with 60 min default, `--json`; effective_end added to the injection_log row once via a conditional update (planned end_at never changed); the task role gets an attribute-limited UpdateItem.
- 05:45 **4.1:** live read-only `list` showed tonight's 9 injections with correct effective ends. Review triaged: 5 patch groups; decision: retry the effective_end write for 10 min after the effective end, to cover a late first-apply row. Simulator code not deployed tonight (tests only).
- 06:04 **4.1 merged** (PR #12, `6562d97`). OG-18 → Done. **Decision:** no new story started. Story 4.2 (reset) touches detection and the incidents table and couldn't be built, reviewed, live-checked and merged before 06:40; moving to the report.
- 06:05 **Report written** (`overnight-2026-09-29.md`); committing via PR. Run ends.
