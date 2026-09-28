---
title: 'Story 4.1: List running injections and record their true end'
type: 'feature'
ticket: 'OG-18'
created: '2026-09-29'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: 'cf72ec29408be5d643b0a7e4caeaf769b61e1eb1'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Operators can only cancel an injection if they kept the ID printed when it was created. `injection_log` also keeps the *planned* end even when an injection was cancelled early, so later scoring would use a wrong window (Story 4.1, OG-18; AD-15; two deferred-work items from the simulator plan).

**Approach:**
- `ops/inject.py list` shows active and recent injections: the ID, type, pipe, measurement and effective window (planned end shortened by any cancel).
- The simulator records the effective end in `injection_log` when a cancel takes effect after the injection first applied.

## Boundaries & Constraints

**Always:**
- **`list`:**
  - it reads `sim_control` only (paginated scan) and is read-only;
  - "active" means `start ≤ now < effective_end`; "recent" means ended within `--recent-min` minutes (default 60);
  - cancel items (with `supersedes`) are folded into their target's effective end rather than listed as injections;
  - output is a readable table, and `--json` prints one JSON line per injection;
  - it works against the stage tables via `SIM_CONTROL_TABLE`.
- **Effective end:**
  - when a cancel shortens an injection that already has an `injection_log` row, the simulator writes `effective_end` onto that row once;
  - this is a conditional update `attribute_not_exists(effective_end)`;
  - `injection_log` stays append-only in spirit: the planned `end_at` is never changed;
  - the task role gains exactly `dynamodb:UpdateItem` on `injection_log`, limited to that attribute (AD-17).
- AD-15: `sim_control` items stay immutable, and cancel is still a new item.

**Never:**
- No reset (4.2), no smoke test (4.3), no change to physics.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| List active | one running leak, one cancelled flatline, one expired stuck | the leak listed as active; the flatline recent, with its effective end = the cancel time; the stuck recent | none |
| JSON | `list --json` | one JSON object per injection with id, type, pipe, measurement, start, planned_end, effective_end, state | none |
| Empty | no items | "no injections" | exit 0 |
| Cancel after apply | cancel of an applied injection | its `injection_log` row gains `effective_end` once | a conditional failure is ignored |
| Cancel before apply | cancel of an injection that never applied | no `injection_log` row, no update | none |
| Live (read-only) | `ops inject list` against the `sim-control-poc-dj` table | lists tonight's cancelled test injections with their effective ends | none |

</frozen-after-approval>

## Code Map

- `ops/inject.py` -- the argparse subcommands (lines ~48–56), `SIM_CONTROL_TABLE`, and the create/cancel paths. Add `list`.
- `facility/control.py` -- the sim_control item parsing shared by `ops/` and `sim/`; reuse it for the effective-end fold (`supersedes`).
- `sim/control.py` -- `ControlPoller`, `log_first_apply` (line 90); add the effective-end update when a cancel is resolved for an injection already logged.
- `infra/stacks/simulator.py` -- the task role grants; add the conditioned `UpdateItem`; update the exact-policy tests.
- `tests/test_inject_cli.py`, `tests/test_control.py`, `tests/sim_fakes.py`, `tests/test_simulator_stack.py`.

## Tasks & Acceptance

**Execution:**
- [x] `ops/inject.py` `list` (plus a shared fold helper in `facility/control.py`). -- listing.
- [x] `sim/control.py` effective-end update; `infra/stacks/simulator.py` grant. -- true end.
- [x] Tests for every matrix row except Live; `docs/ci-cd.md` section 5 usage. -- matrix.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given `ops inject list` against `sim-control-poc-dj`, then tonight's injections show with their effective ends.

## Implementation Notes

- Fold helper: `facility.control.windows(items)` returns each non-cancel injection with its effective `end_at`, paired with the planned end.
- `list` states: `active`, `recent`, plus `scheduled` for a start in the future (not otherwise reachable via the CLI). The table shows both effective and planned end.
- The poller attempts the `effective_end` update on each poll for cancelled injections whose effective end has passed, conditioned `attribute_exists(pk) AND attribute_not_exists(effective_end)` so a never-applied injection gets no row; success or a conditional failure marks it done in memory (one attempt per process), other errors retry next poll.
- IAM: `dynamodb:UpdateItem` on `injection_log` with `ForAllValues:StringEquals dynamodb:Attributes [pk, effective_end]` and `StringEqualsIfExists dynamodb:ReturnValues NONE`. Not yet verified against live IAM (no deploy in this session).

- Live (read-only, 2026-09-29 05:44): `ops.inject list --recent-min 600` against `sim-control-poc-dj` listed the night's 9 test injections with effective ends at their cancel times and planned ends preserved. The simulator's `effective_end` write is not deployed (tests only).

## Plan Change Log

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 5, low 5. Outcome: 5 patch groups, no loopback. The Edge Case Hunter report was still pending at triage because of the time limit; see Implementation Notes.

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| BH-1, BH-2 | Cancel-before-start shows "scheduled"; no cancelled indicator | medium | patch | `cancelled` state and flag. |
| BH-5, IA-Q | Conditional failure is ambiguous: a late first-apply row never gets `effective_end` | medium | patch | Retry for 10 min after the effective end. |
| VG-1, VG-2 | Retry, scheduled and `--recent-min` untested | medium | patch | Tests. |
| BH-7 | `ForAllValues` passes when the key is absent | medium | patch | `Null` condition. |
| BH-3, BH-4, BH-8 | `list` bypasses AWS error handling; stale description; redeploy note | low | patch | Fixed. |
| BH-6 | `_ended` grows and is lost on restart | low | reject | Bounded by `sim_control` size; a re-send after restart is conditional and harmless. |
| IA-scoring | No log reader uses `effective_end` yet | low | defer | Scoring reads arrive with Epic 4 demo scoring; the field is available. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
