# Stack and Architecture Intent

This is background implementation guidance. Where it differs, the architecture spine (`ARCHITECTURE-SPINE.md`, listed in SPEC companions) is authoritative. Items are marked as applying to the **POC** or **Later**.

## AWS services

- **POC:** AWS IoT SiteWise for ingestion, asset modelling and telemetry storage. Detection uses our own deterministic Python detectors; SiteWise anomaly detection (needs 14+ days of training data) is next phase.
- **Later:** IoT Greengrass (edge), Kinesis, Managed Flink, Timestream and SageMaker, each where it fits.
- **Excluded:**
  - Amazon Lookout for Equipment: closed to new customers on 7 Oct 2025 and ends on 7 Oct 2026.
  - Amazon Monitron: closed to new customers; device sales ended in July 2025.

## Architecture principles

- **POC:** The platform coexists with the client's existing historian (for example, PI) through connectors and does not replace it.
- **POC:** The asset model, tag-to-asset mapping and a data contract per sensor are the foundation. Nearly every analytic depends on knowing which sensor sits on which pipe.
- **POC:** Raw data is stored unaltered, with lineage traceable from each alert or score back to it.
- **Later:** fast detection at the edge, with models trained in the cloud and pushed to the edge.
- **Later:** store-and-forward at the edge with backfill, and data gaps shown honestly.
- **Later:** adaptive sampling, with high-frequency transients sent as events only, and hot, warm and cold storage tiers.
- **Later:** a one-way data diode (unidirectional gateway), deployment locked to one region, and customer-held encryption keys.
- **Later:** records that can't be altered, to MRV audit grade.

## Leak detection approaches (roadmap reference)

- Mass balance across pipe segments.
- Long-horizon statistics (CUSUM) to catch slow leaks that fixed thresholds miss.
