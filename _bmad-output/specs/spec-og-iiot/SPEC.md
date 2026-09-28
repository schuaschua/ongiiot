---
id: SPEC-og-iiot
companions:
  - stack.md
  - roadmap.md
  - ../../planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md
sources:
  - ../../planning-artifacts/briefs/brief-og-iiot-2026-09-28/brief.md
  - ../../planning-artifacts/briefs/brief-og-iiot-2026-09-28/addendum.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Pipe Integrity Streaming Analytics: 4-Day POC

## Why

This spec meets a mandate and solves a pain. The mandate: an upstream liquefied natural gas (LNG) operator exports to the EU. From 1 January 2027, EU importers must show methane monitoring, reporting and verification (MRV) equivalent to the EU's own rules. The pain: the operator has just upgraded thousands of pipe sensors to stream live, but engineers can't watch the streams, don't know whether the new sensors can be trusted, and reject black-box analytics. We are the consultancy under contract to build an AWS platform that turns the streams into a few trusted, traceable insights. This 4-day proof of concept (POC) has to earn funding for the next phase. The longer-term opportunity is moving from calendar-based to risk-based inspection (see `roadmap.md`).

## Capabilities

- **CAP-1**
  - **intent:** Engineers see live sensor data for each selected pipe, from a simulated stream that behaves like the client's live AWS stream.
  - **success:** Every simulated sensor's data flows end to end into the per-pipe view during the demo.
- **CAP-2**
  - **intent:** Every sensor is tied to its pipe, with a data contract (units, valid range, sample rate, owner) and pipe attributes (service, material, age).
  - **success:** 100% of selected sensors are mapped to a pipe and have a data contract; each selected pipe has service, material and age recorded.
- **CAP-3**
  - **intent:** The OT and instrumentation team can see how trustworthy each sensor is, through a data-quality score covering drift, flatline, stuck values and gaps.
  - **success:** Every selected sensor shows a score and names any failing check. At least one simulated data-quality fault is flagged in the demo.
- **CAP-4**
  - **intent:** The platform detects leak or methane-loss events in the sensor stream.
  - **success:** It detects a leak injected into the simulated stream, and the client's engineers recognise the detection as credible.
- **CAP-5**
  - **intent:** Integrity engineers see which pipes behave differently from similar pipes (same service, material and age).
  - **success:** At least one flagged pipe is judged worth investigating by client engineers.
- **CAP-6**
  - **intent:** Every alert explains itself: it shows its confidence and the sensors that contributed, groups related alerts on the same pipe into one, and opens the raw sensor trend in one click.
  - **success:** During the demo, every alert shown is traced to its raw data in one click, and no pipe shows duplicate alerts for the same event.
- **CAP-7**
  - **intent:** Operators receive an alert message when an alert fires.
  - **success:** During the demo, every alert raised, including the injected leak, reaches the operator role (played by the consultants) as a message. Each message gives the pipe, the alert, its confidence and a link to the raw trend.

## Constraints

- 4 calendar days, 2–4 consultants, with a demo at the end of day 4 to the client's operations and engineering leads.
- Build on AWS in `ap-southeast-1` (Singapore), with IoT SiteWise for ingestion and telemetry storage. Detection uses our own deterministic detectors, not SiteWise anomaly detection (which needs 14+ days of training data). Do not use Amazon Lookout for Equipment (it ends on 7 Oct 2026) or Amazon Monitron (closed to new customers).
- Access to OT is read-only. Nothing is written back to OT systems.
- Data is simulated: we build a simulator that publishes a live stream into AWS the way the client's stream would arrive. The simulator must be able to inject faults and leaks on demand. The ingestion path must accept the real stream later without being rebuilt.
- No client data is available. We model everything as a plausible upstream LNG facility: pipes, sensors, tag names, sensor types, sample rates and pipe attributes.
- Scope is one facility and 10–50 simulated pipes, modelled so that enough of them share service, material and age for CAP-5 comparisons.
- Operator alert messages (CAP-7) are delivered by email.
- The POC is demo-only. Alerts go to consultants playing the operator role, never to client operators.
- We supply evidence to the client's own regulatory reporting. We are not the official record.
- No black-box output. Every insight must be explainable and traceable to raw data.
- Raw data is kept unaltered, and lineage is preserved from raw data to every derived number, as MRV auditability requires.

## Non-goals

- Rollout across multiple facilities.
- Integrating work orders with SAP PM or Maximo.
- Writing back to OT, closed-loop control, or advisory setpoints.
- Risk-based inspection (RBI) scheduling and inspection evidence packs.
- LNG-specific analytics: cryogenic cooldown, insulation cold spots, hydrates, corrosion under insulation.
- Production-grade security hardening. The one-way data diode and customer-held keys come after the POC.
- Views for regulators, insurers or joint-venture partners.
- MRV-grade methane reporting. The POC demonstrates detection only.

## Success signal

- At the end-of-day-4 demo, the client's operations and engineering leads watch CAP-1 to CAP-7 run live on the simulated stream. An injected leak is detected and messaged to the operator role and traced to raw data. The leads then agree to fund the next phase.

## Assumptions

- The client's methane sensors meet EU MRV measurement requirements (the client said so; not verified).
- The client's OT team accepts a one-way data diode and a read-only approach. This matters for the next phase, not the POC.
- Who funds the next phase is deferred.
