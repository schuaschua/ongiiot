---
title: 'Story 3.2: Flag a pipe that behaves differently from its peers'
type: 'feature'
ticket: 'OG-15'
created: '2026-09-29'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: '75dc22c139ddfb2e80e4c2eaf9bec4a358f12fba'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Some problems cross no single threshold but make a pipe behave unlike its peers (same service, material and age). Integrity engineers need such a pipe flagged, with the comparison and peers named (Story 3.2, OG-15; FR5; AD-5, AD-14).

**Approach:** A pass-2 facility detector, `outlier_peer`. For each pipe it computes dimensionless behaviour features from trusted sensors over a trailing span, then scores each feature against the pipe's peer group with a robust z-score (median and MAD). A pipe whose worst feature stays beyond a tunable threshold gets one finding, naming the pipe, the features and sensors compared, and the peers.

## Boundaries & Constraints

**Always:**
- AD-5 pass 2 and AD-14:
  - compare only within `peer_group`, which needs at least 4 pipes with trusted data, the pipe itself included;
  - sensors below `dq_min` are excluded and named;
  - one finding per pipe, and its `evidence_aliases` are the raw aliases behind the flagged features.
- Features (autonomous decision, logged), each a trailing mean over `avg_s` of trusted samples:
  - `pressure_ratio = pressure_out / pressure_in`
  - `flow_ratio = flow_out / flow_in`
  - `temperature`
  - `methane_ppm`
- Score: `z = (x − median(peers)) / (1.4826 × MAD(peers) + floor_f)`, where `floor_f` is a per-feature noise floor tunable that stops a near-zero MAD from exploding.
  - Confidence is `0` below `min_z`, otherwise `min(1, (|z| − min_z) / (full_z − min_z))`, taking the worst feature.
- Tunables are calibrated on simulator physics:
  - no finding across healthy 30-minute runs (several seeds and times of day);
  - a default leak on P-007 makes P-007 an outlier (pressure ratio or methane) within two runs of the leak entering the window;
  - no other pipe in the group is flagged by the leak.
- The finding goes into the run summary like the Story 3.1 findings. There is still no incident writing.

**Never:**
- No incidents, alert-state writes, email (3.3, 3.4), Grafana changes, or changes to other detectors, the reader, the writer or IAM.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Healthy | 30 min of normal simulator output | no `outlier_peer` findings | none |
| Leak outlier | default leak on P-007 | `outlier_peer` on P-007; the explanation names the feature, its z, and the peer pipe ids | none |
| Peers only | the leak on P-007 | no `outlier_peer` on P-007's peers | none |
| Small group | fewer than 4 trusted pipes in a group | no finding for that group | none |
| Untrusted sensor | P-007 `pressure_out` untrusted | `pressure_ratio` skipped for P-007, and said so; other features still scored | none |
| Shared shift | a whole group shifts together | no finding | none |
| Live | `poc-dj`: `ops.inject leak --pipe P-007 --minutes 10` | an `outlier_peer` P-007 finding in the summary alongside the leak findings; no other outlier | recorded; injection cancelled; simulator off |

</frozen-after-approval>

## Code Map

- `detect/core/detectors/_peers.py` (median, peer grouping), `_pass2.py` (trusted-sensor helpers, `requires`, avg tunables), `leak_methane_rise.py` (a facility detector with per-pipe findings and named untrusted peers). Reuse them.
- `detect/core/detectors/__init__.py` -- `PASS2` registry.
- `detect/handler.py` -- pass 2 isolation, `check_pass2_set`, the summary `findings` / `suppressed` fields (from Story 3.1).
- `facility/facility.yaml` -- `detectors.outlier_peer: {}`; peer groups of 6 pipes.
- `sim/physics.py` -- leak effects on the pressure ratio and methane; noise levels for calibration.
- `tests/test_leak_detectors.py` -- calibration and test style to mirror.

## Tasks & Acceptance

**Execution:**
- [x] `detect/core/detectors/outlier_peer.py`; register in `PASS2`. -- the detector.
- [x] `facility/facility.yaml` -- calibrated tunables, with the evidence in Implementation Notes. -- config.
- [x] Tests (`tests/test_outlier_peer.py` and a handler test): every matrix row except Live. -- matrix.
- [x] `docs/ci-cd.md` §7 -- what the outlier check means and how to read it. -- operability.
- [x] Live (the orchestrator runs this). -- acceptance.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given the live check, then the summary shows an `outlier_peer` P-007 finding naming its peers, and no other outlier.

## Implementation Notes

- **Detector** (`detect/core/detectors/outlier_peer.py`, facility kind, registered third in `PASS2`): the four frozen features over the trailing `avg_s`. Autonomous decisions, logged:
  - Ratios are a ratio of means over the seconds where both sensors have a GOOD value (`sum(out) / sum(in)`), not a mean of per-second ratios, so one near-zero inlet reading cannot blow it up; a non-positive inlet sum or mismatched in/out units gives no value.
  - Peers are the *other* pipes of the group (leave-one-out median and MAD), so the pipe under test never dilutes its own reference; a feature is scored only when at least 4 pipes (the pipe included) have a value and share the sensor's unit.
  - Confidence and evidence exactly as frozen; findings list flagged features worst first, name the peers of the worst feature and any left-out peer with its reason (untrusted, too little data, different unit); evidence aliases are in the pipe's sensor order.
  - An untrusted own sensor skips only the features that need it; the explanation says so when there is a finding. `requires = ()` (no single sensor silences the check); a detector method (see review fixes) lets `handler._suppressed` report `"<feature> skipped: <sensor> untrusted (...)"` in the summary's `suppressed` even without a finding.
  - No `dataclasses` import: the detectors' import rule (`test_dq_detectors`) allows only `math`, `collections`, `typing`.
- **Calibrated tunables:** `outlier_peer: {avg_s: 60, min_points: 30, min_z: 4, full_z: 8, floor_pressure_ratio: 0.003, floor_flow_ratio: 0.005, floor_temperature: 2.0, floor_methane_ppm: 1.0}`.
  - Healthy physics, 6 x 30 consecutive one-minute runs (seeds 0, 1, 2, 3, 5; different times of day), all 24 pipes, max |z| per feature: `pressure_ratio` 2.31, `temperature` 1.45, `methane_ppm` 0.46, `flow_ratio` 0.30 (min_z 4). Static steady-state spread over 300 seeds (no noise): `pressure_ratio` max 2.56, `temperature` (with the daily cycle) max 1.86.
  - Floors traded against healthy max |z|: `pressure_ratio` 0.001 / 0.002 / 0.003 / 0.005 -> 5.97 / 3.34 / 2.31 / 1.45; `temperature` 0.5 / 1 / 2 / 3 K -> 3.75 / 2.43 / 1.45 / 1.03.
  - Default leak (10% of mean flow) on P-007 (and on P-003, P-010, P-016, P-020), second run after entering the window (entered 1, 17, 30, 45, 60 s): `methane_ppm` z >= 337, `flow_ratio` |z| >= 17.7, so confidence 1.0; first-run confidence by seconds of leak: 1 s -> none, 5 s -> 0.70, >= 10 s -> 1.0. Peers' max |z| during the leak 1.91 (`pressure_ratio`). The leak's pressure-ratio shift (~0.01) is inside normal flare-header peer spread (up to 0.0087), so pressure_ratio alone cannot separate a leak; methane and flow ratio carry it (plan allows "pressure ratio or methane").
- **Tests:** `tests/test_outlier_peer.py` (40 tests: every matrix row but Live, synthetic and physics, confidence ramp, peers left out and named, small group / 4-pipe minimum, shared shift, untrusted own and peer sensors, non-finite values, zero flow, unit mismatch, 6 healthy 30-run sequences, two-run timing, all peer groups, untrusted `pressure_out` via simulator `dq_flatline`). Handler tests: summary carries the outlier finding (synthetic and physics leak), feature-skip `suppressed`, tunables at cold start (missing and invalid keys, bundled values).
- **Existing tests touched:** the handler tests' synthetic `live_store` now gives every pipe the same temperature (P-004 and P-016 have their own temperature contract, so a mid-range value made them peer outliers); `tests/test_leak_detectors.py` runs only the `leak_*` detectors in its `pass2` helper, and its registry and import tests list `outlier_peer`.
- **Review fixes (2026-09-29):**
  - Ratios: new tunable `min_den_frac: 0.01` (of the inlet contract span, above its min): an idling or shut-in inlet gives no ratio (`<den> too low`); a negative outlet mean gives none (`<num> negative`). Healthy runs keep every feature scored (lowest baselines: flare flow 1.5 kg/s vs 0.5, flare pressure_in 1.5e5 Pa vs 1e5). Calibrated values unchanged.
  - Units: ratios are not unit-compared across peers (dimensionless once the pipe's own in/out units match, else `unit mismatch`); temperature/methane need an equal, present contract unit. Left-out peers say `unit mismatch`.
  - Explanation: per flagged feature its sensors (`flow_ratio (flow_out/flow_in)`), value, median, peers compared and left out; every untrusted sensor of a skipped feature is named.
  - Flag only when `|z| > min_z` (no zero-confidence finding). `check_tunables` requires finite values.
  - `handler._suppressed` uses `_pass2.trusted` / `dq_of` / `dq_min` and a new `_pass2.untrusted_reason`, and adds the detector's optional `skipped(fw, t)` (replaces `feature_sensors`): untrusted, too little data, unit mismatch, too low / negative, `only N pipes with data`. It now runs inside the detector's isolation.
  - Pipes with an empty or missing `peer_group` are skipped. Docs: masking starts at 4 of 6 (tests for 3 and 4 of 6), contract units, and the incident note for Story 3.3.
- Calibration scripts in `.work/calib_outlier*.py` (gitignored). `cdk synth -c stage=poc-dj --quiet` exit 0 (run without AWS credentials).

- Live check (2026-09-29 03:46–03:50 +08, `poc-dj`, deploy run 36473017107, pre-review code `c33b1d3`):
  - Default leak on P-007 at 19:46:26Z.
  - Three clean runs before the leak.
  - The 19:48:54 run gave `outlier_peer` P-007 1.0 ("Unlike its 5 peers in fg-cs-10to15 (P-008, P-009, P-019, P-020, P-021) over the last 60 s: methane_ppm 159 vs peer median 2.047 (z +136.4); flow_ratio 0.9491 vs peer median 0.9998 (z -9.9)"), plus `leak_mass_balance` 0.637 and `leak_methane_rise` 0.523.
  - No other pipe flagged.
  - Injection cancelled; switched off with the reviewed code (`f24d86e`).

## Plan Change Log

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 5, low 14, false 0. Outcome: 9 patch groups, 1 deferral (to 3.3), no loopback.

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| BH-2, ECH-1, ECH-2 | Tiny inlet mean or negative numerator makes the ratios explode | medium | patch | `min_den_frac` guard; negative numerator gives None. |
| BH-3, ECH-6, VG-1, VG-o1 | Unit handling wrong for ratios and single features; peer-unit branch untested | medium | patch | Ratios dimensionless; single features compare units; missing unit is not comparable; tests. |
| BH-4, ECH-9 | `_suppressed` re-implements the trust test | medium | patch | Reuse the `_pass2` helpers. |
| BH-5, BH-6, ECH-3, ECH-7, ECH-claim | Explanation incomplete (first untrusted only, worst feature only, no sensor names) | medium | patch | All named per feature. |
| BH-1 | Docs give the wrong masking count (3 vs 4 of 6) | medium | patch | Docs fixed; 3-of-6 and 4-of-6 tests. |
| BH-7, VG-o3 | Zero-confidence finding at `min_z` | low | patch | Strict `>`. |
| BH-8 | Non-finite tunables accepted | low | patch | `isfinite` checks. |
| ECH-4, ECH-5 | Features skipped silently | low | patch | Listed in `suppressed`. |
| ECH-8 | Empty `peer_group` pooled | low | patch | Skipped. |
| BH-9 | Docs claim units the code doesn't use | low | patch (doc) | "Contract units". |
| BH-10 | Outlier and leak findings not cross-referenced | low | defer | Story 3.3 groups all findings on a pipe into one incident (AD-6). |
| IA-R2, IA-R3, IA-R4 | No engineer-facing surface; no multi-run persistence; not all features dimensionless | low | reject | 3.3/3.4 own the surface; the trailing mean is the plan's persistence reading; the plan allowed peer-comparable features. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
