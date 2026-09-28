---
title: 'Story 3.1: Detect leaks from mass balance and methane rise'
type: 'feature'
ticket: 'OG-14'
created: '2026-09-29'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: '8ade86cdf574f66b244de4f08664cd9d1f4efae7'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing detects a loss of containment. Integrity engineers need leaks caught within minutes from two independent signals, flow imbalance and a methane rise, each explained and backed by evidence, and never raised on healthy pipes (Story 3.1, OG-14; FR4; AD-5 pass 2).

**Approach:** Add pass 2 to the detection run, with two pure pipe detectors: `leak_mass_balance` and `leak_methane_rise`. Pass 2 runs after pass 1 has scored every sensor. Sensors below `dq_min` are excluded, and the explanation says so. Findings are logged in the run summary. Incidents, alert-state writes and email come in Stories 3.3 and 3.4.

## Boundaries & Constraints

**Always:**
- AD-5 pass 2:
  - it runs after `dq_score` fills `PipeMeta.dq`;
  - leak detectors receive the dq-annotated `PipeWindow`;
  - they exclude any sensor with `dq < tunables.dq_min`; if a required sensor is excluded, there is no finding; an optional corroborating sensor that is excluded is named in the explanation;
  - per-detector isolation and validation as in pass 1.
- Each finding:
  - names the pipe;
  - has a confidence in [0, 1];
  - has `evidence_aliases`, the raw aliases that drove it;
  - has a one-sentence explanation with the key numbers.
- Semantics (autonomous decision, logged):
  - **`leak_mass_balance`:** the trailing mean of `(flow_in − flow_out) / flow_in` over `avg_s`. Confidence is `0` below `min_frac`, otherwise `min(1, frac / full_frac)`. Required: `flow_in`, `flow_out`. A matching `pressure_out` drop is corroboration only, cited when present.
  - **`leak_methane_rise`:** the trailing mean of `methane_ppm` minus the median of the same measurement on the pipe's peer group (peer medians are passed in; see Code Map). Confidence is `0` below `min_ppm`, otherwise `min(1, excess / full_ppm)`. Required: `methane_ppm`.
- Tunables go under `tunables.detectors.leak_*`, calibrated on simulator physics:
  - no finding on any pipe across normal 30-minute runs (several seeds and times of day);
  - a default leak (10% of mean flow) on P-007 gives both findings at confidence ≥ 0.8 within two runs of entering the window.
- The run summary log line gains `findings: [{detector, pipe, confidence}]` for pass 2.

**Never:**
- No incident table, alert-state writes, SNS or deep links (3.3, 3.4).
- No outlier detector (3.2).
- No change to pass 1 scoring, the reader, the writer or IAM.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Leak | default `leak` on P-007 in the window for ≥ 2 min | `leak_mass_balance` and `leak_methane_rise` findings for P-007, confidence ≥ 0.8, evidence aliases and explanations | none |
| Healthy | 30 min of normal simulator output, all pipes | no leak findings | none |
| Untrusted flow | P-007 `flow_out_dq < dq_min` during a leak | no `leak_mass_balance` (required sensor excluded); methane finding still possible | none |
| Untrusted corroboration | P-007 `pressure_out` untrusted | mass-balance finding still made; explanation says `pressure_out` was excluded | none |
| Isolation | a leak on P-007 only | no findings on other pipes | none |
| Live | `poc-dj`: `ops.inject leak --pipe P-007 --minutes 10` | both P-007 findings in the detection summary within about 4 min; none elsewhere | recorded; injection cancelled; simulator switched off |

</frozen-after-approval>

## Code Map

- `detect/handler.py`:
  - `run()` does pass 1, then `score_facility`, then writes;
  - it already rebuilds the dq-annotated `fw` (Story 2.1 left this in place for pass 2);
  - add `PASS2_DETECTORS` and run them over the annotated window with the same `_run_detector` isolation;
  - add `findings` to the summary.
- `detect/core/detectors/` -- `__init__.py` (`PASS1`), `_trailing.py` helpers, and `dq_drift.py`, which has the peer-median logic (peer groups via `PipeMeta.peer_group`). A peer-median helper can be shared.
  - Because `leak_methane_rise` needs peers, it can be a **facility** detector that emits per-pipe findings. That is allowed (AD-5 `kind`), and the plan permits either kind as long as each finding names one pipe.
- `detect/core/models.py` -- `Finding` (`evidence_aliases`, `confidence`, `explanation`), `PipeMeta.dq`.
- `sim/physics.py:13-17, 45-58` -- leak effects (`flow_out = flow_in − rate`, a `pressure_out` drop, methane toward about 6000 ppmv per unit leak fraction) and the noise levels for calibration.
- `facility/facility.yaml` -- `detectors.leak_mass_balance: {}`, `leak_methane_rise: {}`.

## Tasks & Acceptance

**Execution:**
- [x] `detect/core/detectors/leak_mass_balance.py`, `leak_methane_rise.py`; `PASS2` registry. -- the detectors.
- [x] `detect/handler.py` -- run pass 2 on the annotated window; add findings to the summary; check tunables at cold start. -- wiring.
- [x] `facility/facility.yaml` -- calibrated tunables, with the evidence in Implementation Notes. -- config.
- [x] Tests: every matrix row except Live, on synthetic windows and simulator physics, including the 30-minute no-false-positive runs and dq exclusion. -- matrix.
- [x] `docs/ci-cd.md` §7 -- what the leak checks mean, where to see findings (summary log) until 3.3, and how to try them. -- operability.
- [x] Live (the orchestrator runs this). -- acceptance.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given the live check, then the detection summary shows both leak findings for P-007 and none elsewhere.

## Implementation Notes

- **Pass 2 wiring** (`detect/handler.py`): `PASS2_DETECTORS = PASS2` run after `score_facility` on the dq-annotated `fw`, through the same `_run_detector` isolation (a failing or malformed detector is logged on its own line, left out of `ran_ok`, the others continue). Pass-2 findings are validated by `check_pass2_finding`: not a `dq_*` id, finite confidence in [0, 1], at least one evidence alias, all of the finding's own pipe, non-empty explanation. `run(..., pass2=PASS2_DETECTORS)`; summary `detectors` and `ran_ok` now cover both passes, and `findings: [{detector, pipe, confidence}]` (unrounded) lists pass 2 only. Cold start checks `(*PASS1, *PASS2)` (so duplicate ids across passes also fail). Pass 2 writes nothing; the dq writes are unchanged.
- **Trust rule** (`_pass2.py`): a sensor is trusted only if `PipeMeta.dq[m]` is present, finite and `>= dq_min` (a missing score is untrusted, fail safe). `dq_min` is validated by both detectors' `check_tunables`.
- **`leak_mass_balance`** (pipe): per whole second in `[end - avg_s, end)` with GOOD `flow_in > 0` and `flow_out`, `(in - out) / in`; mean over pairs; no finding below `min_points` pairs or `min_frac`; confidence `min(1, frac / full_frac)` exactly as frozen. Negative loss (out > in) is never a leak.
- **Autonomous decision: pressure corroboration is measured against `pressure_in`.** Measured on 4 seeds x 24 h of normal physics (per-minute trailing 60 s means, all 24 pipes): a plain `pressure_out` drop (trailing 60 s vs the rest of the window) reached 0.79% because the process disturbance moves both pressures, while a default leak drops it about 1.03%, so it cannot tell. The ratio `pressure_out / pressure_in` cancels the shared swing: normal max drop 0.40%, leak 0.86-1.03% by the second run. So the corroboration uses the ratio, needs both pressures trusted, and cites both aliases as evidence. If `pressure_out` is untrusted the explanation says "pressure_out was excluded as untrusted (dq 0.20 < dq_min 0.5)"; if only `pressure_in` is, "pressure_out was not checked because pressure_in was excluded ...". Corroboration never changes the confidence.
- **`leak_methane_rise`** (facility, per-pipe findings): trailing `avg_s` mean of GOOD `methane_ppm` (at least `min_points`), minus the median of the same means on the other trusted pipes of the `peer_group` (at least 2, as `dq_drift`); untrusted peers are left out and named. The shared `median` / `peer_groups` helpers moved to `detect/core/detectors/_peers.py` (`dq_drift` now imports them; its behaviour is unchanged, all pass-1 tests pass).
- **Calibrated tunables:** `leak_mass_balance: {avg_s: 60, min_points: 30, min_frac: 0.02, full_frac: 0.08, pressure_drop_frac: 0.006}`, `leak_methane_rise: {avg_s: 60, min_points: 30, min_ppm: 30, full_ppm: 300}`.
  - Normal physics, 4 seeds x 24 h, per-minute trailing means, all pipes: loss fraction max 0.16% (12x below `min_frac`), negative max 0.17%; methane excess over peer median max 0.60 ppmv (50x below `min_ppm`); ratio pressure drop max 0.40% (below 0.6%).
  - Default leak (10% of mean flow) on P-007, full pipeline (pass 1, `dq_score`, pass 2) on physics windows, by seconds of leak in the window: 15 s -> 0.31 / 0.16 (mass balance / methane); 30 s -> 0.62 / 0.49; 45 s -> 0.93 / 0.92; >= 60 s -> 1.0 / 1.0. The second run after a leak enters the window always holds >= 61 s of it, so both reach 1.0 (tested for 1, 17, 30, 45, 60 s at the first run). No pass-1 finding is raised by the leak, and no other pipe gets a finding. Same result on P-003, P-010, P-016, P-020 (all four peer groups).
  - `avg_s` 60 is what makes "within two runs" hold with margin: a longer average dilutes a new leak; shorter adds noise for nothing (normal is already far below the thresholds).
- **Tests:** `tests/test_leak_detectors.py` (synthetic rows, confidence ramps, trust and exclusion rules, peers, window-end exclusivity, and the physics rows: 4 x 30 consecutive normal runs at different times of day and seeds, two-run timing, Leak/Isolation, every peer group, Untrusted flow and Untrusted corroboration with the simulator's `dq_flatline`), plus handler tests (summary findings, pass 2 sees the annotated dq, isolation, 7 malformed-finding cases, tunables at cold start). The handler tests' synthetic `live_store` now gives every pipe the same background methane (contract mid-ranges differ: P-022's methane max is 20000, which would read as a leak).
- Calibration scripts lived in `.work/` (gitignored).
- **Review fixes (2026-09-29):**
  - Pass 2 is skipped (ERROR line, `pass2_skipped: true`) when any pass-1 detector fails, since its sensors would wrongly score 1.0.
  - `leak_mass_balance` uses the ratio of sums `sum(in - out) / sum(in)`, and a new tunable `min_flow_frac: 0.01` of the `flow_in` contract span suppresses barely flowing or negative-flow pipes. The lowest simulated baseline is 2.9% of span (flare headers), which leaves a 3x margin. Calibrated values are unchanged, and all calibration tests still pass.
  - Both checks give no finding when the compared units differ.
  - An untrusted `pressure_out` is named even without `pressure_in`. Sparse methane peers are named ("had too little data").
  - `check_pass2_set` enforces `detector_id == det.id` and one finding per (detector, pipe). `check_passes` at cold start requires pass-1 ids to be `dq_*` and pass-2 ids not to be, on top of cross-pass id uniqueness.
  - `check_avg_tunables` validates `window_len_s` and requires `window_len_s - avg_s >= min_points`.
  - Summary findings carry `explanation` and `evidence_aliases`, with confidence rounded to 3 decimals. A new `suppressed: [{detector, pipe, reason}]` list comes from each detector's `requires`.

- Live check (2026-09-29 02:57–03:01 +08, `poc-dj`, deploy run 36467288572, pre-review code `15487f6`):
  - Default `leak` injected on P-007 at 18:57:25Z.
  - The runs at 18:56:48–18:58:48 had no findings.
  - The 18:59:48 run (window end 18:58:00, 35 s of leak) gave `leak_mass_balance` P-007 0.69 and `leak_methane_rise` P-007 0.58, and no other pipe.
  - Consistent with the calibration (1.0 at 60 s of leak).
  - Injection cancelled; switched off with the reviewed code (`a96fddf`).

## Plan Change Log

- 2026-09-29 (review): `leak_mass_balance` uses the ratio of sums, not the mean of per-second ratios, and gains `min_flow_frac`. The summary gains `explanation`, `evidence_aliases`, `suppressed` and `pass2_skipped`. Pass 2 is skipped when pass 1 fails.
- 2026-09-29: `leak_*` tunables gain `min_points` (both) and `leak_mass_balance.pressure_drop_frac` (the corroboration threshold); the frozen confidence formulas are unchanged. The `pressure_out` corroboration is measured relative to `pressure_in` (evidence in Implementation Notes).

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 1, medium 6, low 14, false 0. Outcome: 10 patch groups, no deferrals, no loopback. The high finding is resolved by an autonomous decision (pass 2 skipped when pass 1 fails).

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| BH-1, ECH-7 | A pass-1 failure leaves sensors at 1.0, trusted by pass 2 (false leak) | high | patch + decision | Skip pass 2 with an ERROR and `pass2_skipped`. |
| BH-2, ECH-1 | Near-zero `flow_in` makes the ratio blow up | medium | patch | Ratio of sums, a `min_flow_frac` guard, non-finite values ignored. |
| BH-8, IA-2, ECH-12 | Summary lacks explanation and evidence; suppressed checks invisible | medium | patch | Explanation, evidence, rounded confidence and a `suppressed` list. |
| IA-1 | No handler-level positive leak test | medium | patch | Test added. |
| VG-1 | Pipe-kind malformed pass-2 finding untested | medium | patch | Parametrized test. |
| VG-2 | Pressure baseline `min_points` guard untested | medium | patch | Test added. |
| BH-4, ECH-10 | `detector_id` mismatch, duplicate findings, id collisions | low | patch | Validated. |
| BH-3, ECH-2, ECH-8 | Unit mismatch not checked | low | patch | Skip when units differ. |
| BH-5, ECH-3, BH-7, ECH-9 | Untrusted or dataless sensors not named | low | patch | Named in the explanation. |
| BH-6, ECH-5 | Tunables validation gaps | low | patch | Checks added. |
| BH-9 | Docs: hard-coded stage; unclear cadence | low | patch | Fixed. |
| ECH-4 | `rate_hz` versus `min_points` | low | reject | Every contract is 1 Hz. |
| ECH-6 | `dq_min` = 0 makes exclusion a no-op | low | reject | Model range [0, 1]; 0 is a legitimate "trust all". |
| ECH-11 | Methane check is facility-kind | low | reject | The plan permits either kind. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
