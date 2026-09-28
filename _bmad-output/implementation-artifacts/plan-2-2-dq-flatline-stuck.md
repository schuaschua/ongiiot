---
title: 'Story 2.2: Flag flatlined and stuck sensors'
type: 'feature'
ticket: 'OG-11'
created: '2026-09-29'
status: 'in-review'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: 'cc5ceca79af526f0896f61fddabb7aea807cf786'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Every sensor scores 1.0 today, even one that is railed or frozen. OT engineers need flatlined and stuck sensors to lose trust within two runs, with the failing check named (Story 2.2, OG-11; FR3; AD-5, AD-13).

**Approach:** Two pure pipe detectors in `detect/core/detectors/`, `dq_flatline` and `dq_stuck`, registered in pass 1. They look at the trailing run of each sensor in the window, and their confidence grows with the run's length, so `dq_score` turns them into a lower `_dq` and a named `_dq_fail`. Adds the typed detector protocol deferred from Story 2.1.

## Boundaries & Constraints

**Always:**
- AD-5:
  - detectors import types only from `detect/core/models.py`;
  - each declares `id`, `version` and `kind = "pipe"`;
  - no I/O and no environment; they never compute `_dq`;
  - each `dq_*` finding names exactly one alias, and has a confidence in [0, 1] and a one-sentence explanation.
- Semantics (autonomous decision, logged):
  - **The trailing run:** only the run of samples ending at the sensor's last sample counts. So recovery clears the finding on the next run.
  - **`dq_flatline`:** a trailing run of samples all within `rel_tol` of the sensor's contract `min` or `max` (railed).
  - **`dq_stuck`:** a trailing run of exactly identical values that is *not* railed. A railed run belongs only to `dq_flatline`, so the two never both fire on one sensor.
  - **Confidence:** `0` below `min_run_s`, otherwise `min(1, run_s / full_s)`.
  - Only GOOD samples with values count.
- Tunables live only in `facility.yaml` under `tunables.detectors.<id>`, and the detector owns its keys:
  - `dq_flatline: {min_run_s: 30, full_s: 60, rel_tol: 1.0e-6}`
  - `dq_stuck: {min_run_s: 30, full_s: 60}`

  Missing keys fail fast at cold start.
- `detect/core/models.py` gets `PipeDetector` / `FacilityDetector` protocols (`id`, `version`, `kind`, `detect`), and `handler.run` dispatches by `kind`. This is the Story 2.1 deferral.
- No false positives on normal simulated data: a 15-minute window from the real simulator physics with no injections produces no findings.

**Never:**
- No `dq_drift` or `dq_gap` detectors (Story 2.3), leak or outlier detectors (Epic 3), or Grafana changes (2.4).
- No change to simulator behaviour, the reader, the writer or IAM.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Railed | `flow_in` at its contract min for ≥ 60 s at the window end | `dq_flatline` confidence 1.0 → `_dq` 0.0, `_dq_fail` `dq_flatline` | none |
| Frozen | `temperature` repeating one mid-range value for ≥ 60 s | `dq_stuck` confidence 1.0 → `_dq` 0.0, `_dq_fail` `dq_stuck` | none |
| Short run | identical values for 20 s | no finding | none |
| Partial | stuck for 45 s | confidence 0.75 → `_dq` 0.25 (< `dq_min`) | none |
| Recovered | stuck earlier, varying again for the last 10 s | no finding | none |
| Normal | 15 min of real simulator output, no injections | no findings on any of 144 sensors | none |
| Others | a fault on one measurement | other sensors keep `_dq = 1.0` | none |
| Live | `poc-dj`: `dq_flatline` and `dq_stuck` injections via `ops/inject.py` | within two runs, those sensors show `_dq < 0.5` and the right `_dq_fail`; the rest stay 1.0 | recorded; injections cancelled and the simulator switched off afterwards |

</frozen-after-approval>

## Code Map

- `detect/core/models.py` -- v1 types (`Sample`, `Contract`, `PipeMeta`, `PipeWindow`, `Finding`, `Tunables`). Add the protocols here.
- `detect/core/dq_score.py` -- `score()`, `check_finding()`, `score_facility()` already turn findings into `(dq, failing)`. Do not change the scoring.
- `detect/handler.py` -- `PASS1_DETECTORS = ()`; per-detector isolation and validation are already in place (Story 2.1 review). Register the two detectors and dispatch by `kind`.
- `sim/physics.py:18-19, 270-276` -- how the simulator produces the faults (flatline = contract `min`; stuck = last value held, GOOD). Tests may use `sim.physics` to build a realistic no-fault window (the layer rule binds `detect/`, not `tests/`).
- `facility/facility.yaml` tunables (`detectors:` section) and `facility/model.py` tunables validation.
- `ops/inject.py` -- creates and cancels injections; needs `SIM_CONTROL_TABLE=sim-control-poc-dj`.
- `tests/test_detect_handler.py`, `tests/detect_fakes.py` -- run-level test pattern.

## Tasks & Acceptance

**Execution:**
- [x] `detect/core/models.py` -- `PipeDetector` / `FacilityDetector` protocols. -- typed contract.
- [x] `detect/core/detectors/__init__.py`, `dq_flatline.py`, `dq_stuck.py` -- as in Boundaries; read tunables from `t["detectors"][id]`. -- the checks.
- [x] `detect/handler.py` -- register both in `PASS1_DETECTORS`; dispatch pipe and facility detectors by `kind`; validate that each detector's tunables exist at cold start. -- wiring.
- [x] `facility/facility.yaml` -- the tunable values above. -- config.
- [x] `tests/test_dq_detectors.py` -- every matrix row except Live, on synthetic windows, plus the "Normal" row on real simulator physics output. -- matrix.
- [x] `tests/test_detect_handler.py` -- a run-level test: a railed alias and a stuck alias give the expected `_dq` / `_dq_fail`, and the other 142 sensors stay at 1.0/"". -- end to end.
- [x] `docs/ci-cd.md` §7 -- what each check means and how to try it with `ops/inject.py`. -- operability.
- [ ] Live (after green): deploy `poc-dj` with the simulator on and wait about 16 min for a full window.
  - Inject `dq_flatline` on P-003 `flow_in` and `dq_stuck` on P-015 `temperature`, each for 10 minutes.
  - After two or three runs, read the four derived values and a sample of untouched ones.
  - Record the results; cancel the injections; deploy with `simulator=off`.

  -- the story's acceptance.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given the live check, then P-003 `flow_in_dq < 0.5` with `dq_flatline`, P-015 `temperature_dq < 0.5` with `dq_stuck`, and the other sensors are at 1.0.

## Implementation Notes

- **Run length** is the span of the trailing run's timestamps (last minus first GOOD sample), so it does not depend on `rate_hz`; 46 samples at 1 Hz = 45 s = confidence 0.75. A confidence of 0 (below `min_run_s`) emits no finding.
- **Railed** = within `rel_tol` x (contract `max - min`) of `min` or `max`, all samples of the run at the same rail. `rel_tol` is scaled by the span because contract minimums are often 0.
- **One definition of railed:** `dq_stuck` calls `dq_flatline.rail_of`, which reads `dq_flatline`'s own `rel_tol`. `dq_stuck` owns only `min_run_s` / `full_s` (as the frozen Boundaries require) and never reads another detector's key directly; this is what guarantees the two never both fire.
- **Non-finite values** (NaN, inf) are treated like missing values: ignored.
- **Tunables check:** each detector has `check_tunables(t)` (finite numbers; `min_run_s >= 0`, `0 < full_s`, `full_s >= min_run_s`, `rel_tol >= 0`). `handler.check_detectors` calls it plus a `kind` check for every registered detector in `runtime()` (cold start), so a missing key fails the cold start. The protocols stay the four members named in the plan; `check_tunables` is optional and found by `getattr`.
- **Dispatch:** `pipe` detectors run once per pipe and their findings must carry that pipe's id; `facility` detectors run once per run and each `dq_*` finding must name an alias of the pipe it names. An unknown `kind` is a logged detector failure at run time and a cold-start error.
- Shared helpers (good-sample filter, trailing run, confidence, tunables validation) live in `detect/core/detectors/_trailing.py`; detectors import types only from `detect/core/models.py`.
- `tests/test_detect_handler.py`'s synthetic healthy store now varies every second (it was a constant `1.0`, which `dq_stuck` now rightly flags).

- **After review (round 1):** a non-GOOD or valueless sample ends a run (trailing non-GOOD samples are skipped back to the last GOOD one); a gap over 2 x the contract sample period (2 s without a rate) splits a run; railed is one-sided (`<= min + tol`, `>= max - tol`), so over/under-range values count; `0 <= rel_tol < 0.5`; `min_run_s <= full_s < window_len_s`; `dq_stuck.check_tunables` also validates `dq_flatline.rel_tol`; duplicate detector ids fail the cold start; `check_tunables` is on both protocols; empty units are omitted from explanations.

## Plan Change Log

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 6, low 19, false 1. Outcome: 9 patch groups; no loopback; the timing question is resolved by an autonomous decision (see the overnight log).

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| BH-2, VG-o1, ECH-1 | `dq_stuck` depends on `dq_flatline.rel_tol` without validating it | medium | patch | Validated in `DqStuck.check_tunables`. |
| ECH-2 | `rel_tol >= 0.5` marks every value railed | low | patch | Range check. |
| BH-3, ECH-3 | `min_run_s`/`full_s` not checked against `window_len_s` | medium | patch | Cold-start cross-check. |
| ECH-4, ECH-5 | Trailing run bridges non-GOOD stretches and long gaps | medium | patch | A non-GOOD sample ends the run; split where the gap exceeds 2× the sample period. |
| BH-4 | Over- and under-range values are not "railed" | medium | patch | One-sided rail test. |
| BH-8, ECH-8, BH-7 | Duplicate detector ids; protocols unused | low | patch | Cold-start id check; handler typed on the protocols. |
| VG-1 | Pipe-id mismatch guard untested | medium | patch | Test added. |
| BH-10 | Unit `None` in explanations | low | patch | Unit omitted when empty. |
| BH-6, BH-10b, ECH-11, IA timing | Inverted doc sentence; incomplete manual test; timing claim | medium | patch (doc) + decision | Timing: two runs after the fault enters the window (AD-9 ingest lag); documented as 2–3 minutes. |
| ECH-7, BH-5 | Legitimate shut-in at the contract min; quantized sensors flagged stuck | low | reject (doc) | Simulated data only; documented as a known limit. |
| BH-9 | Finding window is not the run start | low | reject | The `Finding` v1 contract defines the window. |
| BH-11 | Per-sample `rail_of` cost | low | reject | Negligible at 144 sensors. |
| ECH-6, ECH-9, ECH-10 | bool values; unvalidated custom detector lists; missing id in the except handler | low | reject | Unreachable through the registered detectors and the protocol. |
| BH-1 | "No tests in diff" | false | reject | The lens got the code-only diff; 468 tests exist (`tests/test_dq_detectors.py`, `test_detect_handler.py`). |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
