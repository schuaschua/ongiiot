---
title: 'Story 4.4: Alert the team when the platform is unhealthy'
type: 'feature'
ticket: 'OG-21'
created: '2026-09-29'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['edge-case-hunter', 'verification-gap']
review_loop_iteration: 0
baseline_revision: 'c09f706bafb06b10b3d699e8ee91a0cb339dfb70'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** If detection starts failing or the simulator task stops, nobody finds out until the demo breaks (Story 4.4, OG-21; AD-15 Health).

**Approach:** Add two CloudWatch alarms per stage: detection Lambda errors ≥ 1 in 5 minutes, and simulator running tasks < 1. Both notify a separate `team-alarms-<stage>` SNS topic, never the operator topic.

## Boundaries & Constraints

**Always:**
- The topic, the alarms and the subscriptions go in `DetectionStack-<stage>`. The simulator alarm uses the deterministic ECS names (`simulator-<stage>` cluster and service), so no new stack and no cross-stack reference are needed.
- Topic `team-alarms-<stage>`. Its email subscriptions come only from a new CDK context `team_emails` (comma-separated), parsed and validated exactly like `operator_emails` (reuse the parser, generalised to take the context name). `sns:Subscribe` is denied to every principal except the deploy role, the same policy statement as the operator topic.
- `cdk.json` sets `"team_emails": "operator@example.com"`: the only consultant address on record (owner said to proceed without them). It needs its own confirmation click.
- Alarm `detection-errors-<stage>`: `AWS/Lambda` `Errors`, `FunctionName=detection-<stage>`, Sum over 300 s ≥ 1, 1 of 1 datapoints, missing data = not breaching.
- Alarm `simulator-down-<stage>`: `AWS/ECS` `CPUUtilization` `SampleCount`, `ClusterName=simulator-<stage>`, `ServiceName=simulator-<stage>`, period 60 s, `< 1` for 3 of 3 datapoints, missing data = **breaching** (no running task means no datapoints). Three minutes rides over the ~1-minute gap of a simulator redeploy (stop-first).
- Both alarms send their ALARM and OK actions to the team topic only. The detection function role gets no new permissions.
- `-c simulator=off`: the simulator alarm's actions are disabled (`actions_enabled=False`), so a deliberate stop doesn't page the team. The errors alarm stays enabled.
- Outputs: `TeamAlarmsTopicArn`.

**Never:**
- No alarm action or subscription on `operators-<stage>`. No Container Insights. No dashboards or other alarms. No change to the Lambda or the simulator code.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Synth, simulator on | `-c stage=poc-dj` | 2 alarms with the exact metric, threshold, period and missing-data settings; both actions → team topic; topic policy denies Subscribe except the deploy role | none |
| Synth, simulator off | `-c simulator=off` | simulator alarm `ActionsEnabled: false`; errors alarm enabled | none |
| Emails | `team_emails` "a@x.io, A@x.io,b@y.io" | 2 subscriptions on the team topic, none added to the operator topic | a malformed address fails synth |
| Never operator | any | no alarm references `operators-<stage>` | none |
| Live, simulator down | `poc-dj` deployed, simulator stopped (`-c simulator=on` but the task is killed) | `simulator-down-poc-dj` goes to ALARM within about 3–4 minutes and the team topic gets a message | none |

</frozen-after-approval>

## Code Map

- `infra/stacks/detection.py:80-103` -- `EMAILS_CONTEXT`, `operator_emails()`: generalise to `emails(value, context_name)` and keep `operator_emails` behaviour. Lines 148-170: the operator topic and its deny-Subscribe statement; factor it into a helper used by both topics.
- `infra/stacks/detection.py:105,123` -- `schedule_state` / the `simulator` switch (`SWITCH_CONTEXT` from `infra/stacks/simulator.py:37`); reuse it for `actions_enabled`.
- `infra/stacks/detection.py:268-272` -- CfnOutputs; add `TeamAlarmsTopicArn`.
- `infra/stacks/simulator.py:158-170` -- service and cluster names (`simulator-<stage>`), for the dimensions.
- `tests/test_detection_stack.py` -- the template assertions style (exact resource properties); add the alarm, topic and switch tests there.
- `cdk.json` -- `team_emails`.
- `docs/ci-cd.md` -- a "Health alarms" section beside the operator email section: the recipients, confirming the subscription, what each alarm means, and the `simulator=off` behaviour.

## Tasks & Acceptance

**Execution:**
- [x] `infra/stacks/detection.py` -- team topic, `team_emails`, 2 alarms, output. -- the alarms.
- [ ] `cdk.json` -- `team_emails`. -- the recipient.
- [x] `tests/test_detection_stack.py` -- the matrix synth rows (at most about 4 tests; prefer parametrize). -- matrix.
- [x] `docs/ci-cd.md` -- the "Health alarms" section. -- runbook.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all pass (suite about 175 or fewer).
- Given `poc-dj` deployed from this branch with the subscription confirmed, when the simulator task is stopped by hand (`aws ecs stop-task`), then `simulator-down-poc-dj` goes to ALARM within about 4 minutes and returns to OK after the service restarts the task.

## Implementation Notes

- Plan approved by the orchestrating session on 2026-09-29. The owner had said to proceed without them ("just do whatever needs to be done without me"), so there was no human checkpoint. The owner can revisit `team_emails` and the 3-minute simulator window.

## Plan Change Log

- 2026-09-29 (build): the team topic also gets an `AllowCloudWatchAlarmsPublish` statement (`cloudwatch.amazonaws.com`, `sns:Publish`, `aws:SourceArn` like this account's `alarm:*`, `aws:SourceAccount`). An explicit topic policy replaces SNS's default one, so without it CloudWatch alarm actions could fail to publish. The deny-Subscribe statement is unchanged.
- 2026-09-29 (build): the `cdk.json` `team_emails` edit was blocked by the session's permission classifier; the code, tests and docs are done, but `cdk.json` still has no `team_emails` until the owner adds it.

## Review Triage Log

**Pass 1 (review_loop_iteration 0).** Lenses: edge-case-hunter, verification-gap, run in turn. Counts: high 0, medium 2, low 7, false 0, maybe-false 0. Outcome: 2 patches, 1 owner action, 1 live check pending, 5 rejected; no loopback.

| Id | Finding | Verdict | Route | Evidence / action |
|----|---------|---------|-------|-------------------|
| EC-1, VG-o1, VG-o2 | `team_emails` is missing from `cdk.json`, so the team topic has no subscribers | medium | owner action | The permission classifier blocked the agent's `cdk.json` edit (Plan Change Log). It must not be worked around; the owner adds it. Tracked on OG-21. |
| VG-2 | The alarm → SNS path (CloudWatch publish under the topic policy; ECS `SampleCount` < 1 with no task) is unproven live | medium | live check | Pre-verified. Run the plan's stop-task check on `poc-dj` before merging and record the result here. |
| VG-1 | The alarm's ECS dimensions are not cross-checked against the synthesized SimulatorStack names | low | patch | Pre-verified. Extend the existing alarm test to compare with the same app's `AWS::ECS::Cluster`/`Service` names (no new test). |
| EC-7 | The docs' stop-task snippet passes the literal `None` when no task runs | low | patch | Add a guard line to the snippet. |
| EC-2 | Re-enabling actions while the alarm is already in ALARM sends nothing if the task never starts | low | reject | A task that never starts fails the deploy through the ECS circuit breaker, which the operator sees directly. The fix needs extra state. |
| EC-3 | SimulatorStack and DetectionStack deployed with different `simulator` values | low | reject | `deploy.yml` deploys `*-<stage>` in one `cdk deploy` with one `-c simulator`. |
| EC-4 | A redeploy longer than 3 min pages the team | low | reject | The measured stop-first gap on `poc-dj` is about 1 min (02:22:58 → task RUNNING 02:23:56). A rollback that runs longer should page. |
| EC-5 | No alarm if the scheduler stops invoking the Lambda | low | reject | The frozen intent limits the story to the two AD-15 alarms ("No … other alarms"). |
| EC-6 | The first deploy sends OK notifications | low | reject | Cosmetic, and it confirms the subscription works. |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass.
- `npx --yes aws-cdk@2.1143.0 synth -c stage=poc-dj --quiet` and `... -c simulator=off --quiet` -- expected: exit 0.

**Manual checks:**
- Live on `poc-dj`: both alarms exist in OK or INSUFFICIENT_DATA state. Stop the simulator task: the alarm fires within about 4 minutes and the ECS service replaces the task. `aws cloudwatch describe-alarm-history` shows the ALARM and OK transitions.
