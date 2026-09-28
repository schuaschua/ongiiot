# Overnight report: 2026-09-28 23:20 → 2026-09-29 06:10 (+08)

Autonomous run under your mandate. The limits were:
- deploy to `poc-dj` only, never `poc`;
- simulator off except during tests;
- merge only after green CI and a triaged four-lens review;
- one story at a time;
- Jira updated at every stage;
- every decision logged.

The running decision log is in `_bmad-output/reports/overnight-log.md`, and each story's plan file has its own review triage and live evidence.

## Summary

- **Stories finished:** 9 (1.5, 2.1–2.4, 3.1–3.4, 4.1), plus the end of 1.4 before you left. Each one was planned, built, reviewed by four lenses, fixed, **checked live on `poc-dj`**, merged (PRs #3–#12) and closed in Jira.
- **Epics done:** Epic 1 (all stories), Epic 2 and Epic 3. Epic 4 is In Progress, with 4.1 done.
- **Tests:** 856 passing (from 262 at the start of the night). CI on `main` is green.
- **The full chain works end to end on `poc-dj`:** simulated stream → trust scores → leak and outlier detection → one incident per pipe → alert state in SiteWise and Grafana → operator email published with an evidence link.
- **AWS now:** everything idle (simulator 0 tasks, detection schedule DISABLED). `poc` has never been deployed.

## Per story

### 1.5 Grafana (OG-9) · PR #3
- **Done:**
  - one shared Managed Grafana 12.4 workspace, `g-f2edb8f41a`;
  - per-stage folder, SiteWise data source (plugin 2.6.3) and the `pipe-detail` and `facility-overview` dashboards, provisioned during `cdk deploy` by a custom resource.
- **Your decisions:** shared workspace; provisioning inside the deploy; only you, as Admin.
- **My decisions:**
  - deployed live before the review, to catch real API issues once;
  - left the generated dashboard JSON out of the review diff (token cost);
  - accepted evidence highlighting as a red "Evidence" row, because Grafana can't restyle panels by variable.
- **Live:** workspace ACTIVE; cross-region Identity Center (us-east-1) works; you are ADMIN; a query returned 1 Hz data.
- **Deferred:** revoking removed admins on update (manual REVOKE for now, documented); a service-account pagination test.

### 2.1 Trust refresh every minute (OG-10) · PR #4
- **Done:** the `detect/` package (pure core plus adapters), a container-image Lambda built from `uv.lock`, and an EventBridge Scheduler job every minute, single-flight (AD-12).
- **My decisions:**
  - a sensor with no samples scores 0.0 / `dq_gap`, following AD-13 rather than the story's "1.0 for every sensor";
  - `alert_state` and `alert_summary` left to 3.3;
  - **the detection schedule follows the `simulator` on/off switch**, so it costs nothing when you're not testing.
- **Spike (AD-18 resolved):** at full load inside the Lambda, `batch_get` takes 6.4–6.8 s per run and `execute_query` 19–22 s, so `batch_get` is the default. A 144-alias `IN` list in ExecuteQuery silently dropped rows, so it filters by prefix instead.
- **Note:** the implementer's own deploy attempt was refused by its permission check. I ran the deploy myself under your direct authorization, not on its behalf.

### 2.2 Flatline and stuck (OG-11) · PR #5
- **Done:** `dq_flatline` (railed at or beyond a contract limit) and `dq_stuck` (identical values that aren't railed), both on the trailing run, plus the typed detector protocol.
- **My decision:** "within two runs" counts from when the fault enters the window. Windows end 60 s behind real time (AD-9), so a flag appears 2–3 minutes after a fault starts. Documented.
- **Live:** P-003 `flow_in` scored 0.0 / `dq_flatline`, P-015 `temperature` scored 0.0 / `dq_stuck`, and the other 142 sensors stayed at 1.0.
- **Known limit:** a legitimately shut-in pipe (flow 0) is flagged `dq_flatline`.

### 2.3 Drift and gap (OG-12) · PR #6
- **Done:**
  - `dq_drift`: the slope of the sensor's residual against its peer-group median, with an R² ≥ 0.97 straight-line check;
  - `dq_gap`: trailing silence.
- **My decision:** accepted the extra R² check. Rate alone couldn't separate drift from process wander, and the check also stops a leak step from counting as drift. The trade-off is that drift slower than about 0.3 K/min isn't flagged.
- **Live:** P-010 drift scored 0.0 / `dq_drift` after 4.5 min, and P-020 scored 0.0 / `dq_gap`.
- **Deferred:** an offset-from-peers check, because a drift that has stopped clears even though the offset remains.

### 2.4 Trust on the overview (OG-13) · PR #7
- **Done:** each pipe tile gains Trust (six `_dq` values, red below `dq_min`, taken from `facility.yaml` at provision time) and Failing-checks panels.
- **Live:** checked through the Grafana API. The plugin's field names match the panel filter, and the injected sensor showed 0 against the 0.5 threshold.
- **Deferred:** a stale-data indicator (tiles keep their colour if detection stops); **a visual check in a browser is needed from you.**

### 3.1 Leak detection (OG-14) · PR #8
- **Done:** pass 2 (AD-5), with `leak_mass_balance` (ratio of sums, plus a low-flow guard) and `leak_methane_rise` (excess over the peer median). Findings go into the summary log.
- **My decision:** **if any pass-1 trust check crashes, pass 2 is skipped for that run**, with an ERROR and `pass2_skipped`, rather than trusting sensors that couldn't be scored.
- **Calibration:** no false positive across 4 seeds × 24 h of healthy simulator output.
- **Live:** P-007 mass balance 0.69 and methane 0.58 in the first window holding 35 s of leak; nothing elsewhere.

### 3.2 Peer outlier (OG-15) · PR #9
- **Done:** `outlier_peer`, a robust z-score (median and MAD) on pressure and flow ratios, temperature and methane, compared against the *other* pipes in the peer group.
- **Live:** P-007 scored 1.0: "Unlike its 5 peers … methane_ppm 159 vs peer median 2.047 (z +136.4)"; no other pipe flagged.

### 3.3 Incidents (OG-16) · PR #10
- **Done:** the `incidents-<stage>` table, implemented exactly per AD-6 and AD-12:
  - a conditional OPEN slot per pipe;
  - quiet counting and a transactional close;
  - reopen within the grace period;
  - a replay guard.

  Every run also writes `alert_state` and `alert_summary`.
- **My decision:** **a source-outage guard.** When ≥ 50% of sensors are silent (simulator start or stop, ingest outage), gap findings don't open incidents. Otherwise every simulator start would open 24 incidents and send 24 emails.
- **Live:** a leak opened one incident (3 detectors, `alert_state` 3). After cancelling, it closed `quiet` and `alert_state` returned to 0.

### 3.4 Email (OG-17) · PR #11
- **Done:**
  - a Deep link v1 builder with a contract test against the dashboard;
  - an AD-7 outbox notifier and the `operators-<stage>` SNS topic;
  - the topic denies Subscribe except to the CDK deploy role.
- **My decision:** **`operator_emails` is empty by default**, so no real email or subscription confirmation was sent overnight.
- **Live:** an SNS publish was accepted 2.3 min after the leak (target ≤ 5), `notified_at` was set, and no duplicate followed.
- **Stated plainly:** delivery is at-least-once (publish, then mark), and this is documented.

### 4.1 Injection list (OG-18) · PR #12
- **Done:**
  - `python -m ops.inject list` (read-only, `--json`, a cancelled flag);
  - the simulator records `effective_end` on the log row once, with a 10-minute retry.
- **Live (read-only):** `list` showed the night's 9 test injections with the correct effective ends.
- **Not yet live:** the simulator's write needs a `SimulatorStack` redeploy.

## Needs you
1. **Grafana:** sign in at https://g-f2edb8f41a.grafana-workspace.ap-southeast-1.amazonaws.com. Check that `pipe-detail-dj` and `facility-overview-dj` are readable (tile sizes), and that the trust colours look right.
2. **Email:**
   - add your address to `operator_emails` in `cdk.json`;
   - deploy `poc-dj` with `simulator=on` and click the SNS confirmation;
   - inject a leak and check the email layout and that **the link opens `pipe-detail-dj` with the evidence sensors selected** (not verifiable overnight).
3. **Merge-free follow-ups:**
   - `poc` has never been deployed (run `deploy` from `main` with `stage=poc` when ready; note the new `poc` tables are RETAIN with PITR);
   - who approves `poc` is still an open question in the spine.
4. **Not committed by me:** your `.memlog.md` edits (deferred-work notes that its CI/CD entries are stale) and `o&g-iiot.code-workspace`.

## Deferred work added tonight (in `deferred-work.md`)
- Grafana admin revoke on update.
- Service-account pagination test.
- Reader `skippedEntries` test.
- Offset-from-peers drift check.
- Story 4.4 alarms must respect the off switch, and cover detection errors and dropped Scheduler invocations.
- A stale-data indicator on the overview.
- An owner visual check of the overview.
- A TTL or archive for closed incidents.
- Memlog update.

## Next up
Epic 4, in this order:
- **4.2 reset:** closes incidents with `closed_reason=reset`, clears `sim_control`.
- **4.3 smoke test:** injection → incident → email → link.
- **4.4 health alarms.**
- **4.5 demo runbook and fallback recording.**
