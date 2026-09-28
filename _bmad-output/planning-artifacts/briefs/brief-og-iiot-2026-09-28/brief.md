---
title: "Product Brief: Streaming Pipe Integrity Analytics Platform (LNG)"
status: final
created: 2026-09-28
updated: 2026-09-28
---

# Product Brief: Streaming Pipe Integrity Analytics Platform (LNG)

## Executive Summary

Our client is an upstream liquefied natural gas (LNG) operator with thousands of pipes across several facilities. They have recently upgraded every pipe sensor to stream data live. The contract is signed: we are the consultancy building a platform on Amazon Web Services (AWS) that analyses this operational technology (OT) data as it arrives. We have **4 days to build a proof of concept (POC)**, and success means the client funds the next phase.

Live data on its own changes nothing. The client's engineers can't watch thousands of data streams, they are wary of black-box analytics, and nobody yet knows whether the new sensors can be trusted. The platform must turn the streams into a small number of insights engineers believe, each traceable back to the raw data.

The timing is set by regulation. The client sells LNG into the European Union (EU). From 1 January 2027, EU importers must show that the gas comes with methane monitoring, reporting and verification (MRV) equivalent to the EU's own rules. Continuous leak and methane detection from sensors the client already has is the fastest route to that evidence. The larger, longer-term value is moving from calendar-based to risk-based inspection.

## The Problem

- **Too much data, too little attention.** Thousands of pipes produce continuous streams. Without ranking and grouping, engineers are likely to face floods of alarms and learn to ignore them.
- **Brand-new sensors with no record behind them.** There is no failure history, data quality is unknown, and tag names are inconsistent. Insights built on unchecked data will lose the client's trust.
- **A traditional engineering culture.** Engineers trust trends and first principles, not models. Any output that can't be explained will be rejected.
- **A regulatory deadline.** EU methane import requirements start on 1 January 2027. Without credible, auditable monitoring data, the client puts its terms of sale to European buyers at risk.
- **Costly calendar-based inspection.** Inspection is scheduled by date, not by condition. Industry sources cite 30–50% fewer inspection points under risk-based inspection (RBI); these are vendor figures and indicative only.

## The Solution

A streaming analytics platform on AWS that tells engineers **which pipes need attention and why**, and earns their trust step by step. The client moves up a trust ladder at its own pace: observe → advise → automatic work orders → automatic control.

**Trust principles, built in from the POC:**
- Every insight links in one click to the raw sensor trend.
- Alerts show their confidence and the sensors that contributed.
- The platform stays quiet by default: related alarms are grouped, and each alert has a named owner.
- Data flows one way, read-only, from OT to the platform. Nothing is written back to OT.
- Data is auditable from day one: the source of every number, a data contract per sensor, and records that can't be altered.

## What Makes This Different

We don't claim a technology moat. The incumbents (Cognite, AVEVA, GE Vernova, Baker Hughes Cordant, Emerson) sell broad platforms with long rollouts. Our advantages are:
- **Speed:** a working POC on the client's own data in 4 days.
- **Built on AWS from the start:** IoT SiteWise for ingestion and anomaly detection. We avoid Amazon Lookout for Equipment, which shuts down on 7 October 2026.
- **Sensor data quality first:** we check whether the new sensors can be trusted before building analytics on them.
- **Focus:** pipe integrity and methane monitoring for LNG, not a general-purpose platform.
- **Continuous, not periodic:** methane monitoring from existing sensors, rather than periodic satellite, drone or infrared-camera surveys.

## Who This Serves

| User | What they need |
|---|---|
| Integrity engineer | "Which of my thousands of pipes should I worry about, and why?" — with evidence they can defend |
| Control room operator | Fewer, grouped alerts, each with an owner, instead of alarm floods |
| Health, safety and environment (HSE) / sustainability lead | Credible methane data for EU MRV and Oil & Gas Methane Partnership (OGMP 2.0) reporting |
| OT / instrumentation team | Confidence that the new sensors are healthy, and control over what data leaves site |
| Operations leadership | Evidence of value that justifies funding the next phase |

Budget holder: `[ASSUMPTION]` operations and engineering leadership, with methane compliance as a strong second driver that may involve the HSE budget.

## Scope: 4-Day POC

**In:**
- One facility, about 10–50 pipes.
- Real client sensor data: the live stream on AWS or a historical export replayed as a stream.
- **Ingestion** with AWS IoT SiteWise into a live view of the selected pipes.
- **Sensor data-quality score:** drift, flatline, stuck values and gaps, for each sensor.
- **Leak and methane detection demo:** at least one detection scenario on the client's data.
- **Outlier among similar pipes:** flag any pipe that behaves differently from pipes with the same service, material and age.
- **Drill-down** from every alert to the raw sensor trend.
- End-of-day-4 demo to the client's operations and engineering leads.

**Out (roadmap after the POC):**
- Rollout across multiple facilities.
- Work-order integration with SAP PM or Maximo.
- Any writing back to OT or closed-loop control.
- RBI scheduling.
- LNG-specific analytics (cryogenic cooldown, insulation cold spots, hydrates, corrosion under insulation).
- Production-grade security hardening.
- Regulator, insurer and joint-venture partner views.

**Team:** 2–4 consultants. `[ASSUMPTION]` The client provides data access and a technical contact from day 1.

## Success Criteria

**POC (day 4):**
- **Primary:** the client's operations and engineering leads agree to fund the next phase.
- Data from all selected sensors flows end to end, and each sensor has a data-quality score.
- The leak/methane demo finds a real or replayed event that the client's engineers recognise as credible.
- Engineers judge at least one outlier insight worth investigating.
- Every alert can be traced to raw data during the demo.

**Next phase (for the funding conversation):**
- Methane monitoring evidence that supports EU MRV equivalence before 1 January 2027.
- A measurable reduction in alert noise and in inspection effort for one inspection campaign.
- A named executive sponsor on the client side. Lack of a sponsor is one of the most common reasons industrial IoT pilots fail to scale.

## Risks and Open Questions

- **Data access on day 1** is the critical-path risk for a 4-day build. Will it be live AWS streaming or a historical export, and who grants access?
- **Methane measurement standard:** the client says the sensors can detect methane. We still need to confirm which sensor types they are and whether they meet EU MRV requirements.
- **Liability:** are we the official record for regulatory reporting, or do we supply evidence to the client's own reporting?
- **Choosing the pipes:** the selected pipes must share enough service, material and age for the outlier comparison to work.
- **OT security constraints:** does the client's OT team accept a one-way data diode and a read-only approach?
- **Who pays:** operations and engineering, HSE, or both?

## Vision

Within 2–3 years, the client runs pipe integrity and methane compliance from this platform across all its facilities. It replaces calendar-based inspection with risk-based inspection, produces continuous methane evidence for EU buyers, and moves up the trust ladder to automatic work orders. For us, the same asset templates and edge/cloud architecture become a repeatable offering for other LNG operators facing the 2027 EU deadline.
