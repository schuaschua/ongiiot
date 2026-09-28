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
- source_plan: `_bmad-output/implementation-artifacts/plan-simulator.md`
  summary: Record the effective (cancelled) end of an injection in injection_log, not only the planned end_at, so demo scoring uses true windows.
  evidence: log_first_apply writes end_at once, at first apply; a later cancel item shortens the real window, but the log keeps the planned end.
- source_plan: `_bmad-output/implementation-artifacts/plan-simulator.md`
  summary: Add `ops/inject list` to show active and recent injections with their IDs, so an operator can cancel without the original output.
  evidence: cancel needs the ULID printed at creation; without it the operator must scan DynamoDB by hand to stop a running leak.
- source_plan: `_bmad-output/implementation-artifacts/plan-cicd-github-actions.md`
  summary: Grafana provisioning (AD-16 `infra/grafana_provision.py`) needs a step in `deploy.yml` and Grafana API permissions for the CI deploy path.
  evidence: The deploy role can only assume CDK bootstrap roles, and deploy.yml has no post-deploy provisioning step; surfaces in Story 1.5.
- source_plan: `_bmad-output/implementation-artifacts/plan-1-4-simulator-fargate.md`
  summary: Decide the cross-stack reference strength for the pipe asset ARNs SimulatorStack imports from SiteWiseAssetsStack (set `@aws-cdk/core:defaultCrossStackReferences`, or pass the ARNs another way).
  evidence: CDK defaults to strong references (exports) and warns at synth; a change that replaces pipe assets would fail the SiteWise stack deploy while SimulatorStack still imports the old exports.
- source_plan: `_bmad-output/implementation-artifacts/plan-1-4-simulator-fargate.md`
  summary: The Story 4.4 simulator health alarm ("running tasks < 1") must respect the `simulator=off` switch (compare against desired count, or disable when off).
  evidence: With desired count 0 the alarm as specified in AD-15 would fire permanently.
- source_plan: `_bmad-output/implementation-artifacts/plan-1-4-simulator-fargate.md`
  summary: Update the architecture `.memlog.md` CI/CD entries (poc now deploys by manual dispatch from main; no reviewer gate) and add the Story 1.4 decisions (Fargate, IAM cannot scope by alias, on/off switch).
  evidence: The memlog still records "merge to main deploys poc" and "reviewer approval" as ADOPTED.
- source_plan: `_bmad-output/implementation-artifacts/plan-1-5-grafana.md`
  summary: Addressed (Story 1.5) the Grafana provisioning entry from plan-cicd-github-actions.md — provisioning now runs during `cdk deploy` as a custom resource (`GrafanaStack-<stage>`), so deploy.yml needs no extra step and the CI deploy role needs no Grafana permissions.
  evidence: infra/stacks/grafana_provision.py, infra/lambdas/grafana_provision/handler.py; AD-16 amended 2026-09-28.
- source_plan: `_bmad-output/implementation-artifacts/plan-1-5-grafana.md`
  summary: Revoke Grafana admins removed from ADMIN_USER_IDS on update (diff old vs new list in a small Lambda-backed custom resource).
  evidence: The AwsCustomResource update path only issues ADD; removal currently needs a manual `aws grafana update-permissions` REVOKE (documented).
- source_plan: `_bmad-output/implementation-artifacts/plan-1-5-grafana.md`
  summary: Test service-account lookup pagination in the Grafana provisioning handler with a paging fake.
  evidence: `_find_service_account` follows nextToken, but the fake never pages; fine at POC scale.
- source_plan: `_bmad-output/implementation-artifacts/plan-2-1-dq-refresh.md`
  summary: Test the BatchGet reader's skippedEntries (non-SUCCESS) and unknown-entryId error paths.
  evidence: The fake always returns an empty skippedEntries list; an inverted check would record a read failure as a dq_gap.
- source_plan: `_bmad-output/implementation-artifacts/plan-2-1-dq-refresh.md`
  summary: Addressed (Story 2.2) — typed PipeDetector/FacilityDetector protocols in detect/core/models.py; run() dispatches by kind.
  evidence: detect/core/models.py, detect/handler.py::_run_detector (plan-2-2-dq-flatline-stuck.md).
- source_plan: `_bmad-output/implementation-artifacts/plan-2-1-dq-refresh.md`
  summary: Story 4.4 alarms must include detection Lambda Errors and Scheduler TargetErrorCount/InvocationDroppedCount (no retries, no DLQ by design).
  evidence: A detector failing every minute would otherwise leave stale _dq values unnoticed.
- source_plan: `_bmad-output/implementation-artifacts/plan-2-3-dq-drift-gap.md`
  summary: Add an offset-from-peers data-quality check, so a sensor that drifted and then stopped (constant offset) stays flagged.
  evidence: dq_drift flags only an ongoing linear trend (R² gate); a completed drift clears while the calibration error remains.
- source_plan: `_bmad-output/implementation-artifacts/plan-2-4-dq-overview.md`
  summary: Show data age on the trust overview (a "last evaluated" indicator, or colour stale scores), so stopped detection doesn't leave tiles green; pair with Story 4.4 alarms.
  evidence: PropertyValue returns the latest value however old; a stopped detector leaves trust tiles green indefinitely.
- source_plan: `_bmad-output/implementation-artifacts/plan-2-4-dq-overview.md`
  summary: Have the owner check the overview's readability in a browser (three tiles per pipe, three pipes per row) and adjust the tile sizes if cramped.
  evidence: Layout verified structurally and via API only; no visual render check was possible overnight.
- source_plan: `_bmad-output/implementation-artifacts/plan-3-3-incidents.md`
  summary: Add a TTL or archive for closed incident history (C# items) and avoid reading a pipe's full history every run.
  evidence: Each run queries every pipe's full partition; cost grows with history (fine at POC scale).
