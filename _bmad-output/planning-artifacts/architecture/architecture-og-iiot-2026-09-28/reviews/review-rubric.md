---
reviews: ../ARCHITECTURE-SPINE.md
against: ../../../../specs/spec-og-iiot/SPEC.md (with stack.md, roadmap.md)
lens: good-spine rubric (build substrate, 2–4 consultants, 4-day AWS POC)
date: '2026-09-28'
verdict: 'Not ready to build from. The pipeline shape is right, but it leaves out how the demo is run, how alerts reach the dashboard, and several shared contracts.'
---

# Spine Review: Pipe Integrity Streaming POC

## Verdict

**Not ready as a build substrate yet. It needs one focused revision, not a rewrite.**

The paradigm is right: pipes and filters with a hexagonal detection core. The alias-based ingestion contract (AD-1), the single facility model (AD-2), the raw/derived split (AD-3), the no-ground-truth rule (AD-4) and the pure `Finding` contract (AD-5) are good choices that prevent real divergence. The main gaps are these:

1. The operational envelope is mostly silent. It says nothing about how the demo is run, warmed up or reset, gives no IAM model, and has no workable way for 2–4 people to deploy in parallel.
2. AD-6's grouping key (pipe + detector) breaks CAP-6's own success criterion: one injected leak can produce several alerts and several emails.
3. Several seams that different consultants build on either side of are named but not defined: the `window`/`metadata` types, the `facility.yaml` schema, the DQ score, `alert_state` and the deep-link format.

## Rubric Scorecard

| Criterion | Result | Notes |
| --- | --- | --- |
| Fixes the real divergence points and misses none | **Partial** | It names the right seams but leaves several undefined. See C3, H1, H2, H3 and M2. |
| Every AD Rule is enforceable and prevents its divergence | **Partial** | AD-6 doesn't prevent duplicates for the same event (C2), and its uniqueness isn't guaranteed under concurrency (H7). AD-3 and AD-11 rely on IAM that is never stated (H5). AD-2 contradicts the Config convention (H2). |
| Nothing under Deferred lets two units diverge | **Fail** | "Detector thresholds are local" conflicts with thresholds living in `facility.yaml`. The sim↔detector leak signature is implicitly local (M1). "No more environments" leaves shared-stage collisions (H4). |
| Covers every spec capability | **Partial** | All CAP-1–7 are mapped. CAP-6 display has no data path (C3). The CAP-3 score is undefined (H3). CAP-4 "methane-loss" has no detector (M1). The spec's "SiteWise for anomaly detection" constraint is deferred without being flagged (H6). |
| Every owned dimension decided, deferred or open | **Fail** | Demo run and reset, IAM, the deployment workflow, Grafana/dashboard provisioning and operations/monitoring are silent. There is no Open Questions section (M8). |

## Findings

### Critical

**C1 — How the demo is run, warmed up and reset is entirely silent.**
The whole POC is judged at one live demo (Success signal), and the spine never says:
- **Warm-up:** how long the simulator must run before detectors have enough history. `dq_drift` and `outlier_peer` need a baseline, and AD-9 forbids backfill, so warm-up happens in real time.
- **Latency:** the detection latency budget from injection to email: 1-minute schedule + window length (the 15-minute window is mentioned only under Deferred) + persistence logic + SNS email delay. The presenter needs to know how long to talk after pressing "inject leak".
- **Runbook:** who injects what and when, and the order of CAP-1 to CAP-7 on stage.
- **Reset between rehearsals:** close or purge alerts, clear `sim_control`, clear the injection log. Raw SiteWise history can't be deleted (AD-3), so decide whether rehearsal data stays visible, the demo uses a fresh time range, or pipes are re-created.
- **Fallback:** what happens if something breaks live, such as a recorded run or a pre-staged alert.

Different consultants will make incompatible assumptions here, for example a detector needing 2 hours of history while the demo plan allows 20 minutes of warm-up.
*Fix:* add an AD, "Demo envelope". It should cover warm-up duration, the maximum injection-to-email latency (which caps window length and persistence thresholds), a scripted injection schedule, the reset procedure (what is purged, what persists), and who owns the runbook.

**C2 — AD-6 groups by (pipe, detector), so one event produces duplicate alerts and emails, which breaks CAP-6.**
CAP-6 says related alerts on the same pipe are grouped into one, and "no pipe shows duplicate alerts for the same event." An injected leak will plausibly trip `leak_mass_balance` (flow in ≠ flow out), `outlier_peer` (the pipe now differs from its peers) and possibly `dq_drift` on the affected flow or methane sensor. Under AD-6 these are three OPEN alerts, and under AD-7 three emails, for one event.
*Fix:* make the grouping key per pipe, or per pipe plus an overlapping time window. Have one OPEN alert per pipe hold a list of contributing findings, with `detector_ids[]` and a combined confidence. Alternatively, define an explicit correlation rule that the alert manager applies. Fold AD-7's "nothing→OPEN" trigger into this grouped alert. Decide whether DQ findings join integrity alerts or form a separate stream (see H3).

**C3 — Alerts have no defined path to the dashboard, so the CAP-6 display is an unbounded seam.**
Alerts live in DynamoDB (AD-6). The paradigm states "Grafana reads SiteWise and never writes", and no Grafana data source for DynamoDB is listed. The only bridge is the per-pipe `alert_state` property, and its type and content are undefined. It must also represent several open alerts per pipe (AD-6), plus confidence, explanation and contributing sensors, which CAP-6 requires to be *shown*. The alert-manager author and the dashboard author will each invent a format.
*Fix:* decide how alerts are displayed. Options:
- (a) `alert_state` is a SiteWise STRING property with a fixed JSON schema: alert_id, status, detector_ids, confidence, explanation, evidence_aliases, window.
- (b) Several typed properties per pipe: `alert_open` (bool), `alert_confidence` (double), `alert_summary` (string).
- (c) Add a Grafana data source that can read the alerts table, such as Athena or a small API, and update AD-11 and the paradigm to match.

Then fix the schema in Consistency Conventions.

### High

**H1 — The detector input contract (`window`, `metadata`, invocation grain) is undefined.**
AD-5 fixes `detect(window, metadata) -> list[Finding]` but not what `window` and `metadata` are. `window` could be a DataFrame, a dict of alias to a series, or aligned or unaligned timestamps, and nothing says how gaps are represented. That matters for `dq_gap`: are missing samples NaN or absent? It also doesn't say whether `detect` is called once per pipe or once per facility. `outlier_peer` needs cross-pipe data, while `leak_mass_balance` is per pipe. This is the seam between the SiteWise reader adapter and every detector author, and it is the most parallelised seam in the build.
*Fix:* add the `Window` and `Metadata` types to AD-5: field names, timestamp type, gap representation, alignment, and the facility-scoped vs pipe-scoped call grain. Also fix the window length and the overlap between consecutive runs.

**H2 — The `facility.yaml` schema and its SiteWise mapping are not fixed, and AD-2 contradicts the Config convention.**
- `facility.yaml` is the day-1 blocker for `sim/`, `infra/` and `detect/`, but its schema isn't given: keys, nesting, where tunables and peer-group attributes live.
- The spine doesn't say how a sensor and its data contract are modelled in SiteWise. A sensor could be a property on the pipe asset, with the contract stored as `<measurement>_unit` / `_min` / `_max` / `_rate_hz` / `_owner` attributes, or a child sensor asset.
- AD-2 says detectors read metadata "from SiteWise, never from their own copy". The Config convention says tunables (thresholds, quiet period) "live in `facility.yaml`". If thresholds live only in YAML, the Lambda needs a copy, which violates AD-2. If they are pushed to SiteWise attributes, the spine must say so.

*Fix:* add a normative schema excerpt for `facility.yaml` and the SiteWise asset-model mapping (models, properties, attributes). State how tunables reach the Lambda. The cleanest options are generating them as SiteWise attributes on a facility asset, or a CDK-generated JSON config asset, with AD-2 amended to match.

**H3 — The CAP-3 "data-quality score" is undefined.**
`<measurement>_dq` is named, but not its type, its scale (0–1? 0–100? pass/warn/fail?), or how the `dq_*` findings roll up into it. CAP-3 also requires the score to *name any failing check*. A single numeric SiteWise property can't carry that, and `Finding.confidence` is not a quality score. It is also unclear whether DQ findings create alerts and emails. CAP-7 says "every alert raised" reaches the operator, and `dq_*` detector IDs fall under AD-6, so by default a flatline sensor emails the operator. That may or may not be intended.
*Fix:* define the score in Conventions: a numeric `<measurement>_dq` plus a `<measurement>_dq_failing` string, or a fixed JSON, and the rollup rule, which the handler or alert manager computes, never an individual detector. Decide whether DQ faults alert and email, or only show on the dashboard.

**H4 — There is no working deployment model for 2–4 people building in parallel, and Grafana provisioning sits outside CDK.**
AD-10 fixes one stage. It doesn't say who deploys, how concurrent `cdk deploy` runs from several laptops are prevented from overwriting each other, or whether each consultant gets a personal stack prefix during days 1–3. CI/CD is deferred, which is fine, but then a manual deploy protocol is needed.
- Amazon Managed Grafana dashboards and the SiteWise data source are not CloudFormation resources. They need the Grafana HTTP API with a service-account token, so `dashboards/` deployment is unaddressed.
- AMG requires IAM Identity Center (or SAML) to be enabled in the account or organisation. That is a day-0 prerequisite that may need a client or organisation admin.

*Fix:* add to AD-10 either per-developer stack prefixes (`poc-<initials>`) with one shared `poc` stage for integration and demo, or a single deploy owner. Add how dashboards and the data source are provisioned (a script using the Grafana API, run after `cdk deploy`) and list the account prerequisites.

**H5 — IAM is silent, so AD-3 and AD-11 aren't enforced.**
- AD-3 ("only the producer writes raw; only detection writes `_dq`/`alert_state`") is a code convention with no mechanism. The simulator's task role and the Lambda role should be scoped so each can only `BatchPutAssetPropertyValue` to its own properties, or the spine should state that this is enforced by code review only.
- "Nothing updates raw" is not guaranteed by SiteWise: a BatchPut with the same timestamp overwrites a value. Retries or a restarted simulator can therefore alter raw history.
- AD-11 names Grafana read-only permissions but no other roles: the Lambda (SiteWise read and write, DynamoDB, SNS publish), the simulator (SiteWise write, `sim_control` read), and the injection CLI (`sim_control` write).

*Fix:* add an IAM AD or table with one role per component and its allowed actions and resources, and decide how same-timestamp overwrites are prevented (for example, the simulator never resends a timestamp).

**H6 — The spec's "SiteWise for anomaly detection" constraint is deferred without being flagged as a deviation.**
SPEC Constraints: "Build on AWS, with IoT SiteWise for ingestion and anomaly detection." The spine uses custom Lambda detectors and moves SiteWise anomaly detection to next phase because it needs 14+ days of training data. That reasoning is sound, and stack.md allows a custom stack. But SPEC is canonical, so this is a spec deviation that the client or spec owner should accept explicitly. AD-9's "never backfills" rule also rules out the one route that would satisfy the constraint: backfilling 14 days of synthetic history, then training.
*Fix:* record this as an explicit decision with rationale, or an Open Question for the spec owner. Optionally allow a one-off, clearly labelled synthetic history backfill for model training as a scoped exception to AD-9, or state why not.

**H7 — AD-6 and AD-7 have no mechanism for concurrency or atomicity.**
- If a detection run exceeds 1 minute, EventBridge starts an overlapping invocation, and both can create an OPEN alert for the same key. That means duplicate alerts and duplicate emails.
- If the DynamoDB write succeeds and the SNS publish fails, the retry on the next run sees the alert already OPEN and never sends the email. That breaks CAP-7 ("every alert raised reaches the operator").

*Fix:* require a DynamoDB conditional write (or a deterministic key on pipe + group plus `attribute_not_exists`) for opening an alert, set Lambda reserved concurrency to 1, and add a `notified` flag. Send the email when an alert is OPEN and `notified = false`, then set the flag, so failed sends are retried.

### Medium

**M1 — There is no shared contract for leak and fault signatures, and methane detection has no detector.**
AD-4 correctly hides ground truth. But the simulator author and the detector author still have to agree on the physics: what a leak does to `flow_out`, `pressure_out` and `methane_ppm`, and how large the effect is. They also need to agree which fault types the simulator can inject, and that list should match `dq_flatline`/`dq_stuck`/`dq_drift`/`dq_gap`. Otherwise the injected leak may be undetectable, or detectors get tuned to one hard-coded signature. CAP-4 also says "leak *or methane-loss*", but the detector ID list has only `leak_mass_balance`, and nothing consumes `methane_ppm`.
*Fix:* add a short "injection catalogue" convention: the fault and leak types, their parameters, and the observable signature each one produces. It lives in `facility/` or `sim/` docs, and detectors may know the signature *types* but never *instances*. Add a methane detector ID (for example `leak_methane_rise`) or state that methane feeds `leak_mass_balance`.

**M2 — The deep-link format is declared "fixed and shared" but never actually stated.**
AD-8 says the format is fixed but gives no dashboard UID, path, variable parameter (`var-pipe_id`), `from`/`to` units (epoch ms), or source location. The notifier (in `detect/adapters/`) and `dashboards/` JSON are built by different people. Also, `evidence_aliases` aren't reflected in the link, so "the sensors that contributed" aren't highlighted when the link opens.
*Fix:* write the literal template, e.g. `https://<workspace>/d/<pipe-detail-uid>/pipe-detail?var-pipe_id=<pipe_id>&from=<ms>&to=<ms>`. Fix the dashboard UID as a constant, and name where the workspace URL comes from (a CDK output or an environment variable). Optionally add `var-sensor=<alias>` for each piece of evidence.

**M3 — The alias convention doesn't cover derived properties or the `<area>` vocabulary.**
Aliases for `<measurement>_dq` and `alert_state` aren't given. `alert_state` is per pipe, so `/lng1/<area>/<pipe_id>/alert_state` is the likely form, but it should be stated. The allowed `<area>` values are unspecified, and `<area>` must be in `facility.yaml`.
*Fix:* extend the aliases row to cover derived properties and point `<area>` at the `facility.yaml` schema.

**M4 — Read volume and Lambda timeout are deferred even though they shape the window contract.**
50 pipes × 6 sensors × 900 s is about 270k points per run through ExecuteQuery, every minute, with pagination and quotas. The Deferred item says "revisit if too slow". The outcome could force aggregates instead of raw values, which changes the `window` type in H1. So this isn't purely local to the adapter.
*Fix:* do a day-1 spike and record the decision, or have H1's `Window` type support both raw and aggregate resolution. Also set the Lambda timeout relative to the 1-minute schedule.

**M5 — There are no operational health signals, and the injection log's storage is undefined.**
Nothing says how the team knows, during the demo, that the simulator task is healthy or the detection Lambda is erroring. There are no CloudWatch alarms and no heartbeat. The injection log "exists only to score the demo afterwards", but its storage isn't stated, and local disk on Fargate is lost when a task restarts.
*Fix:* add minimal alarms (Lambda errors, simulator task not running, ingestion lag) sent to a team SNS topic separate from the operator topic. Put the injection log in S3 or a separate DynamoDB table that `detect/` has no IAM access to. That also makes AD-4 enforceable.

**M6 — No policy for how detectors interact on bad data.**
Detectors run in isolation (AD-5). The spine doesn't say whether `leak_mass_balance` and `outlier_peer` should ignore or down-weight sensors that DQ flags. Without a rule, one detector author will suppress on bad data and another won't, and a flatlined flow meter will raise a "credible" leak. That undermines CAP-4's "recognised as credible" and the no-black-box constraint.
*Fix:* decide whether the handler passes DQ status into `metadata` for downstream detectors (a two-pass run), or whether DQ gating happens in the alert manager.

**M7 — The peer-group definition for CAP-5 isn't fixed.**
"Same service, material and age": age as an exact value or a band? Is there a minimum group size? The `facility.yaml` author has to design the population (10–50 pipes) so the groups are big enough, and the outlier detector has to use the same key.
*Fix:* add a `peer_group` convention, for example (service, material, age band of 5 years), with a minimum of 4 pipes per group. It can be derived by `infra/` into a SiteWise attribute.

**M8 — There is no Open Questions section, and assumptions have no owners or verification steps.**
The `[ASSUMPTION]` items don't say who confirms them or when: `eu-west-1` with ExecuteQuery and AMG, Fargate, 1 Hz. Stack versions also need checking against the target region: that AMG offers Grafana 12.4 workspaces there, and that the Lambda `python3.14` runtime is available.
*Fix:* add an Open Questions list with an owner and a day-0 verification for each assumption, and include H6.

### Low

**L1 — Layering rules have no enforcement tool.** "`detect/core/` imports nothing" and "`sim/` and `detect/` never import each other" are good rules, but nothing checks them. Name import-linter contracts or a simple CI grep, even if that just runs locally.

**L2 — Teardown, cost and retention after the POC aren't covered.** SiteWise and DynamoDB retention, Managed Grafana per-user cost, and a `cdk destroy` path aren't mentioned. SiteWise assets must be disassociated and deleted in order, and the SiteWise data source must be removed from Grafana first. One line under Deferred or Operations would do.

**L3 — SNS email subscription logistics are missing.** Subscriptions need a confirmation click. The spine should say where recipient addresses are declared (CDK context or `facility.yaml`) and that confirmation is a pre-demo checklist item.

**L4 — The units convention conflicts with the measurements.** Conventions say "SI", but the measurements include `methane_ppm`. State the exceptions, or say "units as declared in the data contract".

**L5 — The frontmatter is incomplete.** `sources` omits `roadmap.md`, which constrains "design choices that must not block" the next phase. The historian-coexistence principle in stack.md (POC) is not acknowledged. AD-1's alias contract supports it, and one sentence would close the trace.

**L6 — Only unit tests are planned.** `tests/` covers detector unit tests only. An end-to-end smoke check (inject → alert → email → link opens) would protect the demo, and would naturally live alongside the C1 runbook.

## What Is Good and Should Stay

- AD-1: using the alias-addressed BatchPut as the single ingestion contract is exactly right for "accept the real stream later without rebuild".
- AD-4: keeping ground truth away from detectors is a strong credibility guard. Make it enforceable via storage and IAM (M5).
- AD-5: pure detectors with a fixed `Finding` shape and per-detector fault isolation is the right way to split work across consultants. It only needs its input types (H1).
- The capability map is complete at the mapping level, and the Deferred list is mostly sound: no Flink, no diode, no CI/CD for a 4-day POC.

## Suggested Revision Order (to unblock day 1)

1. H2 (`facility.yaml` schema), H1 (`Window`/`Metadata`) and C3 (alert display schema). These seams block parallel work.
2. C2 (grouping key) and H3 (DQ score). Then H7 (conditional writes and the `notified` flag).
3. H4 (deploy model and Grafana provisioning) and H5 (IAM table).
4. C1 (demo envelope and reset). Draft it now and finalise it on day 3 with measured latency.
5. H6 and M8 go to the spec owner as open questions.
