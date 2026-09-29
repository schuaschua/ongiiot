---
title: 'Story 4.3: Smoke-test the full chain before the demo'
type: 'feature'
ticket: 'OG-20'
created: '2026-09-29'
status: 'done'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['edge-case-hunter', 'verification-gap']
review_loop_iteration: 0
baseline_revision: 'f13c199d02ce13f1c06f9991be05366ddde9b72f'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Before the client arrives, nothing proves in one step that injection → incident → email → evidence link works on the demo stage (Story 4.3, OG-20; AD-15 smoke, latency target ≤ 5 min).

**Approach:** `ops/smoke.py` injects a leak on a test pipe, waits for a *new* OPEN incident with `notified_at`, reads the emailed link, and checks that it opens the stage's `pipe-detail` dashboard. It fails with a clear message naming the step if the 5-minute budget runs out.

## Boundaries & Constraints

**Always:**
- One budget of 5 minutes (`--budget-s`, default 300), counted from the injection. Every wait checks the time left. On timeout the script exits 1, naming the step it was waiting for (`incident`, `email`, `link`) and the time spent.
- Steps, each printed with its elapsed time:
  1. **Precheck:** the test pipe has no OPEN incident. If it has one, exit 1 with "run ops.reset first"; there is nothing to clean up.
  2. **Inject** a default leak (writing the `sim_control` item the way `ops/inject.py` does), lasting the budget plus 2 minutes.
  3. **Incident:** poll `incidents-<stage>` for the pipe's OPEN slot every 10 s, until its `incident_id` exists and `opened_at` is at or after the injection start.
  4. **Email:** wait until that incident has `notified_at`.
  5. **Link:** find the link that was emailed for that `incident_id` (see below).
  6. **Resolve:** mint a short-lived Grafana token on the stage's service account and `GET /api/dashboards/uid/<uid>` for the uid in the link path. Pass only if it returns 200, the uid matches the stage's `pipe-detail` uid, and the dashboard's templating has `pipe_id`, `area` and `evidence`. Then delete the token.
- The link comes from the email itself, not a rebuild: the notifier's summary entry in `notified` gains `link`, and the smoke reads it from `/aws/lambda/detection-<stage>` with `FilterLogEvents`, matched on `incident_id`. `ops/` must not import `detect/` (layer rules), and AD-8 keeps `deeplink.py` the only link builder.
- Always cancel the injection at the end (pass, fail or Ctrl-C), with a cancel item as in `ops/inject.py`. The incident stays OPEN. The final line says "PASS" or "FAIL: <step>: <reason>", and a pass also tells the operator to run `ops.reset` before the demo.
- Options: `--stage`/`STAGE` (default `poc`), `--pipe` (default `P-007`, the calibrated leak pipe), and `SIM_CONTROL_TABLE`/`--table`. The incidents table, function, log group, workspace id and dashboard uid follow the stage naming and CloudFormation outputs already used.

**Never:**
- No change to the email text or the link format; no new subscription or queue on the operator topic; no alarms (4.4); no reset inside the smoke.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Pass | warmed-up stage, P-007 clean | each step printed with elapsed time, then PASS; the injection is cancelled | none |
| Dirty pipe | P-007 already OPEN | "FAIL: precheck: P-007 has OPEN incident …; run ops.reset first"; nothing injected | exit 1 |
| No incident | no OPEN slot by the budget | "FAIL: incident: none after 300 s"; injection cancelled | exit 1 |
| No email | OPEN, but `notified_at` never set | "FAIL: email: …"; injection cancelled | exit 1 |
| Bad link | the link's uid returns 404 or the vars are missing | "FAIL: link: …"; injection cancelled; token deleted | exit 1 |
| Stale incident | an OPEN slot whose `opened_at` is before the injection | not accepted; keeps waiting (then the precheck would have caught it) | as No incident |

</frozen-after-approval>

## Code Map

- `ops/inject.py` -- `new_injection`/`to_item` put, and the cancel item (`cancel_of`); `_scan`, env and AWS error handling. Reuse, don't move.
- `detect/adapters/notifier.py:66,134` -- `NotifyResult.notified` entries `{pipe, incident_id, message_id}`: add `link` (the string already built at line 125). The handler logs `notified` in its summary (`detect/handler.py`).
- `infra/lambdas/grafana_provision/handler.py:118-166` -- `service_account_name`, `ensure_service_account`, `short_lived_token`, `GrafanaApi`, `TOKEN_TTL_S`. Import them from `ops/smoke.py`; layer rules allow `ops` → `infra`. `infra/lambdas/` has no `__init__.py`, so check the import works under pytest and `python -m`.
- `infra/stacks/grafana.py:121-122` -- `SharedGrafanaStack` outputs `WorkspaceId`, `GrafanaUrl`; `infra/stacks/detection.py:230` -- `dashboard_uid("pipe-detail", stage)`, the uid to expect.
- `dashboards/pipe-detail.json` -- template variable names (`pipe_id`, `area`, `evidence`).
- `tests/test_layers.py` -- must still pass.
- Tests: `tests/test_notifier.py` (link in `notified`), new `tests/test_smoke_cli.py` with fakes (tables, logs, grafana client and API, clock).

## Tasks & Acceptance

**Execution:**
- [x] `detect/adapters/notifier.py` -- `link` in each `notified` entry; the notifier test asserts it. -- the emailed link becomes readable.
- [x] `ops/smoke.py` -- steps 1–6, budget, cleanup, output. -- the operator command.
- [x] `tests/test_smoke_cli.py` -- every matrix row with a fake clock (no real sleeps). -- matrix.
- [x] `docs/ci-cd.md` -- a "Smoke test before the demo" subsection next to the reset, including the IAM the caller needs (`logs:FilterLogEvents`, Grafana service-account token actions). -- runbook.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all pass, with at most about 8 new tests.
- Given `poc-dj` deployed from this branch, simulator on and P-007 clean, when `ops.smoke --stage poc-dj` runs, then it prints PASS within 5 minutes and the injection is cancelled.

## Implementation Notes

- The expected dashboard uid and the Grafana workspace come from stack outputs read in the precheck (`GrafanaStack-<stage>` `PipeDetailUid`; `SharedGrafanaStack` `WorkspaceId`, `GrafanaUrl`), so `ops/smoke.py` needs no CDK import and fails before injecting if the stage is not deployed.
- `infra.lambdas.grafana_provision.handler` imports as a namespace package under pytest and `uv run python -m ops.smoke` (repo root on the path).
- Besides the uid in the link path, the resolve step also checks the uid in the returned dashboard.
- If the cancel at the end fails, the result is `FAIL: cancel: ...` with the `ops.inject cancel` command to run.
- Tests: 8 new (6 functions, two parametrized); the notifier test asserts `link` in `notified` and that it is the emailed one.

- Live check on `poc-dj` (2026-09-29, deploy run 36526379321 from `bdff339`): `ops.smoke --stage poc-dj` printed PASS in 203 s.
  - A leak was injected at 05:33:17Z. Incident `01M3NTFN…` opened (at the 05:34:00Z window end) and was emailed at 05:36:34Z.
  - The emailed link was read from the log, and `pipe-detail-dj` resolved with `pipe_id`, `area` and `evidence`. The injection was cancelled.
  - `ops.reset` then closed the incident. A stray incident opened at 05:37:37Z, as the 4.2 hint predicts; findings stopped from 05:40:37Z, and a second reset left the stage quiet.
- Merged: PR #18, merge commit `2a0f358`.

## Plan Change Log

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Lenses: edge-case-hunter, verification-gap, run in turn (owner's choice for Epic 4). Counts: high 0, medium 3, low 9, false 1, maybe-false 0. Outcome: 8 patches, 5 rejected; no loopback.

| Id | Finding | Verdict | Route | Evidence / action |
|----|---------|---------|-------|-------------------|
| EC-1, EC-c2, VG-o3 | The injection is recorded only after `put_item` returns, so Ctrl-C or an ambiguous write leaves a live leak uncancelled | medium | patch | `ops/smoke.py` sets `self.injection` after the put. Set it before; a cancel of an unwritten target is harmless (the simulator folds cancels by `supersedes`). |
| VG-2 | A failed cancel is untested | medium | patch | Pre-verified. Add a test: the second put raises; expect exit 1, `FAIL: cancel:` and the hint. |
| VG-4 | The handler summary's `notified[].link` is never asserted across the producer/consumer boundary | medium | patch | Pre-verified. Extend `test_entry_point_emails_once_with_the_stage_link` to assert the link. |
| EC-4, EC-c1 | The budget is only checked inside `wait()`, so a link resolved after 300 s still prints PASS | low | patch | Real, but it only overruns by the resolve step's seconds. Check the deadline before PASS. |
| EC-6 | An unexpected exception ends with a traceback, not `FAIL: <step>: …` | low | patch | The `finally` still cancels. One `except Exception` keeps the documented last line (also covers EC-5, EC-7). |
| EC-8 | `SIM_CONTROL_TABLE` from another stage injects on the wrong stage | low | patch | Plausible, since the env var is shared with `ops.inject`/`ops.reset`. Fail the precheck unless the table is `sim-control-<stage>`. |
| VG-1 | A wrong-dashboard uid is never tested | low | patch | Pre-verified. Add a parametrized case to the bad-link test. |
| VG-3 | The link-step timeout (pre-4.3 Lambda) is untested | low | patch | Pre-verified. Add a parametrized case to the timeout test. |
| EC-3 | A quiet close within `reopen_grace_s` makes the injection reopen the old incident (old `opened_at`), so the smoke fails "incident: none" | low | reject | Real but rare: the pre-demo flow runs `ops.reset`, whose closes never reopen. The fix needs a closed-history query and a new branch. |
| EC-2 | A second Ctrl-C during the cancel gives a traceback | low | reject | Unlikely; it adds interrupt handling inside cleanup. |
| EC-5 | A non-dict Grafana response gives an AttributeError | low | reject | The Grafana API returns objects; EC-6's patch turns it into a FAIL line anyway. |
| EC-7 | A huge `--budget-s` overflows `timedelta` | low | reject | Operator input nobody gives; EC-6's patch covers it. |
| EC-9 | One throttle aborts the smoke | false | reject | boto3 retries throttling errors by default before raising. |
| EC-10 | A network error shows "returned 0" | low | reject | Cosmetic. |

## Design Notes

The poll interval is 10 s: detection runs once a minute, and a leak on P-007 opened and emailed its incident about 2–3 minutes after injection in today's runs.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.

**Manual checks:**
- Live on `poc-dj` after the deploy: `ops.smoke --stage poc-dj` prints PASS; then `ops.reset --yes`.
