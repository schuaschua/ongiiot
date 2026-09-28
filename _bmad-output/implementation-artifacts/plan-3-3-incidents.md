---
title: 'Story 3.3: Group findings into one explainable incident per pipe'
type: 'feature'
ticket: 'OG-16'
created: '2026-09-29'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: '801214a4e197c4dac813d4b2d11992fa06ed2d87'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Findings only exist in log lines. Operators need one incident per pipe that gathers every detector's finding with its confidence and evidence, opens once, closes when things are quiet, never floods them with duplicates, and shows as alert state on the pipe (Story 3.3, OG-16; FR6; AD-6, AD-12, AD-13).

**Approach:** A per-stage `incidents` DynamoDB table, written only by a new `detect/adapters/incidents.py`, exactly per AD-6 and Incident item v1:
- a conditional OPEN slot per pipe;
- upsert of each finding into the pipe's incident;
- quiet counting and close;
- reopen within grace;
- a `last_run` replay guard.

After the incident updates, each run writes `alert_state` and `alert_summary` for every pipe (AD-13).

## Boundaries & Constraints

**Always:**
- AD-6 exactly:
  - one OPEN slot `pk=<pipe_id>, sk=OPEN`, created with `attribute_not_exists(pk)`; `ConditionalCheckFailed` means update;
  - every finding of any family, `dq_*` included, joins the incident: upsert `detectors[id]` (confidence, explanation, version, `first_seen`, `last_seen`, `quiet_runs = 0`), a sorted union of `evidence_aliases`, `evidence_to` widened, `evidence_from` fixed at open, `confidence` = max;
  - `quiet_runs` increments only when the detector is in `ran_ok`, the pipe is in that detector's covered pipes, and it produced no finding;
  - close when every detector has `quiet_runs >= quiet_runs_to_close`: one `TransactWriteItems` deletes the slot and puts `sk=C#<incident_id>`, `status=CLOSED`, `closed_reason=quiet`;
  - reopen: a finding within `reopen_grace_s` of the pipe's latest close restores that incident (same `incident_id`, `notified_at` kept); `closed_reason=reset` never reopens.
- AD-12: every incident write carries `last_run` and is conditional on `last_run < :run_id`. A replayed `run_id` makes no incident writes, and the derived writes are identical because they are timestamped at `window_end`.
- Covered pipes (autonomous decision, logged): a pipe is covered by a detector in a run when that detector ran OK and the pipe was not listed as suppressed or skipped for it.
- AD-13, written after the incident updates, every run, every pipe:
  - `alert_state` (INTEGER) = number of detectors in the OPEN incident, or `0`;
  - `alert_summary` (STRING ≤ 1024 B) = JSON `{"incident_id","confidence","detectors":[{"id","c"}],"evidence":[alias]}` or `{}`, with `evidence` dropped first if too long.
  - `derived_writer` gains INTEGER support.
- Infra: `incidents-<stage>` table in `DetectionStack` (keys `pk`/`sk` as S, on demand, `RETAIN` for `poc`, `DESTROY` otherwise). The detection role gets `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` and `Query` on that table only, plus DynamoDB `TransactWriteItems` (resource-scoped). The simulator role gets nothing new (AD-17).
- If pass 2 was skipped (Story 3.1 rule), no detector counts as quiet for that run.

**Never:**
- No email, SNS or deep link (3.4); no reset (4.2); no Grafana changes.
- No change to detector behaviour.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Open | first findings on P-007 (3 detectors) | one OPEN item with 3 detectors, confidence = max, sorted evidence; `alert_state` 3; `alert_summary` JSON | none |
| Grow | more findings in later runs | same `incident_id`; detectors upserted; `evidence_to` widens; `evidence_from` fixed | none |
| One slot | two runs racing on one pipe (conditional fail) | still one OPEN item | `ConditionalCheckFailed` → update path |
| Quiet close | all detectors quiet for `quiet_runs_to_close` runs | slot deleted, `C#<id>` CLOSED `quiet`, `alert_state` 0, `alert_summary` `{}` | transactional |
| Not covered | a detector suppressed for the pipe | its `quiet_runs` unchanged | none |
| Reopen | a finding within `reopen_grace_s` of a quiet close | same `incident_id` OPEN again, `notified_at` kept | none |
| Reset-closed | the latest close was `reset` | a new incident, never a reopen | none |
| Replay | the same `run_id` again | no incident writes; identical derived values | conditional writes refuse |
| Healthy | no findings | no incident; `alert_state` 0 / `{}` for all pipes | none |
| Live | `poc-dj` leak on P-007, then cancelled | OPEN incident with the leak and outlier detectors; `alert_state` ≥ 2; after cancel and quiet runs, CLOSED `quiet` and `alert_state` 0 | recorded; simulator off |

</frozen-after-approval>

## Code Map

- `detect/handler.py` -- `run()`: pass 1, then score and write `dq`, then pass 2 → `summary.findings` / `suppressed` / `pass2_skipped` / `ran_ok`. Add the incident step after the findings, then the alert writes. `runtime()` builds the clients; add the incidents table from env `INCIDENTS_TABLE`. `run_id` = normalised `scheduled_time`.
- `detect/adapters/derived_writer.py` -- the derived-only guard; `_variant` handles DOUBLE and STRING; add INTEGER. `facility.derived_alias(pipe, "alert_state" | "alert_summary")` already exists (asset model v1).
- `detect/core/models.py` -- `Finding`. Keep incident state logic pure where possible (for example a `detect/core/incident_rules.py` that decides open / upsert / quiet / close from the current item and findings), with I/O in the adapter.
- `infra/stacks/detection.py` -- the pattern for the table, grants and env (see `infra/stacks/simulator.py` tables and removal policy).
- `facility/facility.yaml` tunables `quiet_runs_to_close: 5`, `reopen_grace_s: 600` (already present).
- `tests/detect_fakes.py` -- extend with a fake DynamoDB table supporting conditional expressions and transactions (or use a small in-memory fake with the same semantics).
- `ops/` has no reset yet (4.2).

## Tasks & Acceptance

**Execution:**
- [x] `detect/core/incident_rules.py` (pure) + `detect/adapters/incidents.py` (DynamoDB I/O) -- AD-6/AD-12. -- incidents.
- [x] `detect/adapters/derived_writer.py` -- INTEGER variant; `alert_state` / `alert_summary` writes after incidents. -- AD-13.
- [x] `detect/handler.py` -- the incident step, covered pipes, the pass-2-skipped rule, alert writes, and summary fields (`incident_ops`: opened, updated, closed, reopened). -- wiring.
- [x] `infra/stacks/detection.py` -- the table, env and exact grants; update the exact-policy tests. -- infra.
- [x] Tests: every matrix row except Live, with a faithful fake table (conditions and transactions); an AD-12 replay test; alert-summary size capping. -- matrix.
- [x] `docs/ci-cd.md` §7 -- incidents, alert state, and how to read the table. -- operability.
- [x] Live (the orchestrator runs this). -- acceptance.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given `cdk synth -c stage=poc-dj`, then it exits 0 with `incidents-poc-dj`.
- Given the live check, then the OPEN incident and `alert_state` are observed, and after cancel the incident closes `quiet`.

## Implementation Notes

- Autonomous decisions (logged):
  - Covered pipes: `handler.covered()` = `ran_ok` minus the detectors listed in `suppressed` for that pipe; empty for every pipe when pass 2 was skipped.
  - Findings joining incidents are pass 1's (with the `dq_score` gap rule's) plus pass 2's. Several findings of one detector on one pipe (for example `dq_*` on two sensors) make one `detectors[id]` entry from the strongest finding; all their aliases join the evidence.
  - `first_seen`, `last_seen`, `opened_at` and `closed_at` are the run's `window_end`; `evidence_from` is the earliest `window_start` at open. `incident_id` = ULID from `window_end`.
  - Update = conditional `PutItem` of the whole item (`attribute_exists(pk) AND last_run < :run_id`), not `UpdateItem`; reopen = one transaction deleting `C#<id>` (`closed_reason = quiet AND last_run < :run_id`) and putting `OPEN` (`attribute_not_exists(pk)`), so the history item is restored, not copied.
  - Replay guard also in code: a pipe whose items hold any `last_run >= run_id` gets no write (covers a late run that would otherwise open a new incident after a later close). A conflict re-reads and decides once more; a second conflict is logged as WARNING and left to the next run. Other DynamoDB errors fail the invocation.
  - `alert_summary` confidences are rounded to 2 dp (display); stored confidences are unrounded. Past 1024 bytes, `evidence` goes first, then trailing detectors.
  - `DerivedWriter`: Python `int` -> `integerValue` (32-bit range), `float` -> `doubleValue`; the handler casts `_dq` to `float`. Alerts are a second `write` call after the incident step, so `values_written` is 288 + 48.
  - The DynamoDB client is a low-level `boto3.client("dynamodb")` (typed attribute values via `TypeSerializer`), standard retries, 3 attempts. `INCIDENTS_TABLE` is required at cold start, and `quiet_runs_to_close` / `reopen_grace_s` are checked there too.

- Live check (2026-09-29 04:29–04:43 +08, `poc-dj`, deploy run 36478254459, pre-review code `626e039`):
  - Default leak on P-007 04:29–04:33.
  - At 04:34: one OPEN incident on P-007 with `leak_mass_balance`, `leak_methane_rise` and `outlier_peer` (confidence 1.0); SiteWise `alert_state` = 3 (`integerValue`), and `alert_summary` JSON with `incident_id`, the detectors and the evidence. No other incidents; no startup gap incidents.
  - At 04:43, after the cancel and quiet runs: no OPEN slot, one CLOSED item (`closed_reason` `quiet`), `alert_state` 0, `alert_summary` `{}`.
  - Switched off with the reviewed code (`7f886f3`).

## Plan Change Log

- 2026-09-29, review fixes (orchestrator decision + review findings):
  - Source-outage guard (orchestrator decision): when >= 50% of raw aliases have no samples, `dq_gap` findings do not feed incidents and no detector counts quiet; `source outage` log line and `source_outage` in the summary; dq values still written.
  - `suppressed` entries carry `scope` (`detector` | `feature`); only `detector` scope blocks quiet counting. Pass-2-skipped runs: pass-1 detectors in `ran_ok` still count quiet.
  - Replay guard also skips that pipe's alert writes; `incident_conflicts` in the summary.
  - `DerivedWriter` takes the data type from the facility's derived properties (`types`), not the Python type.
  - `TransactionCanceledException` is a conflict only with a `ConditionalCheckFailed` reason.
  - Empty detectors map closes quiet; tunables must be finite; `alert_summary` falls back to `{}`; `iso()` refuses naive datetimes.
  - Grants reduced to Query, PutItem, DeleteItem, TransactWriteItems (one statement); PITR on the `poc` incidents table only.

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 2, medium 9, low 12, false 0. Outcome: 12 patch groups plus the orchestrator's source-outage guard, 1 deferral, no loopback.

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| orchestrator | Every simulator start opens 24 `dq_gap` incidents | high | patch (decision) | Source-outage guard: ≥ 50% of aliases silent → no `dq_gap` into incidents, no quiet counting. |
| BH-2 | A per-feature skip blocks `outlier_peer` quiet counting forever | high | patch | Scoped suppression entries. |
| BH-3, IA-3 | Pass-2 skip also blocks pass-1 quiet counting | medium | patch | Only failed and pass-2 detectors lose credit. |
| BH-1, ECH-1, IA-2 | Replay rewrites historical alert values from the current state | medium | patch | Skip alert writes on replay; docs fixed. |
| BH-5 | `int` inferred as INTEGER | medium | patch | Type taken from the property's data type. |
| BH-10, ECH-3 | Every transaction cancellation treated as a conflict | medium | patch | Check `CancellationReasons`. |
| BH-9 | Conflicts invisible in the summary | low | patch | `incident_conflicts`. |
| ECH-2, ECH-5, ECH-6, BH-12 | Empty detectors map crashes `max()`; NaN grace; summary over the cap; naive datetime | medium | patch | Guards. |
| BH-6 | IAM grants broader than used | medium | patch | Exact grants (AD-17). |
| BH-7 | No PITR on the `poc` table | low | patch | PITR for `poc`; TTL for history deferred. |
| VG-1, VG-2, BH-13 | Paging and transaction races untested | medium | patch | Tests. |
| BH-8, BH-11, ECH-4 | Docs `grep` broken; partial-failure ordering undocumented | low | patch (doc) | `jq`; run order and recovery documented. |
| BH-4 | Reopen keeps stale detectors; no confidence decay | low | reject (doc) | AD-6 semantics (max over detectors, restore on reopen); documented. |
| BH-7b | No TTL or archive for closed history | low | defer | Grows slowly at POC scale. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
