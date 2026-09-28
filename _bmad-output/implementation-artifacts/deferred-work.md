- source_plan: none
  summary: Build the simulator that streams the modelled facility into SiteWise by alias in real time, with fault and leak injection via sim_control, deployed on ECS Fargate (AD-1, AD-3, AD-4, AD-9, AD-15).
  evidence: Split from the day-1 build intent; depends on the facility model and SiteWise asset stack being built first.
- source_plan: none
  summary: Run the day-1 spikes — ExecuteQuery latency at full load, IAM alias-prefix write scoping, and IAM Identity Center for Amazon Managed Grafana — and record the outcomes against AD-17, AD-18 and the Grafana fallback.
  evidence: Split from the day-1 build intent; the spikes need the asset stack and simulator stream deployed to produce meaningful results.
- source_plan: `_bmad-output/implementation-artifacts/plan-facility-model-sitewise.md`
  summary: Verify on the first real deploy that the seed Lambda's BatchPutAssetPropertyValue grant on pipe asset ARNs authorises writes addressed by propertyAlias (else add the time-series resource type).
  evidence: maybe-false, high if true (the seed and the stack would fail with AccessDenied). A real deploy of stage poc-dj, or AWS IAM docs for the iotsitewise time-series resource type, would settle it. This is the spine's AD-17 open question.
- source_plan: `_bmad-output/implementation-artifacts/plan-simulator.md`
  summary: Package and deploy the simulator — sim/Dockerfile (python:3.14-slim from uv.lock) and a per-stage CDK SimulatorStack with sim_control and injection_log tables, a public-subnet VPC without NAT, an ECS Fargate service (desired 1) and the AD-17 task role, depending on SiteWiseAssetsStack.
  evidence: Split from the simulator plan at the token gate (about 1,900 tokens against the 1,600 target); the simulator runtime is testable locally without it.
