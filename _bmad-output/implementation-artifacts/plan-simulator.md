---
title: 'Simulator: live facility stream with fault and leak injection'
type: 'feature'
ticket: 'OG-6'
created: '2026-09-28'
status: 'done'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: '9ecd067f12ba6fab0ea63d16cf0ce8e648c0da96'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** There is no client data. The POC needs a live, believable 1 Hz stream for every modelled sensor (CAP-1), with faults and leaks that can be injected on demand so detection can be demonstrated (CAP-3, CAP-4). The stream must arrive the same way the real stream will (AD-1).

**Approach:** A Python simulator generates plausible values per pipe from `facility.yaml` and writes them to SiteWise by raw alias in real time. It applies injections it reads from `sim_control` and logs each one to `injection_log` the first time it applies. A minimal `ops/inject.py` creates and cancels injections. Packaging and deployment (Docker, CDK, Fargate) are a later build.

## Boundaries & Constraints

**Always:**
- Follow the spine: AD-1 (alias ingestion only; `DescribeTimeSeries` check at startup), AD-3 (raw is write-once; per-alias timestamps strictly increase; no clamping; restart resumes at wall-clock now), AD-4 (injection instances only in `sim_control` and `injection_log`), AD-9 (UTC, real time, no backfill or acceleration), AD-15 (poll ≤ 5 s, read-only; items immutable; cancel = a new item with `supersedes`).
- Use the sim_control item v1 shape and the spine's injection catalogue. Aliases and contracts come only from `facility/model.py`.
- Table names and `STAGE` come from environment variables, so the later CDK stack can set them.
- Size the default leak so that flow imbalance and methane rise are clearly above normal noise within 2 minutes.

**Never:**
- No Dockerfile, CDK, IAM, detection, reset, smoke or Grafana work.
- No writes to `derived/` or `attr/` aliases.
- No AWS calls in tests; use fakes only.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Normal | no injections | every raw alias gets one GOOD value per second, within its contract; `flow_out ≈ flow_in`; peers look similar | No error |
| Leak | `leak` on P-007, default `rate_kg_s` | `flow_out` drops by the rate; `pressure_out` drops; `methane_ppm` rises; other pipes are unaffected | No error |
| dq_flatline | on one measurement | constant value | No error |
| dq_stuck | on one measurement | last value repeated, quality GOOD | No error |
| dq_drift | `per_min` | linear offset growing from `start_at` | No error |
| dq_gap | on one measurement | no samples while active | No error |
| Window | now outside `[start_at, end_at)` | not applied | No error |
| Cancel | a new item with `supersedes: X` | X stops at the canceller's `end_at` | No error |
| First apply | injection becomes active | exactly one `injection_log` row, even across restarts | conditional put; a duplicate is ignored |
| Derived alias | writer asked to write `.../derived/...` or `.../attr/...` | refuses | raises before any API call |
| Unassociated alias | `DescribeTimeSeries` shows no asset for an alias | simulator refuses to start | exits non-zero, naming the alias |
| Invalid inject | unknown type, a dq type without measurement, unknown pipe | `ops/inject.py` refuses | non-zero exit, no write |

</frozen-after-approval>

## Code Map

- `facility/model.py` -- reuse `load(stage=)`, `Facility.alias()`, `pipe.contracts`, `pipe.service`, `peer_groups()`. Do not change its behaviour.
- `tests/test_sitewise_stack.py` -- `FakeSiteWise` style for AWS fakes. Follow it.
- `pyproject.toml` -- add `python-ulid` as a runtime dependency.

## Tasks & Acceptance

**Execution:**
- [x] `facility/control.py` -- sim_control item v1 dataclass, validation against the catalogue and model, `effective(items, now)` resolving windows and `supersedes`, and conversion to and from DynamoDB items; shared by `sim/` and `ops/`. -- AD-15 shape lives here.
- [x] `sim/physics.py` -- pure, seeded per-pipe model: `step(state, t, active) -> (state, samples)`. Steady state set by `service` inside the contracts; slow ambient temperature cycle; small correlated noise; mass-balanced flows; the injection effects in the matrix. -- CAP-1 and CAP-4 realism.
- [x] `sim/writer.py` -- raw writer: buffers samples; flushes with `BatchPutAssetPropertyValue` using at most 10 entries and 10 values per entry; refuses `derived/` and `attr/`; enforces strictly increasing per-alias timestamps; retries throttling. -- AD-1, AD-3, AD-17 code guard.
- [x] `sim/control.py` -- poller: scans `sim_control` every 5 s or less; logs first applies to `injection_log` with `attribute_not_exists(pk)`. -- AD-4, AD-15.
- [x] `sim/main.py` -- entry point `python -m sim.main`: load the model by `STAGE`; alias association check; 1 Hz wall-clock loop; flush every `FLUSH_S` (default 5); JSON logs; clean SIGTERM. The clock and AWS clients are injectable for tests. -- AD-1, AD-9.
- [x] `ops/inject.py` -- CLI `inject {leak,dq_flatline,dq_stuck,dq_drift,dq_gap} --pipe P --measurement M --minutes N [--rate-kg-s X] [--per-min X]` and `cancel <injection_id>`; validates through `facility/control.py`; writes only `sim_control`. -- AD-15.
- [x] `tests/test_control.py`, `tests/test_physics.py`, `tests/test_sim_writer.py`, `tests/test_sim_runtime.py`, `tests/test_inject_cli.py` -- one or more tests per matrix row, using fakes only. -- matrix coverage.

**Acceptance Criteria:**
- Given the shipped model, when the runtime runs 60 simulated seconds against a fake SiteWise, then all 144 raw aliases receive 60 GOOD values with strictly increasing timestamps and no out-of-contract values.
- Given a leak injected at t=0 on P-007, when 120 simulated seconds pass, then P-007's `flow_in − flow_out` and `methane_ppm` sit clearly outside their no-injection noise band, and no other pipe changes.

## Design Notes

- Physics is pure, so tests drive simulated time without sleeping. Only `sim/main.py` touches the clock and AWS.
- Default leak `rate_kg_s` = 10% of the pipe's mean flow. Methane rises toward a plateau of roughly 500 ppmv or more above a background of about 2 ppmv.

## Implementation Notes

- Merged: commit `ce617b8` on `main` (before pull requests were used).

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass, including the existing 92.
- `uv run python -m ops.inject --help` -- expected: usage printed, exit 0.

## Review Triage Log

**Pass 1** (lenses: blind-hunter, edge-case-hunter, verification-gap, intent-alignment). Verdicts: high 0, medium 9, low 17, false 3, maybe-false 0. There were no intent_gap or bad_plan entries, so the patch and defer entries are processed.

| # | Lens | Finding | Verdict | Route | Evidence / action |
|---|------|---------|---------|-------|-------------------|
| 1 | blind, edge | `resolve()` can extend an injection when the cancel ends later than the injection | medium | patch | `control.py:230` replaces `end_at` unconditionally. Use `min(inj.end_at, cancel_end)`. |
| 2 | blind, edge | One failed batch drops the rest of the flush | medium | patch | `writer.py:121-122` exits the loop on the first `WriteError`. Send every batch, then raise one aggregate error. |
| 3 | blind, edge | Retries stack (boto 5 attempts plus the writer's 6) and block the 1 Hz loop for many seconds | medium | patch | `main.py:227` and `writer.py:134,144`. Keep one retry layer and cap the writer's total backoff per flush at 1 s or less. |
| 4 | blind, edge | `ops/inject` gives a traceback on AWS errors and on `--minutes inf` or huge values | medium | patch | `inject.py:93,119` only catch `ControlError`, and the minutes check allows inf. Map `ClientError` to `_error`; require minutes finite and within a maximum. |
| 5 | blind, edge | `ops/inject` ignores `FACILITY_PATH` | low | patch | `inject.py` always loads the default. Direct fix: honour it. |
| 6 | blind, edge | A non-string `type` or `supersedes`, or an empty `supersedes`, crashes the poller or counts as a cancel | medium | patch | The `TypeError` escapes the `ControlError` handler, so every scan crash-loops. Add type checks in validation. |
| 7 | blind, edge | Cancelling an injection that already expired or was already cancelled is accepted | low | reject | Harmless once #1 is fixed; the guard would add branches. |
| 8 | blind, edge | `injection_log` keeps the planned `end_at` after a cancel | medium | defer | Real, but the fix adds an update path. The log is only used for demo scoring. |
| 9 | edge | A clamped leak rate is logged as requested | low | reject | Only happens above 90% of flow. Unlikely. |
| 10 | edge | A log write that keeps failing is never retried after the window ends | low | reject | Needs a sustained DynamoDB outage. |
| 11 | edge | A huge `per_min` overflows and crash-loops | low | reject | An absurd input; it would need a cap. |
| 12 | edge | A large backwards clock jump delays SIGTERM | low | reject | Unlikely on Fargate. |
| 13 | blind, edge | Non-ResourceNotFound `DescribeTimeSeries` errors give a traceback | low | patch | Direct fix: wrap in `StartupError` so the process exits 3 with a JSON log line. |
| 14 | edge | `physics.py:233` divides by zero if the baseline flow is 0 | low | patch | Direct guard, as on the neighbouring line. |
| 15 | edge | The methane plateau cap can go negative | low | patch | Direct fix: `max(0.0, ...)`. |
| 16 | edge | Non-canonical fractional timestamps are accepted | low | reject | `strptime` `%f` parses them correctly. Harmless. |
| 17 | blind, impl | `boto3` is not a runtime dependency | medium | patch | `sim/main.py` and `ops/inject.py` import it. Move it to `dependencies`. |
| 18 | vgap | Flush failure → stream continues is untested | medium | patch | Add a runtime test with a failing fake. |
| 19 | vgap, blind | Scan failure keeping the current injections is untested | medium | patch | Add a runtime test whose scan raises. |
| 20 | vgap | Retry of a transient `injection_log` failure is untested | low | patch | Add the test. |
| 21 | vgap | The default `dq_drift` rate is untested | low | patch | Add `test_default_drift_rate`. |
| 22 | blind | `sim_control` grows without limit and is fully scanned | low | reject | Tiny over a 4-day POC. |
| 23 | blind | No `list` command in `ops/inject` | low | defer | A usability gap for demo operations; new public surface. |
| 24 | blind | `JsonFormatter` fields overwrite reserved keys | low | patch | Direct fix: reserved keys win. |
| 25 | blind | Duplicated `_error_code`; dead `effective_end` | low | patch | Delete the dead `effective_end`. The duplication is tolerable. |
| 26 | blind | Tests missing for overlapping injections, bad env values, and a 24 h run staying in contract | low | patch | Add only the 24 h in-contract test. It is cheap, and the ambient peak is otherwise unchecked. |
| 27 | blind | Plan frontmatter claims lenses ran | false | reject | The review was in progress when the lens read the plan. |
| 28 | intent | Real-AWS and live-time surfaces are not exercised | false | reject | The human deferred deployment; fakes only by the frozen boundary. |
| 29 | intent | Detector-surface detectability is untested | false | reject | Detection is a later build; the simulator-side bands are the agreed proxy. |
| 30 | orchestrator | Patch #3 turned off SDK retries on the only SiteWise client, so a throttled describe of any of the 144 aliases at startup would refuse to start | medium | patch | Fixed directly: `_aws` returns a separate startup client with standard retries (5 attempts), used only by `check_aliases`. The writer client keeps no SDK retries. |

Post-patch verification: `uv run pytest -q` 191 passed; `cdk synth -c stage=poc-dj` exit 0; `python -m ops.inject --help` exit 0.
