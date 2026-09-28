# Intent: Streaming Pipe Integrity Analytics Platform (LNG)

Source: brainstorm 2026-09-28. Input for bmad-product-brief / bmad-prd.

## Context
- Customer: a traditional ("old-school") upstream liquefied natural gas (LNG) operator with thousands of pipes across several facilities.
- They recently upgraded every pipe sensor to live streaming. Operational technology (OT) data will reach us through Amazon Web Services (AWS), and we build the platform that analyses it as it arrives.
- The sensors are brand new: no failure history, unknown data quality, and tag names that don't follow a consistent pattern. Engineers are sceptical of black-box analytics.

## Problem and goal
- Problem: streaming data from thousands of pipes gives no value on its own. Engineers can't watch it all, and calendar-based inspection and alarm floods continue.
- Goal: turn the new data streams into fast, visible value the customer trusts, starting within weeks, and grow into a platform they call indispensable within 3 years.
- Headline business case: move from calendar-based to condition- and risk-based inspection, with savings recorded in a dollar value ledger (inspections avoided, unplanned trips avoided) that justifies the sensor spend.

## Target users and their jobs
| User | Job they hire the platform for |
|---|---|
| Integrity engineer | "Tell me which 20 of my 5,000 pipes to worry about", and give me evidence I can defend to the regulator |
| Control room operator | Fewer, grouped, owned alerts instead of alarm floods |
| Maintenance / inspection planner | Prioritise and bundle inspections by condition |
| OT / instrumentation team | Know the new sensors are healthy; control what data leaves site |
| Plant manager / executive | Trips avoided and a value ledger that proves ROI |

## Product principles (the trust ladder)
Trust is the product. The customer moves up at their own pace:
1. **Observe**: show data and data quality only.
2. **Advise**: ranked insights and recommendations.
3. **Auto work order**: alerts raise work orders in their enterprise asset management system (SAP PM or Maximo).
4. **Auto control**: advisory setpoints first; closed loop only once proven trustworthy.

Rules at every step:
- No black box: one click from any insight down to the raw sensor trend. Explanations name the contributing sensors and similar past events.
- Humble alerts: each shows its confidence and "why I could be wrong".
- Quiet by default: an alert budget per shift, related alarms grouped into one incident per segment, and every alert has a named owner.
- Safe change: new or changed models run in shadow mode with versioned change notes. One-tap "useful / not useful" feedback on every alert.
- Speak their language: engineering terms such as API 570 and ASME B31.3, not data-science jargon. The platform learns their tag names; nobody has to rename tags.
- Read-only toward OT at first. The OT team co-owns what leaves site.

## First release: 30-day lighthouse pilot (~50 pipes, one facility)
Scope:
1. **Sensor readiness report**: per-tag data-quality score (drift, flatline, stuck value, gaps) and a map of which sensors are live.
2. **Asset knowledge graph (first version)**: pipes, sensors, piping and instrumentation diagrams (P&IDs) and work orders linked together, with tags mapped to assets automatically and a data contract per sensor (units, range, rate, owner).
3. **Sibling-pipe outliers**: compare each pipe with others of the same service, material and age, and flag the one that stands out.
4. **Weekly risk watchlist**: ranked pipes to worry about, each drillable to raw trends.

Success criteria: agreed with the customer before the pilot starts. Candidates: % of tags mapped and scored, readiness issues confirmed by the OT team, watchlist items engineers rate useful, and alert noise within budget.

## Platform capabilities beyond the pilot (in priority order)
1. **Inspection business case**: dynamic risk-based inspection scheduling, evidence packs for extending inspection intervals, and the value ledger. Engage the regulator in year 1. First win: replace one calendar-based inspection campaign with condition-based inspection.
2. **Commissioning baseline**: learn each pipe's "normal fingerprint" over its first 90 days and remove daily and seasonal ambient cycles.
3. **Cold-start priors from their past**: digitise paper inspection records, hold rule-capture workshops with veteran engineers (the platform proposes rules; engineers approve them), and use physics-first hydraulic models whose deviations flag anomalies.
4. **LNG-specific analytics**: cryogenic cooldown-rate monitoring, insulation failure and cold-spot detection, hydrate risk envelope, corrosion under insulation, and live corrosion rate and remaining life.
5. **Loss and leak detection**: mass balance across segments and long-horizon statistics (CUSUM, cumulative sum) to catch slow leaks, feeding methane emissions reporting (OGMP 2.0, the Oil & Gas Methane Partnership).
6. **Fleet learning (long-term advantage)**: store failure-precursor signatures and scan for them fleet-wide, and spread risk from inspection findings to similar pipes. This gets better with scale and supports a multi-tenant product for other LNG operators.

## Architecture intent
- **AWS**, using managed services where they fit (e.g. IoT SiteWise/Greengrass, Kinesis, Managed Flink, Timestream, SageMaker). Coexist with the existing historian (e.g. PI) through connectors; don't replace it.
- **Reflex at the edge, brain in the cloud**: fast local detection on edge gateways. Models are trained in the cloud and pushed to the edge.
- **Edge resilience and cost**: store-and-forward with backfill, and gaps shown honestly. Adaptive sampling raises the rate when something interesting happens. High-frequency transients are sent as events only.
- **Tiered storage** (hot, warm, cold) so streaming everything stays affordable.
- **Latency tiers per use case**: milliseconds at the edge, minutes in the stream, daily batch.
- **OT security**: one-way data diode (unidirectional gateway), read-only from OT. Deployment is locked to one region, with customer-held encryption keys.
- **Foundation**: the asset knowledge graph, tag auto-mapping and data contracts. Nearly every analytic depends on knowing which sensor sits on which pipe.
- Asset templates are configurable, with a path to multi-tenancy.

## Out of scope for now
- Automatic control or writing back to OT.
- Automatic work-order creation (comes after trust is earned in the advise stage).
- Rollout across all facilities in one go (neighbouring sites join by request after the lighthouse).
- Regulator portal, insurer and joint-venture views, cross-operator benchmark, outcome-based pricing.

## Open questions and assumptions to validate
- Which facility and which ~50 pipes: do they share enough service, material and age for sibling comparison?
- Which sensor types are installed (thickness/ER (electrical resistance) probes, vibration, acoustic, temperature, pressure) and at what sampling rates?
- Can we access P&IDs, tag lists, historian data and maintenance records for the knowledge graph?
- Is the regulator open to data-driven extension of inspection intervals?
- Does the OT team accept the data diode and co-ownership model? What are their cyber constraints?
- Pilot success criteria and how value-ledger dollars are measured.
- Commercial model (flat vs per-tag pricing) and data sovereignty requirements.
