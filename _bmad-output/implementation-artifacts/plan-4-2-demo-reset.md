---
title: 'Story 4.2: Reset the demo between rehearsals'
type: 'feature'
ticket: 'OG-19'
created: '2026-09-29'
status: 'done'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['edge-case-hunter', 'verification-gap']
review_loop_iteration: 0
baseline_revision: '9ca7c4cfd6a932ba34ffff17afdc63a91cb1c2a1'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Each rehearsal leaves OPEN incidents and live `sim_control` injections behind, and nothing clears them, so the next rehearsal starts dirty (Story 4.2, OG-19; AD-15).

**Approach:** `ops/reset.py` invokes the stage's detection Lambda with `{"action": "reset"}`. The Lambda closes every pipe's OPEN incident with `closed_reason=reset`. The script then deletes every `sim_control` item.

## Boundaries & Constraints

**Always:**
- Only `detect/adapters/incidents.py` writes `incidents` (AD-6). A reset close is the same `TransactWriteItems` as a quiet close (delete the `OPEN` slot, put `C#<incident_id>`), with `closed_reason=reset`.
- The reset's `run_id` is the invocation time (`iso(now)`, UTC ms). Its writes carry `last_run` and are conditional on `last_run < :run_id` (AD-12), so a later scheduled run proceeds and an earlier in-flight run's writes fail as conflicts.
- A reset close never reopens. `incident_rules._reopens` already requires `quiet`; keep a test for it.
- The reset event runs no detectors and writes no SiteWise values; the next scheduled run writes `alert_state = 0` and `alert_summary = {}`. It sends no email.
- The reset event is idempotent: pipes with no OPEN slot are skipped. It returns and logs one JSON summary: `{"action":"reset","run_id","closed":[{pipe, incident_id}],"conflicts":[pipe]}`.
- `ops/reset.py` deletes `sim_control` items with a paginated scan and batched deletes. It never touches raw data or `injection_log`. The simulator re-scans every ≤ 5 s and drops vanished items, which stops their effect.
- `ops/reset.py` reads `STAGE` (default `poc`), `SIM_CONTROL_TABLE` and the function name `detection-<stage>`, like `ops/inject.py`. It prints what it closed and deleted, and it exits non-zero if the invoke fails or returns a Lambda error; in that case it deletes nothing.
- It asks for confirmation unless `--yes` is given.
- **Decision (owner, 2026-09-29), stray incidents after reset:** keep the AD-15 order (close incidents, then delete injections). A run within about 5 minutes of the reset may still see the fault in its trailing window and open a new incident. `ops/reset.py` prints this hint, and the docs say to wait about 5 minutes and rerun the reset if an incident appears. No hold-off marker.

**Never:**
- No `Scan` on `incidents` and no new IAM for the Lambda (it already holds `Query`, `PutItem`, `DeleteItem` and `TransactWriteItems`).
- No smoke test (4.3), no alarms (4.4), no change to detectors or physics.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Reset with incidents | P-007 and P-012 OPEN, 2 active injections | both closed with `closed_reason=reset` (`C#` items, `last_run` = the reset `run_id`), 2 `sim_control` items deleted | none |
| Nothing to reset | no OPEN, empty `sim_control` | "closed 0 incidents, deleted 0 injections" | exit 0 |
| Finding after reset | a finding on P-007 within `reopen_grace_s` of its reset close | a new incident with a new `incident_id` (not a reopen); it gets its own email | none |
| Concurrent run | a scheduled run whose `run_id` is earlier than the reset's `run_id` writes the slot after the reset | the reset's close wins, and the run's write fails as a conflict or is skipped by the replay guard | the next run proceeds |
| Invoke fails | Lambda error, or `FunctionError` in the response | message, exit 1, `sim_control` untouched | no delete |
| Scheduled event | `{"scheduled_time": …}` | unchanged detection run | as today |

</frozen-after-approval>

## Code Map

- `detect/handler.py:490` `handler()` -- route `event.get("action") == "reset"` to a new `reset()` before `parse_scheduled_time`. Any other `action` value raises `ValueError`. `runtime()` (line 437) already builds `Incidents`; reuse it.
- `detect/adapters/incidents.py` -- add `Incidents.reset(run_id, now) -> ApplyResult`-like result. For each facility pipe: `read()`; skip it if there is no open slot or the replay guard applies (`max_last_run >= run_id`). Otherwise write a `close` decision with `closed_reason=RESET` via `_write` (the conflict handling is the same as in `_apply_pipe`). Reuse `rules.Decision`, `replace`, `iso`.
- `detect/core/incident_rules.py` -- add `reset_close(incident, now) -> Decision` (status CLOSED, `closed_reason=RESET`). `RESET` already exists (line 40), and `_reopens` (line 174) already excludes it.
- `ops/inject.py` -- the pattern for env/`--stage`/`--table`, AWS error handling and exit codes; reuse its helpers where they're importable, without moving code.
- `infra/stacks/detection.py` -- the Lambda name `detection-<stage>`. No IAM change.
- Tests: `tests/test_incidents.py`, `tests/test_incident_rules.py` (if present), `tests/test_detect_handler.py`, and a new `tests/test_reset_cli.py` using the fakes the inject CLI tests use.
- `docs/ci-cd.md` §5 or §7 -- a short "Reset between rehearsals" section with the command.

## Tasks & Acceptance

**Execution:**
- [x] `detect/core/incident_rules.py` -- `reset_close`. -- pure rule.
- [x] `detect/adapters/incidents.py` -- `Incidents.reset`. -- the only writer.
- [x] `detect/handler.py` -- the `action: reset` branch and summary line. -- the entry point.
- [x] `ops/reset.py` -- invoke, then delete `sim_control`; `--yes`, `--stage`, `--table`. -- the operator command.
- [x] Tests for every matrix row; `docs/ci-cd.md` usage. -- matrix.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass. The suite stays about 150 tests (OG-23), so add at most about 8.
- Given `poc-dj` deployed with an injected leak and an OPEN incident, when `ops/reset.py --yes` runs, then the incident becomes `C#…` with `closed_reason=reset`, `ops.inject list` shows nothing active, and `injection_log` still has its rows.

## Implementation Notes

- `Incidents.reset(run_id, now, pipes, config_hash) -> ResetResult(closed, replayed, conflicts)`: `pipes` and `config_hash` are passed by the handler (the adapter does not know the facility; `to_item` needs the hash). Per pipe: read, skip if no OPEN slot or replay guard, write `rules.reset_close` through `_write` (same close transaction), one retry on conflict, then logged and listed in `conflicts`.
- The handler's reset summary also carries `stage` and `config_hash` (logging convention) besides the planned keys. Any non-`reset` `action` raises `ValueError`; events without `action` run as before.
- `ops/reset.py` exits 1 on an invoke error, a `FunctionError`, a payload that is not a reset summary, a declined prompt, or a failed delete (after incidents were closed; the message says to rerun). Conflicts are printed as a warning with exit 0. The Lambda client uses `read_timeout=70` and standard retries (5 attempts) so a throttle from the single-concurrency function is retried. It reuses `ops.inject._scan` (paginated, consistent) and deletes through `Table.batch_writer()`.
- Tests (+8, suite 151 -> 159): `test_incidents.py` reset closes all and is idempotent / reset-closed never reopens (now through a real reset, new incident without `notified_at`) / reset beats an earlier in-flight run and the next run proceeds; `test_detect_handler.py` reset event end to end (no SiteWise writes, no email, unknown action refused); `test_reset_cli.py` 5 cases (with incidents + paginated delete + invoke-before-delete order, nothing to reset, invoke failure x2, confirmation). `tests/sim_fakes.FakeTable` gained `delete_item` and `batch_writer`.
- Docs: `docs/ci-cd.md` section 5 "Reset between rehearsals"; the section 7 "no reset yet" limit now points to it.
- Not done here: the live check on `poc-dj` (needs a deploy of `DetectionStack-poc-dj`).

- Live check on `poc-dj` (2026-09-29, deploy run 36521760457 from `ab52163`):
  - A leak was injected on P-007 at 04:32:28Z. Incident `01M3NPZZ…` opened and was emailed at 04:35:34Z.
  - `ops.reset --yes` at 04:36:37Z took 1.5 s. It closed that incident (`C#…`, `closed_reason=reset`, `last_run` = the reset's run_id, `notified_at` kept) and deleted 22 `sim_control` items (the leak plus earlier test items). `ops.inject list` then showed "no injections", and `injection_log` still held its 12 rows.
  - **A stray incident appeared, as the owner's decision foresaw, but it lasted longer than the hint says.** The run at 04:37:37Z opened a new incident (`01M3NQ5E…`) and emailed it. Findings on P-007 (methane still ~65 ppmv above peers, plus outlier) continued through the 04:39:37Z run and were clean from 04:40:37Z, about 4 minutes after the reset.
  - A second `ops.reset --yes` at about 04:42Z closed it. The runs at 04:42:37Z and later opened nothing.
- Merged: PR #17, merge commit `b04e63e`.

## Plan Change Log

- 2026-09-29, after the live check (owner): the stray-incident hint changes from "about 2 minutes" to "about 5 minutes" in `ops/reset.py` and `docs/ci-cd.md`. On `poc-dj` a leak's methane stayed above its peers for about 4 minutes after the reset. The owner amended the frozen decision; the order and the no-marker choice are unchanged.

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Lenses: edge-case-hunter, verification-gap (owner chose two lenses, run in turn). Counts: high 0, medium 1, low 5, false 0, maybe-false 0. Outcome: 2 patches, 1 defer, 4 rejected; no loopback.

| Id | Finding | Verdict | Route | Evidence / action |
|----|---------|---------|-------|-------------------|
| VG-1 | Reset conflict path (retry, `conflicts`, CLI warning) untested | medium | patch | Pre-verified: every reset test asserts `conflicts == []`. Add a double-conflict test and a CLI warning assertion. |
| EC-3 | No `--yes` and no TTY: `input()` raises `EOFError`, giving a traceback | low | patch | `ops/reset.py` calls `ask()` unguarded. Treat EOF or Ctrl-C as "no" (abort, exit 1). Likely when 4.3 or a pipe calls it. |
| VG-2 | CLI "delete injections failed" path untested | low | defer | Pre-verified. `FakeTable.fail_scans` raises a non-botocore error, so the test needs a fake change. |
| EC-1, VG-o | A pipe skipped by the reset's replay guard stays OPEN with no report | low | reject | Only reachable if a stored `last_run` is at or after the reset's invocation time. Reserved concurrency 1 serialises runs, and a scheduled `run_id` is never after the time it runs, so it needs clock skew. The fix adds a summary field and a CLI branch. |
| EC-2 | An OPEN incident on a pipe no longer in `facility.yaml` is never reset | low | reject | The facility is fixed for the POC. The fix needs a `Scan`, which the frozen intent forbids. |
| EC-4 | Deleting an active injection leaves its `injection_log` row without `effective_end` | low | reject | The frozen intent requires `injection_log` untouched (AD-15). The rows keep their planned end; any later scoring must bound windows by the reset time. |
| EC-5 | `{"action": null}` falls through to a `scheduled_time` error | low | reject | No sender produces it, and the error still fails loudly. |

## Design Notes

A reset invoke is synchronous (`InvocationType=RequestResponse`), so the script knows the incidents are closed before it deletes injections.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.

**Manual checks:**
- Live on `poc-dj` after the deploy: inject a leak, wait for the incident, run the reset, check the incident, `ops.inject list` and `injection_log`.
