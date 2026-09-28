- source_plan: none
  summary: Build the simulator that streams the modelled facility into SiteWise by alias in real time, with fault and leak injection via sim_control, deployed on ECS Fargate (AD-1, AD-3, AD-4, AD-9, AD-15).
  evidence: Split from the day-1 build intent; depends on the facility model and SiteWise asset stack being built first.
- source_plan: none
  summary: Run the day-1 spikes — ExecuteQuery latency at full load, IAM alias-prefix write scoping, and IAM Identity Center for Amazon Managed Grafana — and record the outcomes against AD-17, AD-18 and the Grafana fallback.
  evidence: Split from the day-1 build intent; the spikes need the asset stack and simulator stream deployed to produce meaningful results.
