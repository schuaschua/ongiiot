---
review: reconcile
target: ../ARCHITECTURE-SPINE.md
inputs:
  - ../../../../specs/spec-og-iiot/SPEC.md
  - ../../../../specs/spec-og-iiot/stack.md
date: '2026-09-28'
verdict: 'Needs revision: mostly aligned, one direct contradiction (alert grouping) and several quiet requirements that are missing or unenforced'
---

# Reconciliation Review — Architecture Spine vs SPEC + stack

## Verdict

The spine covers most of the spec well: a stable ingestion contract (AD-1), one facility model (AD-2), raw data written once (AD-3), no ground-truth leakage (AD-4), a fixed Finding shape with confidence and evidence (AD-5), a one-click deep link (AD-8), on-demand injection (AD-9) and read-only access to OT (AD-11). The non-goals are respected.

It still needs revision. One rule directly contradicts CAP-6: grouping is per detector, not per pipe or event. Also, one stated constraint (SiteWise for anomaly detection) is quietly replaced without an explicit decision. Several quiet requirements are missing or not enforced: demo-only recipients, historian coexistence, lineage to every derived number, and delivery of every alert.

Severity: **High** means it contradicts the spec or puts the demo success signal at risk. **Medium** means a spec requirement has no binding rule. **Low** means a clarification or wording issue.

## Findings

### F-1 [High] Alert grouping is per detector, but CAP-6 requires grouping related alerts on a pipe into one (contradiction)

- **Spec:** CAP-6 says "groups related alerts on the same pipe into one". Its success criterion is "no pipe shows duplicate alerts for the same event".
- **Spine:** AD-6 allows "at most one `OPEN` alert per `detector_id`" per pipe.
- **Gap:** One injected leak will plausibly trigger `leak_mass_balance`, `outlier_peer` (the pipe now differs from its peers), and possibly `dq_drift` on the pressure or flow sensors. The result is 2 or 3 open alerts and 2 or 3 emails for one event on one pipe. That fails CAP-6 live in the demo. AD-7 then multiplies the emails.
- **Fix:** Make the unit of grouping the pipe, or the pipe plus an event correlation window. The rule would be one `OPEN` alert per pipe, holding a list of contributing findings, with each finding's detector, confidence and evidence. Alternatively, define an explicit correlation rule: findings on the same pipe with overlapping windows merge. Define how the combined confidence is derived. Decide whether DQ findings are grouped with integrity alerts or kept as a separate alert class.

### F-2 [High] "Every alert raised reaches the operator" is not guaranteed (CAP-7)

- **Spec:** CAP-7 success is "every alert raised, including the injected leak, reaches the operator role".
- **Spine:** AD-7 says the notifier publishes only on the change from nothing to `OPEN`. The Errors convention says "a failure in an adapter fails that invocation, and the next scheduled run retries".
- **Gap:** If the DynamoDB write succeeds and the SNS publish then fails, or the Lambda times out, the next run sees an alert that is already `OPEN`. It never sends the email, so the alert is silently lost. The spine has no `notified` flag and no outbox.
- **Fix:** Add a `notified_at` attribute (or an outbox) to the alert record. The notifier sends for any `OPEN` alert where `notified_at` is null and sets the flag after a successful publish. That gives at-least-once delivery without email storms.

### F-3 [High] The SPEC constraint "IoT SiteWise for ... anomaly detection" is replaced without a recorded decision

- **Spec:** "Build on AWS, with IoT SiteWise for ingestion **and anomaly detection**." stack.md allows "alternatively, use a custom machine learning stack".
- **Spine:** Detection is custom pure-Python detectors in Lambda. SiteWise native anomaly detection is moved to *Deferred* because it "needs 14 or more days of training data". AD-9 also forbids backfill: "never backfills or speeds up time".
- **Gap:**
  - The deviation from a named constraint is buried in the Deferred list. It is not an AD with rationale, and the client stakeholders expect SiteWise anomaly detection.
  - AD-9's no-backfill rule also closes the obvious way around the 14-day limit: loading synthetic history into SiteWise before the demo to train a model.
  - The custom detectors are rule-based statistics, not ML. That is fine for explainability, but it should be stated as the deliberate reading of "custom stack".
- **Fix:** Add an explicit AD recording that detection uses explainable custom detectors (allowed by stack.md), and why. Then either allow a one-off, separately labelled historical seed to train a SiteWise model for a side-by-side view, or state plainly that SiteWise anomaly detection is out of scope for the POC. Get sign-off, because this touches a stated constraint.

### F-4 [High] The demo-only rule (alerts go only to consultants, never to client operators) is not bound

- **Spec:** "The POC is demo-only. Alerts go to consultants playing the operator role, never to client operators." CAP-7 also says the operator role is "played by the consultants".
- **Spine:** The rule appears only as a diagram label ("Consultants as operators"). No AD controls who may subscribe to the SNS topic.
- **Gap:** SNS email subscriptions are easy to add, including by a client lead during the demo. Nothing prevents a client operator from receiving an alert that looks operational.
- **Fix:** Add a rule:
  - The SNS subscriptions are defined only in CDK from an allowlist of consultant addresses in config.
  - The topic policy denies `Subscribe` from outside the deploying role.
  - Every message carries a "POC / simulated data — not an operational alert" banner.

### F-5 [Medium] Lineage "to every derived number" is incomplete

- **Spec:** "lineage is preserved from raw data to every derived number, as MRV auditability requires". stack.md adds "lineage traceable from each alert **or score** back to it".
- **Spine:** Alerts carry `evidence_aliases` and a window (AD-8). The `<measurement>_dq` scores and `alert_state` values carry no lineage: no source window, no detector ID, no algorithm or config version. Alerts also do not record the detector version or the thresholds used, and thresholds live in the editable `facility.yaml`.
- **Gap:** You cannot rebuild how a given DQ score or confidence was produced. That is an MRV-relevant quiet requirement.
- **Fix:**
  - Record `detector_version` and a config hash (of the `facility.yaml` tunables) on every alert, and write both to the lineage log for every `_dq` write.
  - Write the `_dq` value with the same timestamp as the end of its window, or store a small lineage record (window, inputs, version) per run, for example as a JSON log line keyed by `run_id`.

### F-6 [Medium] Raw-data immutability is a convention, not enforced

- **Spec:** "Raw data is kept unaltered."
- **Spine:** AD-3 says "nothing updates or deletes" raw properties.
- **Gap:**
  - `BatchPutAssetPropertyValue` overwrites a value that has the same alias and timestamp. A restarted simulator, or a real gateway replaying data, can silently alter raw data.
  - Nothing states that the detection Lambda's IAM role cannot write raw properties.
  - Nothing states that out-of-range values are stored as received rather than clamped.
  - Nothing states the SiteWise retention setting for raw data.
- **Fix:**
  - Add IAM scoping: the detection role may put only to `*_dq` and `alert_state`, and the producer role only to raw aliases.
  - Store values exactly as received, with no clamping.
  - Set SiteWise hot-tier retention to at least the POC duration.
  - Note that the simulator must never re-emit an existing timestamp.

### F-7 [Medium] Coexistence with the existing historian (for example, PI) is missing

- **stack.md (POC principle):** "The platform coexists with the client's existing historian (for example, PI) through connectors and does not replace it."
- **Spine:** The historian is not mentioned. AD-1 says "No other raw-data ingestion path exists".
- **Gap:** The POC principle is dropped. AD-1's wording may also rule out a historian connector as a later ingestion route.
- **Fix:** State that a historian connector is a producer that lands behind the same SiteWise alias contract, either as a SiteWise Edge source or a connector feeding `BatchPutAssetPropertyValue`. State that the platform never becomes the system of record in place of the historian. Even if nothing is built for it in the POC, record the principle.

### F-8 [Medium] "Accept the real stream later without a rebuild" rests on an unmarked assumption

- **Spec:** CAP-1 says the "simulated stream behaves like the client's **live AWS stream**". The constraints say the "ingestion path must accept the real stream later without being rebuilt".
- **Spine:** AD-1 assumes the real stream arrives through a SiteWise Edge gateway ("which is how a SiteWise Edge gateway delivers data"). This is not marked `[ASSUMPTION]`.
- **Gap:** The spec implies the client already streams into AWS, possibly through IoT Core MQTT or Kinesis. If so, the real path needs an IoT Core rule or bridge to SiteWise, and the "no rebuild" promise holds only if that bridge is anticipated. Separately, the alias convention `/lng1/<area>/<pipe_id>/<measurement>` will not match real tag names.
- **Fix:**
  - Mark the SiteWise Edge assumption and name the adapter needed if the client's stream is IoT Core or Kinesis. For example: an IoT Core rule action that calls SiteWise `BatchPutAssetPropertyValue` counts as "no rebuild".
  - Add a rule that no component parses meaning (pipe, measurement) out of an alias string. Mapping comes only from `facility.yaml` and SiteWise metadata, so real tag names can be mapped in without code changes.

### F-9 [Medium] The dashboards cannot show alert detail (confidence, contributing sensors)

- **Spec:** CAP-6: "every alert shown ... shows its confidence and the sensors that contributed ... traced to its raw data in one click".
- **Spine:** Grafana reads only SiteWise. Alerts live in DynamoDB. The only mirror is a per-pipe `alert_state` property with no defined shape.
- **Gap:** The dashboards cannot show confidence or contributing sensors for an alert unless `alert_state` carries them. That format is undefined, and the value may be a single string or number.
- **Fix:** Define the `alert_state` payload, for example a JSON string with `alert_id`, `status`, `confidence` and `evidence_aliases`. Alternatively, give Grafana a read-only DynamoDB or Athena source for the alerts table and state that in AD-11 and AD-8.

### F-10 [Medium] The DQ score's shape and how it names failing checks are undefined (CAP-3)

- **Spec:** "Every selected sensor shows a score **and names any failing check**" (drift, flatline, stuck, gaps).
- **Spine:** A `<measurement>_dq` property exists per sensor, but its type, range and failing-check encoding are not defined. `Finding` carries `pipe_id`, not a sensor ID. Sensor attribution is only through `evidence_aliases`.
- **Fix:**
  - Define the `_dq` value, for example a 0–1 score plus a companion `<measurement>_dq_fail` string listing the failing `dq_*` IDs.
  - Define how the four checks combine into the score.
  - Make sure a sensor with no data (a gap) still gets a score, since a gap produces no windows.

### F-11 [Medium] The deep link may not open the *raw* trend of the *contributing* sensors

- **Spec:** CAP-6 says "opens the raw sensor trend in one click".
- **Spine:** AD-8 builds the link "only from" `evidence_aliases` and the window, but then uses only `pipe_id` and `from`/`to`. The contributing sensors are not in the link.
- **Gap:** The link opens the whole pipe dashboard, not the evidence sensors. Grafana's SiteWise panels also default to aggregates on longer ranges, so what is shown may not be raw.
- **Fix:**
  - Include the evidence aliases as a dashboard variable (multi-value), or highlight them.
  - Require the pipe-detail panels to query raw values (not aggregates) for the evidence window.
  - Write the exact link template into AD-8.

### F-12 [Medium] Methane-loss detection is not covered by a named detector (CAP-4)

- **Spec:** CAP-4 covers "leak **or methane-loss** events".
- **Spine:** `methane_ppm` is measured, but the only leak-family detector is `leak_mass_balance`.
- **Fix:** Add a methane detector ID, for example `leak_methane_ppm`, or state that mass balance plus methane corroboration forms one detector and that `methane_ppm` is part of its evidence. The injected leak should visibly move both flow balance and methane, so engineers find it credible.

### F-13 [Low] Alerts can reopen and duplicate after the quiet period

- **Spine:** AD-6 closes an alert after a quiet period. A new finding then creates a new alert and a new email.
- **Gap:** A detector that flickers around its threshold during a long injected leak would raise duplicate alerts for the same event, which violates the CAP-6 success criterion.
- **Fix:** Set the quiet period to be longer than the longest detector window. Consider "reopen the recently closed alert" instead of creating a new one within a grace period.

### F-14 [Low] The peer-group definition for CAP-5 and its data prerequisite are not stated

- **Spec:** Similar means the "same service, material and age". The facility must be "modelled so that enough of them share service, material and age".
- **Spine:** AD-2 carries the attributes, but nothing says how age is bucketed or what the minimum peer-group size is.
- **Fix:** Define age bands in `facility.yaml` and a generation-time check, for example every pipe in a group of at least N peers. Also add a generation check for CAP-2's "100% of selected sensors mapped and have a data contract": fail generation otherwise.

### F-15 [Low] "Evidence, not the official record" is not reflected

- **Spec:** "We supply evidence to the client's own regulatory reporting. We are not the official record." MRV-grade reporting is a non-goal.
- **Spine:** Silent.
- **Fix:** Add one line saying that outputs and emails are labelled as evidence and never as a reporting record. It can be combined with the banner in F-4.

### F-16 [Low] AD-11 wording contradicts AD-3

- AD-11 says "No component writes to a data source", yet the detection job writes `_dq` and `alert_state` into SiteWise, which holds the raw data.
- **Fix:** Reword to "No component writes to OT or client source systems; platform stores are written only as AD-3 and AD-6 allow."

### F-17 [Low] Detection latency against the live demo is unstated

- **Spec (success signal):** CAP-1 to CAP-7 run live, and an injected leak is detected and messaged.
- **Spine:** Detection runs every minute over 15-minute windows (Deferred). The time from injection to email is not bounded.
- **Fix:** State a target, for example an email within 5 minutes of injection. Size the leak detector's window and the injection magnitude to meet it.

### F-18 [Low] Fault types that can be injected are not enumerated

- **Spec:** The simulator must inject "faults and leaks on demand". At least one DQ fault must be flagged.
- **Spine:** AD-9 defines a control item but does not list the injectable types.
- **Fix:** List the types: `flatline`, `stuck`, `drift`, `gap`, and `leak` (magnitude, pipe, start and duration). Each should map to the detector expected to catch it, so each DQ check can be demonstrated. Also state where the injection log lives, and that `detect/` has no IAM access to it (this enforces AD-4).

## Covered correctly (no action)

- CAP-1 live view: AD-1, AD-9 and the pipe dashboard.
- CAP-2 data contract fields (units, valid range, sample rate, owner) and pipe attributes: AD-2.
- Excluded services (Lookout for Equipment, Monitron): not used.
- Read-only OT and no write-back: AD-11.
- Email delivery through SNS, and message content (pipe, alert, confidence, link): AD-7.
- Scope of 10–50 pipes and one facility: pipe IDs and the `/lng1/` prefix.
- Detectors never see injected ground truth: AD-4.
- Non-goals (multiple facilities, SAP PM or Maximo, write-back, RBI, LNG-specific analytics, security hardening, other stakeholder views, MRV-grade reporting): none are violated. The data diode and keys are correctly deferred.
- stack.md Later items (edge, Kinesis, Flink, tiers, diode, immutable records): correctly deferred.
- CUSUM and slow-leak detection are a roadmap reference only. They can optionally be noted as a future detector behind AD-5.
