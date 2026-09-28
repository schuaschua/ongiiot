---
title: 'Story 2.3: Flag drifting and silent sensors'
type: 'feature'
ticket: 'OG-12'
created: '2026-09-29'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: '1e5a6ce06469e00fc8fb3611002d0b8e0513ebf4'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Slow calibration drift and dropped data are invisible. A drifting sensor keeps scoring 1.0, and a sensor that goes silent for a few minutes still has earlier samples in the 15-minute window, so the whole-window gap rule never fires (Story 2.3, OG-12; FR3; AD-5, AD-13).

**Approach:** Two more pass-1 detectors.
- `dq_drift` is a facility detector. It compares each sensor with the median of the same measurement on its peer pipes (AD-14 `peer_group`), and flags a residual that keeps moving in one direction.
- `dq_gap` is a pipe detector. It flags a trailing silence, meaning no GOOD samples for the last part of the window.

Both are calibrated on real simulator output, so normal data (including normal ingest lag) never raises them.

## Boundaries & Constraints

**Always:**
- AD-5, the detector protocol from 2.2, one alias per finding, a one-sentence explanation. Tunables go under `tunables.detectors.<id>` and are validated at cold start.
- **`dq_drift` (autonomous decision, logged):**
  - For each sensor, the residual is its value minus the median of the same measurement on the *other* pipes in its peer group, at the same timestamps.
  - Fit a least-squares slope to the residual over the trailing `fit_s` seconds.
  - Normalize by the contract span (max − min) to get a rate in fraction of span per minute.
  - Confidence is `0` below `min_rate`, otherwise `min(1, rate / full_rate)`.
  - Needs at least 2 peers with data and at least `min_points` samples; otherwise no finding.
- **`dq_gap` (autonomous decision, logged):**
  - `silence_s = window_end − last GOOD sample` (the whole window when there is none).
  - Confidence is `0` below `min_gap_s`, otherwise `min(1, silence_s / full_gap_s)`.
  - A fully empty window scores 1.0 `dq_gap` exactly once. It must not duplicate the AD-13 no-sample rule in `dq_score`; keep one source of that finding.
- **Calibration targets:**
  - Across normal 15-minute simulator windows (several times of day, all 144 sensors): no finding from either detector.
  - The `ops/inject.py` example drift (`temperature`, `per_min` 0.5) crosses `dq_min` within 5 minutes of drift in the window.
  - A 3-minute `dq_gap` injection crosses `dq_min`.
  - Normal ingest lag (the simulator's 5 s flush plus SiteWise latency, windows ending 60 s behind) never raises `dq_gap`.

  The chosen values and the evidence go in Implementation Notes.

**Never:**
- No leak or outlier detectors (Epic 3), no Grafana changes (2.4), no changes to the simulator, reader, writer or IAM.
- No change to how `dq_score` computes `dq` from findings.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Drift | `dq_drift` `per_min` 0.5 on P-010 `temperature` for ≥ 5 min in the window | P-010 `temperature_dq < dq_min`, `_dq_fail` `dq_drift`; peers unaffected | none |
| Shared change | a whole peer group moves together (process change) | no `dq_drift` | none |
| Too few peers | fewer than 2 peers with data | no `dq_drift` for that sensor | none |
| Silence | `dq_gap` on P-020 `flow_out` for 3 min at the window end | `_dq < dq_min`, `_dq_fail` `dq_gap` | none |
| Old gap | a 3-minute gap early in the window, with data since | no `dq_gap` | none |
| Empty window | no samples at all | exactly one `dq_gap` finding → 0.0 | none |
| Ingest lag | normal data, window ending 60 s behind | no `dq_gap` | none |
| Normal | real simulator windows, no injections | no findings from either detector | none |
| Live | `poc-dj`: the Drift and Silence rows via `ops/inject.py` | both flagged; the other sensors stay 1.0 | recorded; injections cancelled; simulator switched off |

</frozen-after-approval>

## Code Map

- `detect/core/detectors/` -- `_trailing.py` (trailing-run helpers, gap splitting), `dq_flatline.py`, `dq_stuck.py`, `__init__.py` (`PASS1`). Mirror their structure and tests.
- `detect/core/models.py` -- `PipeDetector`/`FacilityDetector` protocols; `FacilityWindow` carries `pipes` (`PipeMeta`, including the peer group if present; if `PipeMeta` lacks `peer_group`, add it from the facility model in `handler.runtime`, since the adapters don't import `facility`).
- `detect/core/dq_score.py` -- `gap_findings()` implements the AD-13 empty-window rule; reconcile it with `dq_gap` so a single finding results.
- `detect/handler.py` -- `_run_detector` dispatches facility detectors once per run and checks that findings name valid pipes and aliases.
- `sim/physics.py` -- `dq_drift` adds `per_min × minutes`; `dq_gap` drops samples. Use it in tests for calibration.
- `facility/facility.yaml` -- `detectors.dq_drift`, `detectors.dq_gap` (currently `{}`); peer groups of 3–4 pipes.
- `tests/test_dq_detectors.py`, `tests/test_detect_handler.py` -- patterns to extend.

## Tasks & Acceptance

**Execution:**
- [x] `detect/core/detectors/dq_drift.py`, `dq_gap.py`; register in `PASS1`; add `peer_group` to `PipeMeta` if missing. -- the checks.
- [x] `facility/facility.yaml` -- calibrated tunables. -- config.
- [x] `detect/core/dq_score.py` -- single source for the empty-window `dq_gap` (no duplicate ids in `_dq_fail`). -- consistency.
- [x] `tests/test_dq_detectors.py` -- every matrix row except Live, plus calibration tests on simulator physics (no false positives; the drift and gap targets). -- matrix.
- [x] `tests/test_detect_handler.py` -- a run-level test with one drifting and one silent sensor; all others stay at 1.0. -- end to end.
- [x] `docs/ci-cd.md` §7 -- both checks, their timing, and how to try them. -- operability.
- [x] Live (the orchestrator runs this). -- acceptance.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass, including the calibration tests.
- Given the live check, then P-010 `temperature` shows `dq_drift` and P-020 `flow_out` shows `dq_gap` below `dq_min`, and the others stay at 1.0.

## Implementation Notes

- **`PipeMeta.peer_group`** already existed (filled by `handler.pipe_metas` from the facility model), so no model change. Peer groups are 6 pipes each (4 groups).
- **`dq_drift`** (facility, `detect/core/detectors/dq_drift.py`): GOOD samples in `[end - fit_s, end)` are keyed by whole second; for each second where the sensor and at least 2 other peer pipes have a GOOD value, residual = value - median(peers). Least-squares slope and R² over those points; `rate = |slope| x 60 / (contract max - min)`. No finding with fewer than 2 peers with data in the fit span, fewer than `min_points` residual points, or a non-positive span. Explanation: "temperature has drifted up from the median of its 5 peer pipes by 0.5 K/min over the last 300 s."
- **Autonomous decision: an R² gate (`min_r2`).** Span-normalised rate alone cannot separate the target drift from normal process wander. Measured on real simulator physics (all 144 sensors, 300 s fits slid every 60 s over 2 x 24 h and 4 seeds x 24 h, about 160,000 fits): normal residual rates reach 8.5e-3 span/min on flows and 3.1e-3 on pressures (per-pipe AR(1) process disturbance, tau ~50 s), versus 1.43e-3 for the 0.5 K/min temperature target. They never fit a line well: max R² among fits with rate >= min_rate was 0.889 and 0.903 (24 h runs); counts at R² >= 0.8 / 0.85 / 0.9 / 0.93 / 0.95 / 0.97 over the 4 seeded days were 542 / 99 / 9 / 1 / 0 / 0. The target drift fits with R² ~0.99. So confidence is 0 when `rate < min_rate` **or** `R² < min_r2`; otherwise `min(1, rate / full_rate)` exactly as frozen. This is the "keeps moving in one direction" test in the Intent. It also rejects steps: a step at fraction u of the fit has R² <= 3u(1-u) <= 0.75, so a leak's `flow_out` / `pressure_out` / `methane_ppm` step never raises `dq_drift` (tested), which protects Epic 3's leak evidence from being excluded.
- **Calibrated `dq_drift`:** `fit_s: 300, min_points: 150, min_rate: 5.0e-4, full_rate: 1.0e-3, min_r2: 0.97`.
  - Normal temperature / methane residual rates peak at 6.9e-5 / 1.7e-5 span/min (24 h), 7x and 29x below `min_rate`.
  - 0.5 K/min on temperature = 1.43e-3 span/min on the default 350 K span (3.1e-3 on the 160 K override of P-004 / P-016) -> confidence 1.0. First flagged with 4 min (P-010) or 5 min (P-003, P-004) of drift in the window; `fit_s` 300 is what makes "within 5 minutes" hold (a longer fit dilutes the slope and the line).
  - Known limit: with `min_r2` 0.97, temperature drift below about 0.3 K/min is not flagged (0.25 K/min fits with R² 0.965 because of sensor noise). `min_r2` 0.95 would catch it at a higher false-positive risk (1 normal fit >= 0.93 in 4 simulated days); kept at 0.97 because every `dq_*` finding joins an incident and emails (AD-6).
  - Clears 1 to 2 minutes after the injection ends (the simulator's offset disappears, a step).
- **`dq_gap`** (pipe, `dq_gap.py`): `silence_s = end - last GOOD sample at or before end` (capped at the window; the whole window when none). **Calibrated `min_gap_s: 30, full_gap_s: 60`** (same timing as `dq_flatline` / `dq_stuck`). Normal 1 Hz data: silence 1 s in every simulated window. Ingest lag (simulator 5 s flush + SiteWise latency L, run 17 s past the minute, window end 60 s behind): no finding for L = 0, 20, 55 s; data about 85 s late would start to score and L = 120 s flags every sensor (tested). A gap ending at the window end crosses `dq_min` once silence > 30 s (30 s gives exactly 0.5); a 3-minute gap reaches 1.0. An old gap with data since gives nothing. All-BAD / UNCERTAIN samples are silence.
- **One empty-window finding:** `dq_score.with_gap_rule(fw, findings)` adds the AD-13 rule's `dq_gap` only for an empty alias that no `dq_gap` finding names yet; `score_facility` uses it. With the detector running there is exactly one `dq_gap` finding per empty alias (the detector's); if the detector fails, the rule still gives 0.0 / `dq_gap` (tested end to end). `score()` is unchanged.
- Calibration scripts lived in `.work/` (gitignored); the committed evidence is the calibration tests in `tests/test_dq_detectors.py` (5 normal facility windows: 4 times of day + a second seed, all 144 sensors, both detectors silent; drift on 5 pipes across 4 peer groups; shared change; leak steps; ingest lag).

- Live check (2026-09-29 01:45–01:53 +08, `poc-dj`, deploy run 36459116772):
  - `dq_drift` (`per_min` 0.5) was injected on P-010 `temperature` and `dq_gap` on P-020 `flow_out` at 17:45:25Z.
  - At the 17:50:00Z window end (4.5 min of drift): P-010 `temperature_dq` = 0.0 / `dq_drift`; P-020 `flow_out_dq` = 0.0 / `dq_gap`; the other 142 sensors = 1.0.
  - Injections cancelled; simulator and detection switched off with the reviewed code (`f808300`).

## Plan Change Log

- 2026-09-29: `dq_drift` gains a fifth tunable, `min_r2` (R² of the residual line), as an extra condition for a non-zero confidence. The frozen confidence formula is unchanged above both thresholds. Reason and evidence in Implementation Notes.

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 4, low 15, false 0. Outcome: 6 patch groups, 1 deferral, no loopback. The Verification Gap lens found no gaps.

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| ECH-1 | Overflow gives NaN slope/r2, so a full-confidence false drift | medium | patch | Finite guard. |
| ECH-2..4, BH-8b | Degenerate tunables accepted (`min_points > fit_s`, `min_rate = 0`, `min_gap_s = 0`) | medium | patch | Cold-start checks. |
| BH-3 | Window-end convention differs between the two detectors | low | patch | Half-open `[start, end)` in both. |
| BH-4 | `dq_gap` assumes sorted input | medium | patch | Max GOOD timestamp. |
| BH-10, ECH-5 | Explanation overstates the peer count | low | patch | Minimum peers used. |
| BH-7, BH-8a, BH-2, BH-6 | All-BAD fallback; stale docstring; whole-second matching; faulty peers in the median | low | patch (doc) | Known limits documented. |
| IA-R3 | Drift that stops clears while the offset remains | medium | defer | An offset-from-peers check is future work; documented. |
| BH-1, BH-5 | Slow-rate sensors (`rate_hz` < 0.5 or < 1/30) | low | reject | Every contract is 1 Hz. |
| BH-11 | Timing claims unmeasured | low | reject | Measured by the live check (see Implementation Notes). |
| BH-12 | No cost guard | low | reject | Six-pipe groups; negligible. |
| IA-C2 | Calibration at the physics level, not the pipeline level | low | reject | The live check exercises the deployed pipeline. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
