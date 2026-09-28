---
title: 'Story 2.1: Refresh sensor trust every minute'
type: 'feature'
ticket: 'OG-10'
created: '2026-09-28'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: '7472add14e304f6bfcbe07284260b03977805cfc'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
  - '{project-root}/docs/ci-cd.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing tells the OT team whether a sensor can be trusted. Epic 2 needs a detection run every minute that reads the live window and writes a trust score per sensor. Later stories add the checks (2.2, 2.3) and leak and outlier detection (Epic 3) (Story 2.1, OG-10; FR3; AD-5, AD-9, AD-12, AD-13, AD-18).

**Approach:** A new `detect/` package, laid out as the spine's structural seed:
- `core` holds the models and `dq_score`;
- `adapters` holds the reader and `derived_writer`;
- `handler.py` runs pass 1 with no `dq_*` detectors yet.

It runs as a container-image Lambda built from `uv.lock`. EventBridge Scheduler invokes it every minute with `scheduled_time`, single-flight. It writes `<m>_dq` and `<m>_dq_fail` for every sensor at `window_end`. A live spike measures ExecuteQuery at full load and picks the reader strategy.

## Boundaries & Constraints

**Always:**
- The spine binds: AD-5 (pure core, types only from `detect/core/models.py`), AD-9 (UTC; `window_end = floor_minute(scheduled_time) − ingest_lag_s`; `window_start = window_end − window_len_s`), AD-12, AD-13, AD-18, and the layer rules (`core` imports only its own models; `sim/`, `detect/` and `ops/` never import each other).
  - AD-12 in detail: reserved concurrency 1, timeout 50 s, async retries 0, max event age 60 s; Scheduler `rate(1 minute)`, flexible window off, retries 0, input `{"scheduled_time": "<aws.scheduler.scheduled-time>"}`; `run_id = scheduled_time`.
- Tunables come only from `facility.yaml` (bundled): `window_len_s`, `ingest_lag_s`, `dq_min`.
- `dq_score.score(findings_for_alias) -> (dq, failing)`: `dq = 1 − max(confidence of dq_* findings)`, or `1.0` if there are none; `failing` is the sorted `dq_*` ids joined by commas.
- Decision (autonomous, logged): an alias with no samples in the window scores `0.0` with `dq_gap` (AD-13), although the story text says "1.0 for every sensor". Story 2.3 then only adds the injection and ingest-lag tests.
- `derived_writer.py` is the only derived writer. It refuses any alias not under `/derived/`, raising before any API call. It writes `<m>_dq` (DOUBLE) and `<m>_dq_fail` (STRING, empty if none) for all 144 sensors at `window_end`, in batches of at most 10 entries.
- Decision (autonomous, logged): `alert_state` and `alert_summary` are not written until Story 3.3 owns them.
- `reader.py` returns a `FacilityWindow` in which every expected raw alias is present (an empty list if there is no data). It has two strategies behind one interface:
  - `execute_query`: SiteWise `ExecuteQuery` on `raw_time_series` by `event_timestamp`;
  - `batch_get`: `BatchGetAssetPropertyValueHistory`, 16 entries per request.

  The strategy is chosen by the env var `READER_STRATEGY`; the default is set from the spike.
- Decision (autonomous, logged): the Lambda is a container image (`detect/Dockerfile`, AWS Lambda Python 3.14 base, `uv sync --locked --no-default-groups`), following the "bundle from `uv.lock`" convention and the simulator's image pattern.
- IAM (AD-17), detection role:
  - SiteWise read: `ExecuteQuery`, `BatchGetAssetPropertyValueHistory`, `Describe*`, `List*`;
  - `BatchPutAssetPropertyValue` on the stage's pipe assets plus `time-series/*` (alias writes are authorised against the time series; see Story 1.4);
  - nothing on `sim_control` or `injection_log`.
- Decision (autonomous, logged): the scheduler follows the existing `simulator` switch. `on` → ENABLED, `off` → DISABLED, so detection costs nothing while testing is off.
- One JSON summary log line per run: `stage`, `run_id`, `config_hash`, `window_start`, `window_end`, reader strategy, read duration, aliases with no data, values written.

**Never:**
- No `dq_*` detectors (2.2, 2.3), incidents, email or deep links (Epic 3), and no Grafana changes.
- No change to the simulator, the SiteWise asset model or the CI trust.
- No writes to raw aliases.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Normal run | stream live, `scheduled_time` 12:34:17Z | window `[12:18:00, 12:33:00)` (ingest lag 60 s, length 900 s); 144 `_dq` = 1.0 and 144 `_dq_fail` = "" at 12:33:00Z | no error |
| No data | simulator off | every `_dq` = 0.0, every `_dq_fail` = `dq_gap` | no error |
| Replay | same `scheduled_time` twice | identical values at the same timestamp | no error |
| Derived guard | writer given a raw alias | refuses before any API call | raises |
| Reader parity | same window through both strategies | the same `FacilityWindow` (series per alias) | adapter failure fails the invocation |
| Layer rules | any import from `detect/core` to adapters, or `detect` ↔ `sim`/`ops` | `tests/test_layers.py` fails | test failure |
| Single flight | CDK template | reserved concurrency 1, timeout 50 s, retries 0, max event age 60, schedule `rate(1 minute)` with retries 0 and flexible window off | none |
| Switch off | `-c simulator=off` | schedule `DISABLED` | none |
| Spike | live `poc-dj`, simulator on at least 16 min | ExecuteQuery duration at full load recorded; strategy default set; result written against AD-18 | if ExecuteQuery is over 50 s or fails, default `batch_get` |

</frozen-after-approval>

## Code Map

- `facility/model.py` -- `parse()`/`load()`, `alias()`, `derived_alias(pipe_id, name)` (132-138), `derived_properties()` (151-160), tunables validation (285-300), `config_hash()` (196-200). The derived properties already exist on every Pipe asset with aliases `<pipe_root>/derived/<name>` (`infra/stacks/sitewise_assets.py:136-138, 176-178`). Do not change.
- `sim/writer.py` -- a batching and guard pattern to mirror, not import (layer rule).
- `infra/stacks/simulator.py` + `sim/Dockerfile` -- image, IAM and on/off-switch patterns (`desired_count` from context `simulator`). Reuse the switch parsing so both stacks agree.
- `infra/app.py` -- add `DetectionStack-<stage>`, depending on `SiteWiseAssetsStack-<stage>` for the pipe asset ARNs.
- `tests/test_simulator_stack.py` -- exact-policy test style.
- `.github/workflows/ci.yml` -- the `image` job builds `sim/Dockerfile`; add the detect image the same way.

## Tasks & Acceptance

**Execution:**
- [x] `detect/core/models.py`, `detect/core/dq_score.py` -- the v1 shapes and scoring exactly as in the spine. -- core.
- [x] `detect/adapters/reader.py` -- both strategies, alias completeness, UTC conversion only here, metadata cached per cold start. -- AD-18.
- [x] `detect/adapters/derived_writer.py` -- derived-only guard; batches; timestamps at `window_end`. -- AD-13.
- [x] `detect/handler.py` -- parse `scheduled_time`, compute the window (AD-9), read, pass 1 (no detectors yet, plus the no-sample gap rule), score, write, and one summary log line. -- the run.
- [x] `detect/Dockerfile` -- Lambda Python 3.14 base, deps from `uv.lock` (no dev or infra groups), copy `detect/` and `facility/`, handler `detect.handler.handler`. -- image.
- [x] `infra/stacks/detection.py` + `infra/app.py` -- `DetectionStack-<stage>`: Lambda (`DockerImageFunction`, linux/amd64, timeout 50 s, reserved concurrency 1, async retries 0, max event age 60), a Scheduler schedule with its own invoke role, the detection role above, a log group with 1-week retention, env `STAGE` and `READER_STRATEGY`, and the schedule state following `simulator`. -- infra.
- [x] `.github/workflows/ci.yml` -- build and smoke-test the detect image (import the handler) next to the sim image. -- CI.
- [x] Tests:
  - `tests/test_detect_core.py`: window maths and replay, `dq_score`, the gap rule;
  - `tests/test_detect_adapters.py`: reader completeness and strategy parity with fakes; writer guard and batching;
  - `tests/test_detect_handler.py`: full run with fakes for the "Normal run" and "No data" rows;
  - `tests/test_layers.py`;
  - `tests/test_detection_stack.py`: exact grants, single-flight settings, the switch;
  - updated exact stack sets.

  -- matrix.
- [x] Spike, live, after the build is green:
  - deploy `poc-dj` with the simulator on;
  - wait at least 16 minutes;
  - invoke with `READER_STRATEGY=execute_query` and with `batch_get`, and record the durations;
  - set the default;
  - write the result against AD-18 in the spine;
  - switch the simulator off.

  -- AD-18 spike.
- [x] `docs/ci-cd.md`, `ARCHITECTURE-SPINE.md` -- the detection section (the switch now also pauses detection); AD-18 and Open Questions updated with the spike result. -- keep docs true.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given `cdk synth -c stage=poc-dj`, then it exits 0 including `DetectionStack-poc-dj`.
- Given a live `poc-dj` with the simulator on for at least 16 minutes, then SiteWise shows `<m>_dq = 1.0` and an empty `_dq_fail` on sampled sensors at the latest `window_end`, and the spike result is recorded.

## Implementation Notes

Built in commit `5721071` on `feat/dq-refresh` (pushed); docs in `b7890c3` (committed, not pushed).

- **Spike (partial):** the live `poc-dj` deploy was refused by the session's permission classifier, so the in-Lambda measurement did not run. A read-only laptop spike against `poc-dj` history (144 aliases, one 15-min window with 70,992 samples) measured `batch_get` 0.8–1.3 s and `execute_query` 14.6–14.9 s (about 27 s extrapolated to a full window), identical windows. Default set to `batch_get`; recorded in AD-18 and Open Questions as partly resolved.
- **Decision (ExecuteQuery filter):** a 144-alias `property_alias IN (...)` returned samples for only a subset of the aliases (4,440 of 67,680 rows), so the `execute_query` strategy filters with `LIKE '<common prefix of the expected aliases>%'` and drops other aliases (derived, other stages) in the adapter. No alias parsing (AD-1).
- **Decision (metadata):** `PipeMeta` (aliases, contracts, attributes) is built by the handler from the bundled `facility.yaml`, once per cold start, not read from SiteWise asset attributes (AD-2 wording). SiteWise metadata is generated from the same file, 2.1 uses only the aliases, and adapters stay free of `facility` imports. Revisit if a later story needs values that can drift in SiteWise.
- **Decision (window maths location):** `window_bounds` / `floor_minute` live in `detect/core/window.py` (pure, imports only `detect.core.models`).
- **Decision (gap rule):** `dq_score.gap_findings` emits a `dq_gap` finding (confidence 1.0) per alias with no samples; `score_facility` merges them with detector findings, so a later real `dq_gap` detector does not duplicate the id.
- **Decision (image):** deps are resolved with `uv sync --locked --no-default-groups --no-install-project` into a throwaway env and copied into `LAMBDA_TASK_ROOT`, which is ahead of the runtime's bundled boto3 on `sys.path` (verified: `/var/task/boto3` 1.43.103, the locked version).
- **Decision (build context):** `.dockerignore` allowlists `detect`; each CDK image asset excludes the other image's code (`infra/stacks/simulator.py` `IMAGE_EXCLUDE = ["detect"]`, detection `["sim"]`), so detection changes never rebuild or restart the simulator. This is the only change to `simulator.py`; the sim image hash changes once because `.dockerignore` changed.
- **Decision (Lambda memory):** 1024 MB (not in the plan): about 130k `Sample` objects per run plus more CPU for parsing.
- **Decision (scheduler role):** trust limited with `aws:SourceAccount`; the schedule depends on the role's policy so the invoke grant exists before the first call.
- **Decision (writer retries):** throttling and transient errors retried up to 4 attempts; anything else raises (adapter failure fails the invocation).

- Live verification (2026-09-29, `poc-dj`, deploy run 36449431078):
  - detection runs every minute; 144 `_dq = 1.0` and empty `_dq_fail` stored at `window_end` (SiteWise accepts the empty string);
  - in-Lambda full-load spike (129,600 samples): `batch_get` 6.4–6.8 s per run, `execute_query` 19.2–22.1 s. `batch_get` is the default.
  - Manual invokes are throttled by the reserved concurrency of 1, as designed, so timings came from the log lines of scheduled runs and config-switched runs.

## Plan Change Log

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 8, low 16, false 2. Outcome: 9 patch groups, 3 deferrals, no loopback. Rows group duplicates across lenses.

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| IA, VG-o1, BH-2 | Default strategy defined three times; the reader's fallback is the rejected `execute_query` | medium | patch | Single constant `batch_get` in `reader.py`. |
| ECH-1, VG-o2, BH-3 | A failed detector's partial findings still feed `dq` | medium | patch | Per-detector collection, extended only on success. |
| ECH-2 | One malformed `dq_*` finding fails the whole run | low | patch | Validated inside the per-detector try. |
| VG-3 | No handler test from findings to written scores | medium | patch | Test added. |
| VG-2 | Writer partial per-entry retry untested | medium | patch | Test added. |
| VG-1 | Image-exclude wiring checked only through constants | medium | patch | Asset-hash synth test. |
| BH-6 | Stacked botocore and writer retries inside the 50 s budget; no total duration | medium | patch | Writer client `max_attempts=1`; `run_s` in the summary. |
| ECH-6, BH-7 | LIKE prefix can end mid-segment and match other stages | low | patch | Trimmed to the last `/`; the `wanted` filter already drops extras. |
| ECH-3 | NaN confidence gives a NaN `dq` | low | patch | Clamped. |
| BH-13 | Misleading `rm uv` in a later layer | low | patch | Removed. |
| ECH-8, BH-8 | BAD-only windows score 1.0 | low | patch (doc) | AD-13 defines the gap as "no samples"; the limitation is documented until 2.2/2.3. |
| BH-12 | `time-series/*` is account-wide | low | patch (doc) | The code guard is the stage boundary (as in 1.4); documented. Describe/List are kept per the AD-17 table. |
| BH-1, ECH-4 | Empty `_dq_fail` string rejected by SiteWise | false | reject | Verified live 2026-09-29 00:28: 144 empty `_dq_fail` values stored. |
| VG-4 | BatchGet `skippedEntries` error path untested | low | defer | The lens suggested defer. |
| BH-4 | No typed Detector protocol | low | defer | Story 2.2 adds the first detectors and the protocol with them. |
| BH-5 | No alarm on failed runs | medium | defer | Alarms are Story 4.4 (OG-21). |
| ECH-5, ECH-7, ECH-9, BH-9..11, BH-13b | `max_attempts < 1`, non-numeric doubles, unaligned `run_id`, tunables validation, discarded annotated window, runtime cache, RIE smoke | low | reject | Unreachable with current callers, or covered: the facility loader validates tunables; the window floors to the minute. |

## Verification

Results at hand-off: `uv run pytest -q` 415 passed; `cdk synth -c stage=poc-dj --quiet` exit 0 with `DetectionStack-poc-dj`; `docker build` of `detect/Dockerfile` OK and the CI smoke import passes; a local dry run of `handler.run` with live SiteWise reads and a fake writer wrote 288 values for both strategies. Not verified: live deploy, the third acceptance criterion (values in SiteWise), the in-Lambda spike, IAM sufficiency of the read grants, and SiteWise accepting an empty `stringValue`.

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
- `docker build --platform linux/amd64 -f detect/Dockerfile -t ongiiot-detect:test .` -- expected: builds.
