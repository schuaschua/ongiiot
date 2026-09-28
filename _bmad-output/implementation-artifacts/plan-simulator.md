---
title: 'Simulator: live facility stream with fault and leak injection'
type: 'feature'
ticket: ''
created: '2026-09-28'
status: 'draft'
route: 'full'
route_source: 'auto'
review: ''
review_source: ''
lenses_ran: []
review_loop_iteration: 0
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
- [ ] `facility/control.py` -- sim_control item v1 dataclass, validation against the catalogue and model, `effective(items, now)` resolving windows and `supersedes`, and conversion to and from DynamoDB items; shared by `sim/` and `ops/`. -- AD-15 shape lives here.
- [ ] `sim/physics.py` -- pure, seeded per-pipe model: `step(state, t, active) -> (state, samples)`. Steady state set by `service` inside the contracts; slow ambient temperature cycle; small correlated noise; mass-balanced flows; the injection effects in the matrix. -- CAP-1 and CAP-4 realism.
- [ ] `sim/writer.py` -- raw writer: buffers samples; flushes with `BatchPutAssetPropertyValue` using at most 10 entries and 10 values per entry; refuses `derived/` and `attr/`; enforces strictly increasing per-alias timestamps; retries throttling. -- AD-1, AD-3, AD-17 code guard.
- [ ] `sim/control.py` -- poller: scans `sim_control` every 5 s or less; logs first applies to `injection_log` with `attribute_not_exists(pk)`. -- AD-4, AD-15.
- [ ] `sim/main.py` -- entry point `python -m sim.main`: load the model by `STAGE`; alias association check; 1 Hz wall-clock loop; flush every `FLUSH_S` (default 5); JSON logs; clean SIGTERM. The clock and AWS clients are injectable for tests. -- AD-1, AD-9.
- [ ] `ops/inject.py` -- CLI `inject {leak,dq_flatline,dq_stuck,dq_drift,dq_gap} --pipe P --measurement M --minutes N [--rate-kg-s X] [--per-min X]` and `cancel <injection_id>`; validates through `facility/control.py`; writes only `sim_control`. -- AD-15.
- [ ] `tests/test_control.py`, `tests/test_physics.py`, `tests/test_sim_writer.py`, `tests/test_sim_runtime.py`, `tests/test_inject_cli.py` -- one or more tests per matrix row, using fakes only. -- matrix coverage.

**Acceptance Criteria:**
- Given the shipped model, when the runtime runs 60 simulated seconds against a fake SiteWise, then all 144 raw aliases receive 60 GOOD values with strictly increasing timestamps and no out-of-contract values.
- Given a leak injected at t=0 on P-007, when 120 simulated seconds pass, then P-007's `flow_in − flow_out` and `methane_ppm` sit clearly outside their no-injection noise band, and no other pipe changes.

## Design Notes

- Physics is pure, so tests drive simulated time without sleeping. Only `sim/main.py` touches the clock and AWS.
- Default leak `rate_kg_s` = 10% of the pipe's mean flow. Methane rises toward a plateau of roughly 500 ppmv or more above a background of about 2 ppmv.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass, including the existing 92.
- `uv run python -m ops.inject --help` -- expected: usage printed, exit 0.
