---
title: 'Story 2.4: See trust scores on the facility overview'
type: 'feature'
ticket: 'OG-13'
created: '2026-09-29'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 0
baseline_revision: 'e3b3fed530ef9021761b1ab7c5784f11f2c57eec'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Trust scores are written every minute, but no one can see them. The OT team needs the facility overview to show every sensor's trust score and highlight the failing checks, so bad sensors stand out at a glance (Story 2.4, OG-13; UX-DR1; FR3).

**Approach:** Extend the `facility-overview` template. Each pipe tile gets a trust view: its six `<m>_dq` values, coloured by `tunables.dq_min`, and the non-empty `<m>_dq_fail` texts. Clicking still opens that stage's `pipe-detail` for the pipe. It is provisioned by the existing Grafana custom resource.

## Boundaries & Constraints

**Always:**
- Only the dashboard template and its expansion change. The existing provisioning, data source and uids stay.
- Per pipe (autonomous decision, logged):
  - keep the existing raw-value tile;
  - add a "Trust" stat panel with the six `<m>_dq` values: red below `dq_min`, green at or above it, and the threshold value filled in from `tunables.dq_min` at provision time, not hard-coded;
  - add a "Failing checks" panel showing each `<m>_dq_fail` that is non-empty (empty strings hidden, or shown as "none" if the panel type can't hide them);
  - both panels link to `pipe-detail<suffix>` with `var-pipe_id` and `var-area`, like the existing tile.
- Queries use the SiteWise data source by alias `${alias_root}/<area>/<pipe_id>/derived/<m>_dq` and `.../derived/<m>_dq_fail` (`PropertyValue`, latest).
- Layout stays readable for 24 pipes: rows per area, tiles grouped per pipe.

**Never:**
- No change to detection, the writer, other dashboards (except shared helpers), the Grafana workspace or IAM.
- No alert-state panels (Epic 3).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Healthy | all `_dq = 1.0`, `_dq_fail` "" | trust panels green; no failing-check text | none |
| Faulty | P-003 `flow_in_dq = 0.0`, `_dq_fail = dq_flatline` | P-003 `flow_in` trust value red; "dq_flatline" shown for P-003 | none |
| Threshold | `dq_min` changed in `facility.yaml` | provisioned threshold equals the new value | none |
| Click | click a pipe's trust or failing panel | opens `pipe-detail<suffix>?var-pipe_id=P-xxx&var-area=<area>` | none |
| Stages | `poc` and `poc-dj` | each overview queries only its own `alias_root` | none |
| Live | `poc-dj` with a `dq_flatline` injection | every derived query on the provisioned overview returns data through the Grafana API; the faulty sensor's value is below the provisioned threshold | recorded; injection cancelled; simulator off |

</frozen-after-approval>

## Code Map

- `dashboards/facility-overview.json` -- template: a text panel, an `__AREA__` row prototype, and a `__PIPE_ID__` stat prototype with `PropertyValue` targets and links.
- `infra/lambdas/grafana_provision/handler.py` -- `expand_pipe_panels` (180) clones every panel that mentions `__PIPE_ID__` per pipe, `render_dashboards` (222) fills placeholders. Add a `__DQ_MIN__` placeholder filled from the stage's facility tunables (passed in the custom resource properties, like `alias_root`).
- `infra/stacks/grafana_provision.py` -- the custom resource properties; add `DqMin` from `facility.tunables["dq_min"]`.
- `tests/test_dashboards.py`, `tests/test_grafana_provision.py` -- extend.
- Live verification pattern: a temporary Grafana service-account token, then `/api/dashboards/uid/...` and `/api/ds/query` with a panel's own targets (see the Story 1.5 plan's Implementation Notes).

## Tasks & Acceptance

**Execution:**
- [x] `dashboards/facility-overview.json` -- add the Trust and Failing-checks prototypes per pipe. -- UX-DR1.
- [x] `handler.py`, `grafana_provision.py` -- the `__DQ_MIN__` placeholder and the `DqMin` property; expansion and layout for the extra panels. -- wiring.
- [x] Tests: the template has both new prototypes with the right aliases, thresholds, links and placeholders; the rendered overview has 24 × 3 pipe panels; `DqMin` flows from `facility.yaml` into the threshold; stage suffix and `alias_root` are correct. -- matrix.
- [x] `docs/ci-cd.md` §6/§7 -- how to read trust on the overview. -- operability.
- [x] Live (the orchestrator runs this). -- acceptance.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass.
- Given the live check, then the provisioned `facility-overview-dj` contains the trust panels, and every derived query on it returns data, with the injected sensor below the threshold.

## Implementation Notes

- **Panels:** per pipe, the raw tile `P-xxx (<area>)`, a stat `P-xxx trust` (six `derived/<m>_dq`, `PropertyValue`, thresholds red / green at `__DQ_MIN__`, background colour, 0 to 1) and a stat `P-xxx failing checks` (six `derived/<m>_dq_fail`). Stat panels hide string fields by default, so the failing panel selects them with the field regex `/_dq_fail$/` (the SiteWise plugin names the value field after the property) and has no displayName override (it would rename the field and break the regex). Grafana cannot hide an empty string per field in a stat, so a `special`/`empty` value mapping shows a green `none`; non-empty values are red.
- **`__DQ_MIN__`:** `fill` substitutes a string that is exactly a placeholder with a non-string value as that value (the threshold stays a number); inside longer text it becomes `str(value)`. `DqMin` is `repr(float(tunables.dq_min))`.
- **Layout:** the pipe prototypes form one block from their template gridPos (8 wide, raw 6 + trust 5 + failing 4 high); blocks fill each area left to right, `24 // 8 = 3` per grid row. 24 pipes -> 72 pipe panels, 2 rows, 432 queries per 30 s refresh.
- **Live-check risk:** the field regex and the empty-string mapping are untested against real plugin frames; check the failing panel renders in the browser.

- Live check (2026-09-29 02:14–02:20 +08, `poc-dj`, deploy run 36462852852):
  - `dq_flatline` injected on P-003 `flow_in`.
  - The provisioned `facility-overview-dj` has 75 panels, including the P-003 raw, trust and failing tiles.
  - The panel queries, run through the Grafana API with `alias_root` substituted, return fields `<m>_dq` / `<m>_dq_fail`: `flow_in_dq` = 0 (below the provisioned threshold 0.5, so red) with `flow_in_dq_fail` = `dq_flatline`; the other five are 1 and ''.
  - Visual rendering not checked (it needs a browser).

## Plan Change Log

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Counts: high 0, medium 3, low 12, false 3. Outcome: 4 patch groups, 2 deferrals, no loopback.

| Ids | Finding | Verdict | Route | Evidence / action |
|-----|---------|---------|-------|-------------------|
| BH-2, ECH-4, VG-o1 | `/_dq_fail$/` may match no field | false | reject | Verified live 2026-09-29 02:20: the plugin returns fields `<m>_dq_fail` and `<m>_dq`. |
| BH-3 | No tests in the diff | false | reject | The tests are in the commit (`tests/test_dashboards.py`, `test_grafana_provision.py`); the lens got a diff without them. |
| BH-4 | `dq_min` not validated | false | reject | `facility/model.py` validates `dq_min` in [0, 1] at load. |
| VG-1 | Green colour of "none" not asserted | medium | patch | Full-result assertion. |
| ECH-1 | Two decimals can show 0.50 below the threshold | low | patch | Three decimals. |
| ECH-3 | Null latest value falls to red | low | patch | `null+nan` mapped to green "none". |
| BH-5 | Measurement list hard-coded in the template | low | patch | Test against `MEASUREMENTS`. |
| BH-7, BH-8, ECH-7 | Query-load caveat; docstring wording | low | patch (doc) | Fixed. |
| BH-1, ECH-2 | Stale scores look current if detection stops | medium | defer + doc | Health alarms (Story 4.4) and a "last evaluated" indicator deferred; documented. |
| BH-6 | Tile readability at 3 per row | medium | defer | Needs a browser look by the user; noted in the report. |
| ECH-5, ECH-6 | Zero-width or over-wide block guards | low | reject | Template-controlled; covered by the layout tests. |
| IA-R3, ECH-claim | Six rows with "none" instead of only the failing ones | low | reject | The plan allows "none" when the panel can't hide empty values. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` -- expected: exit 0.
