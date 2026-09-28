---
title: 'Facility model v1 and SiteWise asset stack'
type: 'feature'
ticket: ''
created: '2026-09-28'
status: 'ready-for-dev'
route: 'full'
route_source: 'auto'
review: ''
review_source: ''
lenses_ran: []
review_loop_iteration: 0
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
- [ ] `pyproject.toml`, `uv.lock` -- create the uv project (Python 3.14): runtime `pyyaml`; infra `aws-cdk-lib==2.271.0`, `constructs`; dev `pytest`. -- AD-2 and Conventions: pinned deps.
- [ ] `facility/facility.yaml` -- author schema v1. 24 pipes (`P-001`…`P-024`) across `train1` and `train2`, in 4 peer groups of 6 with plausible service, material and age. Default contracts for all 6 measurements, a few `contract_overrides`, and tunables per the skeleton. -- CAP-2 and CAP-5 need peer groups of at least 4.
- [ ] `facility/model.py` -- frozen dataclasses (`Facility`, `Pipe`, `Contract`); `load(path, stage="poc")`; the validation list; `alias()`, `derived_alias()`, `attr_alias()`, `derived_properties()` (derived properties v1: name, level, type); `config_hash()` (sha256 of canonical JSON of the tunables); `FacilityError`. -- AD-14 is the single loader.
- [ ] `infra/app.py`, `cdk.json` -- CDK app that reads `-c stage=` (default `poc`) and instantiates `SiteWiseAssetsStack-<stage>` in `ap-southeast-1`. -- AD-10 and AD-16.
- [ ] `infra/stacks/sitewise_assets.py` -- a `Pipe` model with 6 raw DOUBLE measurements (with units), the derived measurements with their v1 types, and attributes per AD-14. A `Facility` model with a hierarchy to `Pipe`. One `CfnAsset` per pipe, with aliases set on every raw, derived and attribute property through external IDs. A Facility asset linking all pipes. -- CAP-2.
- [ ] `infra/stacks/seed_attributes.py`, `infra/lambdas/seed_attributes/handler.py` -- a custom resource (Provider framework) that writes attribute values by alias with `BatchPutAssetPropertyValue`, in batches of 10 or fewer. It re-runs when the facility model hash changes. -- CAP-2: values per pipe.
- [ ] `tests/test_model.py` -- a matrix test per row above, plus a test on the shipped yaml. -- validation.
- [ ] `tests/test_sitewise_stack.py` -- CDK `Template` assertions: asset count = pipes + 1; every expected raw and derived alias appears; derived types match v1; the stage suffix is in the names. -- CAP-2: 100% mapped.

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
