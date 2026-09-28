---
title: 'Facility model v1 and SiteWise asset stack'
type: 'feature'
ticket: ''
created: '2026-09-28'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: '7b454b71d967ed72feec1d4fecaeb5f15d95bc9c'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/specs/spec-og-iiot/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing exists yet. The simulator, detection and dashboards all need one agreed model of the facility: pipes, sensors, aliases, contracts and tunables. That model must exist in SiteWise before any data can flow (CAP-2).

**Approach:** Author `facility/facility.yaml` (schema v1). Add `facility/model.py`, the only loader and validator. Add a CDK Python app with a per-stage SiteWise asset stack generated from the model: a Pipe asset model with raw, derived and attribute properties, one asset per pipe, and a Facility root asset. A deploy-time custom resource sets each pipe's attribute values.

## Boundaries & Constraints

**Always:**
- Follow the spine exactly: AD-2, AD-14 (schema, validation, alias derivation, `config_hash`), AD-13 (derived properties v1 types), AD-10, AD-16 (stage naming, `alias_root`), and the Conventions (units, IDs, alias format).
- Aliases are derived by `Facility.alias()` and `Facility.derived_alias()`, never authored.
- Attribute values are addressed by alias `<alias_root>/<area>/<pipe_id>/attr/<name>`.
- Every resource name is suffixed with the stage. Region `ap-southeast-1`.
- `uv` project on Python 3.14, `aws-cdk-lib==2.271.0`, with everything pinned in `uv.lock`.

**Never:**
- No simulator, detection, Grafana or IAM role work (deferred).
- No disassociated data streams setting.
- No second parser of `facility.yaml`.
- No hand-authored aliases.
- No console-created resources.
- No AWS deployment in this build: verification stops at `cdk synth` and the tests. Deploy after the POC account is confirmed (human decision).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Valid model | shipped `facility.yaml` | `load()` returns `Facility`; aliases like `/lng1/train1/P-007/flow_in`; stage `poc-dj` gives `/poc-dj/lng1/...` | No error |
| Duplicate/bad pipe ID | two `P-007`, or `P-7` | load fails naming the ID | `FacilityError` |
| Unknown area | `area: train9` | load fails naming pipe and area | `FacilityError` |
| Incomplete contract | sensor without `owner` after overrides | load fails naming pipe and measurement | `FacilityError` |
| Wrong unit | `flow_in` unit `m3/h` | load fails (fixed unit table) | `FacilityError` |
| Small peer group | group with fewer than 4 pipes | load fails naming the group | `FacilityError` |
| Unknown sensor | `sensors: [vibration]` | load fails | `FacilityError` |
| Tunables edit | change `dq_min` | `config_hash()` changes; reorder keys gives the same hash | No error |

</frozen-after-approval>

## Code Map

- The repo is greenfield: there is no application code yet. Git baseline `4a822d5`, branch `feat/facility-model-sitewise`.
- The spine's Shared Shapes hold the `facility.yaml` skeleton, the derived properties v1 table and the unit conventions. Implement against them verbatim.
- Tooling: `uv` 0.11.8 (install Python 3.14 with `uv python install 3.14`), node 22, Docker. There is no global CDK CLI, so use `npx aws-cdk@2`.

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml`, `uv.lock` -- create the uv project (Python 3.14): runtime `pyyaml`; infra `aws-cdk-lib==2.271.0`, `constructs`; dev `pytest`. -- AD-2 and Conventions: pinned deps.
- [x] `facility/facility.yaml` -- author schema v1. 24 pipes (`P-001`…`P-024`) across `train1` and `train2`, in 4 peer groups of 6 with plausible service, material and age. Default contracts for all 6 measurements, a few `contract_overrides`, and tunables per the skeleton. -- CAP-2 and CAP-5 need peer groups of at least 4.
- [x] `facility/model.py` -- frozen dataclasses (`Facility`, `Pipe`, `Contract`); `load(path, stage="poc")`; the validation list; `alias()`, `derived_alias()`, `attr_alias()`, `derived_properties()` (derived properties v1: name, level, type); `config_hash()` (sha256 of canonical JSON of the tunables); `FacilityError`. -- AD-14 is the single loader.
- [x] `infra/app.py`, `cdk.json` -- CDK app that reads `-c stage=` (default `poc`) and instantiates `SiteWiseAssetsStack-<stage>` in `ap-southeast-1`. -- AD-10 and AD-16.
- [x] `infra/stacks/sitewise_assets.py` -- a `Pipe` model with 6 raw DOUBLE measurements (with units), the derived measurements with their v1 types, and attributes per AD-14. A `Facility` model with a hierarchy to `Pipe`. One `CfnAsset` per pipe, with aliases set on every raw, derived and attribute property through external IDs. A Facility asset linking all pipes. -- CAP-2.
- [x] `infra/stacks/seed_attributes.py`, `infra/lambdas/seed_attributes/handler.py` -- a custom resource (Provider framework) that writes attribute values by alias with `BatchPutAssetPropertyValue`, in batches of 10 or fewer. It re-runs when the facility model hash changes. -- CAP-2: values per pipe.
- [x] `tests/test_model.py` -- a matrix test per row above, plus a test on the shipped yaml. -- validation.
- [x] `tests/test_sitewise_stack.py` -- CDK `Template` assertions: asset count = pipes + 1; every expected raw and derived alias appears; derived types match v1; the stage suffix is in the names. -- CAP-2: 100% mapped.

**Acceptance Criteria:**
- Given the shipped model, when `cdk synth -c stage=poc-dj` runs, then it succeeds and every sensor of every pipe has a raw alias, a `_dq` and `_dq_fail` alias, and 5 contract attributes.
- Given two stages, when both synthesize, then no alias or resource name collides.

## Design Notes

- Derived values are SiteWise *measurements*, so that `BatchPut` can write them; their types follow the v1 table.
- Attributes carry model defaults from `contracts`. The custom resource writes per-pipe values, including overrides.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx aws-cdk@2 synth -c stage=poc-dj --quiet` -- expected: exit 0.

## Review Triage Log

**Pass 1** (lenses: blind-hunter, edge-case-hunter, verification-gap, intent-alignment). Verdicts: high 0, medium 7, low 12, false 7, maybe-false 1. There were no intent_gap or bad_plan entries, so the patch and defer entries are processed.

| # | Lens | Finding | Verdict | Route | Evidence / action |
|---|------|---------|---------|-------|-------------------|
| 1 | blind | `uv.lock` missing from diff; deps unpinned | false | reject | The lock is tracked and pins everything. The reviewed diff excluded it on purpose. |
| 2 | blind, vgap | Validation rules outside the matrix are untested (version, keys, duplicate areas or sensors, min ≥ max, rate_hz, age, tunables, dq_min) | medium | patch | Deleting any of those checks leaves every test passing. Add a parametrized invalid-document test. |
| 3 | blind, vgap, edge | `build_app` is untested; an empty `-c stage=` falls back to shared `poc` | medium | patch | `stage or "poc"` treats `""` as unset, so a developer deploy could overwrite the shared stage. Only `None` should default to `poc`; `""` should be rejected. Add `build_app` tests. |
| 4 | blind, vgap | `on_event` is untested | medium | patch | Nothing tests Create/Update writes, Delete no-op, or a stable physical ID. Add the test. |
| 5 | blind, edge | An error entry with an empty `errors` list is treated as retryable | low | patch | An empty `codes` set counts as a subset of `RETRYABLE`. Fix: `if not codes or ...`. |
| 6 | vgap | Giving up after `MAX_ATTEMPTS` is untested | medium | patch | Dropping that condition would make it return success silently. Add the test. |
| 7 | blind, edge | Retries can outlast the 5-minute timeout | low | reject | Only happens under sustained throttling, and the outcome is still a failed deploy. The fix would add timing guards. |
| 8 | blind, edge | Seed doesn't re-run when pipe assets are replaced | medium | patch | `ModelHash` ignores asset identity, so replaced assets keep the model defaults. Add the asset IDs to the resource properties. |
| 9 | blind | Inline entries make the template large | low | reject | About 85 kB at 24 pipes, well inside limits. The fix would be a redesign. |
| 10 | blind, edge | Area names aren't checked for alias-safe characters | low | patch | Direct fix: validate areas with `^[a-z0-9_-]+$`. |
| 11 | blind | `facility.id` error message doesn't match the regex | low | patch | Direct fix to the message text. |
| 12 | blind, edge | Tunables accept zero, negative, inf and NaN; `quiet_runs_to_close` accepts floats; contract values accept inf | medium | patch | Detection would break later. Require finite positive windows, an int ≥ 1 for `quiet_runs_to_close`, and finite contract numbers. |
| 13 | blind | `attr_alias` accepts contract attributes for sensors the pipe lacks | low | patch | Inconsistent with `derived_alias`. Direct fix: restrict to the pipe's sensors. |
| 14 | blind, edge, vgap | `load()` leaks `OSError`; `version: true` or `1.0` passes | low | patch | Direct fix: wrap `OSError`/`UnicodeDecodeError` in `FacilityError` and require an int version. |
| 15 | blind | Facility is named "LNG Train 1" but has two trains | low | patch | Rename it to something like "LNG Facility 1". |
| 16 | blind | IAM scope for writes by alias is unverified | maybe-false | defer | This is the spine's AD-17 open question; only a real deploy settles it. |
| 17 | vgap | Seed IAM policy isn't pinned by a test | low | patch | Removing the grant leaves every test passing. Add a template assertion. |
| 18 | blind | Plan review metadata is inconsistent | false | reject | The review was in progress when the lens read the plan. |
| 19 | blind | `cdk.json` has no feature flags | low | reject | Legacy defaults work for this stack, and it isn't met in everyday use. |
| 20 | edge | Duplicate YAML keys are silently merged | low | reject | Rare authoring error, and the fix needs a custom loader. |
| 21 | edge | `config_hash` raises `TypeError` on non-JSON tunables | low | reject | YAML dates in tunables are unlikely, and the fix adds a validation pass. |
| 22 | edge | Unused or overridden default contract fields are never validated | low | reject | Bad values fail loudly at synth. Unlikely in practice. |
| 23 | edge | External IDs containing `.` would be rejected at deploy | false | reject | The SiteWise pattern `[a-zA-Z0-9_][a-zA-Z_\-0-9.:]*[a-zA-Z0-9_]+` allows `.` and `:` (API_AssetModelPropertyDefinition). |
| 24 | vgap | Pipes with only some sensors are never tested | low | patch | Filtering exists but no test exercises it. Add a parse-based subset test. |
| 25 | intent | The deployed-account surface isn't exercised | false | reject | The human chose no deploy for this build, and the frozen block says so. |
| 26 | intent | Tunables aren't in SiteWise | false | reject | AD-2 puts tunables in `facility.yaml` bundled into consumers, not in SiteWise. |
| 27 | intent | Derived properties are externally written measurements, not computed | false | reject | AD-13 requires the detection run to write them. |
