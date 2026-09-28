# Addendum: Streaming Pipe Integrity Analytics Platform (LNG)

This addendum holds detail that supports the brief for the liquefied natural gas (LNG) platform but belongs in downstream documents: the product requirements document (PRD) and the architecture.

## Parked roadmap after the POC

Taken from the brainstorm, in priority order.

### Next phase: the brainstorm's pilot scope

The brainstorm's 30-day, ~50-pipe pilot was replaced by the 4-day proof of concept (POC) once the timeline became known. Its scope is the natural next phase:

- sensor readiness report
- asset knowledge graph with tags mapped automatically
- outliers among similar pipes
- weekly risk watchlist

### Later capabilities, in priority order

1. **Methane and leak detection to MRV grade.** Mass balance across segments and long-horizon statistics (cumulative sum, or CUSUM), feeding the Oil and Gas Methane Partnership (OGMP) 2.0 and EU measurement, reporting and verification (MRV). Moved up the list because of the 1 Jan 2027 EU import requirement.
2. **Inspection business case.** Dynamic risk-based inspection (RBI) scheduling, evidence packs for extending inspection intervals, and a ledger of savings in dollars. Engage the regulator in year 1.
3. **Commissioning baseline.** Learn each pipe's normal behaviour over the first 90 days and remove daily and seasonal ambient cycles.
4. **Starting data for the models.** The platform has no failure history yet. Fill the gap by digitising paper inspection records, running rule-capture workshops with veteran engineers, and flagging deviations from physics-based hydraulic models.
5. **LNG-specific analytics.** Cryogenic cooldown rate, insulation cold spots, hydrate risk, corrosion under insulation, and remaining life.
6. **Fleet learning.** Scan the whole fleet for signatures that preceded past failures, and spread risk from inspection findings to similar pipes.

## Architecture intent

Input for the architecture and spec.

- **Amazon Web Services (AWS) managed services:** IoT SiteWise/Greengrass, Kinesis, Managed Flink, Timestream and SageMaker. Avoid Lookout for Equipment (see its end-of-life date under Landscape research).
- **Existing historian:** coexist with it (for example, PI) through connectors rather than replacing it.
- **Detection:** fast detection at the edge, with models trained in the cloud and pushed to the edge.
- **Edge resilience:** store-and-forward with backfill, and data gaps shown honestly.
- **Data volume and cost:**
  - adaptive sampling
  - high-frequency transients sent as events only
  - hot, warm and cold storage tiers
- **Operational technology (OT) security:** one-way data diode, read-only from OT, deployment locked to one region, and customer-held encryption keys.
- **Foundation:** the asset knowledge graph, tag auto-mapping and a data contract per sensor.
- **Auditability for MRV:** records that can't be altered, and traceable data lineage.

## Landscape research

Web research, September 2026; [unverified] items need checking.

**AWS services**

- Implication: build on IoT SiteWise or a custom machine learning (ML) stack.
- Amazon Lookout for Equipment: closed to new customers on 7 Oct 2025 and ends on 7 Oct 2026. AWS points customers to IoT SiteWise, which added multivariate anomaly detection (Jul 2025) and model retraining (Sep 2025).
- Amazon Monitron: closed to new customers; device sales ended in Jul 2025.

**Incumbents** (all sell broad platforms)

- Opening for us: AWS-native streaming, sensor data-quality checks, fast time-to-value on a focused pilot.
- Cognite Data Fusion / Atlas AI: agentic AI. Oil and gas customers include Aker BP, ADNOC Offshore and Hess.
- AVEVA: PI System is being folded into CONNECT.
- Emerson: now owns AspenTech; sells Permasense/Rosemount ET310 wireless thickness sensors.
- Baker Hughes Cordant asset performance management (APM): added Antea mechanical integrity/RBI, generally available Q1 2026.
- GE Vernova APM Integrity: RBI (American Petroleum Institute, API 580/581), integrity operating windows (IOWs), thickness monitoring.
- Honeywell Forge.
- SLB Lumi: well-focused.
- Sensor Networks smartPIMS: wireless ultrasonic thickness sensors with a cloud portal.

**RBI value**

- Industry sources cite a 30–50% reduction in inspection points compared with time-based inspection. These are vendor and consultant figures, so treat them as indicative.
- API is sunsetting the semi-quantitative RP 581 method (Aug 2025).

**Regulation**

- EU Methane Regulation 2024/1787:
  - OGMP 2.0 Level 5 required for operated assets by Feb 2027.
  - From 1 Jan 2027, importers must show equivalent MRV. This affects LNG exporters selling into the EU.
- US:
  - The Waste Emissions Charge was disapproved in March 2025 [verify details].
  - The Environmental Protection Agency (EPA) extended some OOOOb/c deadlines; the rest of the monitoring requirements remain.
- Standards: API RP 584 (IOWs), 2nd edition 2021; API 570 updated in 2024.

**Pilot purgatory**

- McKinsey (2017): fewer than 30% of pilots scale.
- Causes:
  - technical rather than business success metrics
  - no executive sponsor
  - politics between IT and OT
  - no architecture designed for scale
  - missing data context
  - security design left late
  - weak business case for scale-up

**Sources:** aws.amazon.com (Lookout for Equipment sunset; SiteWise anomaly detection and retraining), eur-lex 2024/1787, oxfordenergy.org ET44, api.org RP 584, inspectioneering.com, mckinsey.com (pilot purgatory), industryweek.com, arcweb.com, gevernova.com, bakerhughes.com, emerson.com, sensornetworksinc.com.
