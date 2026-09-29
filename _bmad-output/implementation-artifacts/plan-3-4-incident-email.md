---
title: 'Story 3.4: Email operators with a one-click evidence link'
type: 'feature'
ticket: 'OG-17'
created: '2026-09-29'
status: 'done'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: 'af7e7a61f5f1d2b039e6ed47537eef1bfbf52d49'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Incidents open silently. Operators, who are consultants in the POC, need exactly one email per incident with the confidence, the detectors and a link that opens the raw evidence in Grafana in one click. The email must be demo-safe: allowlisted recipients only, and a POC banner (Story 3.4, OG-17; FR7; AD-7, AD-8; NFR7, NFR8).

**Approach:**
- `detect/adapters/deeplink.py::build_link` renders the literal Deep link v1 template.
- `detect/adapters/notifier.py` runs at the end of each detection run. For every OPEN incident without `notified_at`, it publishes an Email v1 message to the stage's operator SNS topic, then conditionally sets `notified_at` (the outbox).
- The topic's subscriptions come only from the CDK context allowlist `operator_emails`.
- `tests/test_deeplink_contract.py` pins the link to `dashboards/pipe-detail.json`.

## Boundaries & Constraints

**Always:**
- AD-7:
  - the notifier runs in-process after the incident writes (no DynamoDB Stream);
  - publish, then `SET notified_at` with `attribute_not_exists(notified_at)`, conditional on the OPEN slot still holding the same `incident_id`;
  - a failed publish is logged and retried next run;
  - detectors joining an already-notified incident send no email;
  - a reopened incident keeps `notified_at` and sends no new email.
- AD-8:
  - the link is built only by `build_link(incident, grafana_url, dashboard_uid)`, on one line with no whitespace;
  - `var-evidence` is repeated per alias, URL-encoded;
  - `from = epoch_ms(evidence_from) − 300000`, `to = epoch_ms(evidence_to) + 300000`;
  - `GRAFANA_URL` and `DASHBOARD_UID` (`pipe-detail` for `poc`, `pipe-detail-<initials>` otherwise) reach the Lambda as CDK-set env vars from the Grafana stacks' values.
- Email v1:
  - subject `[POC] Pipe <pipe_id> incident <incident_id>`;
  - the body's first line is `POC - simulated data - evidence only, not an operational alert or official record`;
  - then the pipe ID and area; the incident confidence (2 dp); one line per detector (id, confidence 2 dp, explanation); the contributing aliases; the Deep link v1.
- Topic `operators-<stage>` with an email subscription per address in the CDK context `operator_emails`, a comma-separated list, empty by default. The topic policy denies `sns:Subscribe` to every principal except the CDK deploy path (the CloudFormation execution role).
- Decision (autonomous, logged): `operator_emails` is empty in the repo and in the overnight live check, so no real email or subscription confirmation is sent without the owner. The runbook shows how to add an address (`-c operator_emails=...` via a deploy input or `cdk.json` context).
- IAM: the detection role gains `sns:Publish` on this topic only, and the incidents condition write for `notified_at` (already covered by `PutItem`/`UpdateItem` as needed; keep the grants exact).
- Summary log: `notified: [{pipe, incident_id}]` and `notify_failures: [...]`.

**Never:**
- No other recipients; no email to client operators; no SES; no change to detectors or incident rules beyond the `notified_at` write.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| New incident | OPEN P-007 without `notified_at` | one publish in Email v1; `notified_at` set | none |
| Already notified | a later run with the same incident | no publish | none |
| Joining detector | a new detector joins a notified incident | no publish | none |
| Publish fails | SNS error | logged in `notify_failures`; `notified_at` not set; the next run retries | no raise for the whole run |
| Race | `notified_at` set between publish and write | conditional write fails; no second publish next run | logged |
| Reopen | a notified incident reopens within grace | no new email | none |
| Link | an incident with 3 aliases | exact Deep link v1: 3 encoded `var-evidence`, from/to ± 5 min, one line | none |
| Contract | `dashboards/pipe-detail.json` | uid `pipe-detail`; variables `pipe_id`, `area`, `alias_root`, multi-value `evidence` | test fails on drift |
| Banner | any email | first body line is the POC banner | none |
| Allowlist | `operator_emails` empty or two addresses | 0 or 2 subscriptions; the Subscribe deny policy is present | none |
| Live | `poc-dj` leak on P-007 | one publish accepted by SNS (message id logged) within 5 min of the leak; `notified_at` set; no second publish; the link resolves to `pipe-detail-dj` | recorded; simulator off |

</frozen-after-approval>

## Code Map

- `detect/adapters/incidents.py` -- the only incidents writer; add a `mark_notified(pipe_id, incident_id, when)` there (AD-6 "only writer"), with a condition on the same `incident_id` and `attribute_not_exists(notified_at)`. `incident_rules` keeps `notified_at` on reopen.
- `detect/handler.py` -- after the incidents and alert writes, call the notifier for OPEN incidents lacking `notified_at` (the incident states are returned by `incidents.apply`); add summary fields; build the SNS client in `runtime()`; env `OPERATOR_TOPIC_ARN`, `GRAFANA_URL`, `DASHBOARD_UID`.
- `infra/stacks/detection.py` -- the topic, subscriptions from context, the topic policy, env and the grant. Get `GRAFANA_URL` and `DASHBOARD_UID` from `GrafanaStack-<stage>` / `SharedGrafanaStack` (cross-stack reference or the same derivation used there); keep dependencies acyclic.
- `infra/stacks/grafana_provision.py` -- how the stage dashboard uid is derived (`pipe-detail` / `pipe-detail-<initials>`).
- `dashboards/pipe-detail.json` -- the template; the provisioned uid gets the stage suffix.
- `.github/workflows/deploy.yml` -- optionally add an `operator_emails` input passed as context via `env:` (not required tonight; the runbook covers `cdk.json` context).

## Tasks & Acceptance

**Execution:**
- [x] `detect/adapters/deeplink.py`, `detect/adapters/notifier.py`, `incidents.mark_notified`. -- the email path.
- [x] `detect/handler.py` -- the notify step and summary fields. -- wiring.
- [x] `infra/stacks/detection.py` -- the topic, allowlist subscriptions, the deny-Subscribe policy, env and the exact grant; tests. -- infra.
- [x] Tests: `tests/test_deeplink.py`, `tests/test_deeplink_contract.py`, `tests/test_notifier.py`, handler and stack tests; every matrix row except Live. -- matrix.
- [x] `docs/ci-cd.md` -- email behaviour, how to add a recipient (confirmation needed), the banner, and the link. -- operability.
- [x] Live (the orchestrator runs this). -- acceptance.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given `cdk synth -c stage=poc-dj`, then it exits 0 with the topic, no subscriptions (empty allowlist) and the policy.
- Given the live check, then one publish is logged for the P-007 incident with `notified_at` set, and no duplicates.

## Implementation Notes

- Live check (2026-09-29 05:21–05:26 +08, `poc-dj`, deploy run 36484072012, pre-review code `2df7397`, empty `operator_emails`):
  - Default leak on P-007 at 21:21:09Z.
  - The 21:23:27 run published to `operators-poc-dj` (SNS MessageId `8af9f9fe-1053-523d-8780-9acacd163200`), 2.3 min after the leak (NFR8 ≤ 5 min), and the OPEN incident got `notified_at` 21:24:21Z.
  - The next run published nothing (outbox holds).
  - The earlier Story 3.3 incident stayed CLOSED; beyond the grace period, a new incident opened.
  - Delivery to an inbox and the link click were not verified (empty allowlist); owner to verify.
  - Leak cancelled; switched off with the reviewed code (`43bb0b2`).
- Merged: PR #11, merge commit `cf72ec2`.

## Plan Change Log

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 9, low 9, false 0. Outcome: 8 patch groups, no loopback. Delivery is documented as at-least-once.

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| BH-3, ECH-2 | Render errors retried forever as publish failures | medium | patch | Render step first; subject kept within SNS limits. |
| ECH-5, BH-2 | Publish can hang past the 50 s Lambda; no per-run budget | medium | patch | 5 s timeouts, 2 retries, deadline check. |
| ECH-6 | Replayed or conflicted pipes still notified | medium | patch | Skipped. |
| ECH-claim, IA-R2 | "Exactly one" is really at-least-once | medium | patch (doc) | Documented. |
| VG-1 | Production `runtime()` wiring untested | medium | patch | Entry-point test. |
| VG-2 | Deny-exemption ARN only partly pinned | medium | patch | Full ARN assertion. |
| BH-9 | Handler publish-failure path untested | medium | patch | Test. |
| ECH-1, ECH-3, ECH-4, ECH-8, BH-7 | Non-dict response; newlines; naive clock; case-duplicate emails | low | patch | Guards. |
| BH-4 | `UpdateItem` table-wide | medium | patch | Attribute condition if synthesizable, else documented. |
| BH-1, BH-5, BH-6 | Backfill email on first enable; subscription overclaim; recipients shared by every stage | low | patch (doc) | Documented. |
| ECH-7 | A concurrent full-item put erases `notified_at` | low | reject (doc) | Reserved concurrency 1 serialises writers. |
| BH-10 | `notified_at` taken from the clock, not `window_end` | low | reject | Outbox timestamp semantics; tests inject a clock. |
| IA-R1 | Delivery and the one-click link not observed at the recipient | low | reject | Empty allowlist by decision; owner to verify (morning checklist). |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
