# Demo runbook: pre-demo checklist and fallback

For the day-4 demo on the shared `poc` stage (Story 4.5, AD-15). Every command below has been run against `poc` or `poc-dj`. Run the commands from the repo root with AWS credentials for account 123456789012 in `ap-southeast-1` (for example `export AWS_PROFILE=ongiiot-poc AWS_REGION=ap-southeast-1`).

The demo shows one chain: **inject a leak → an incident opens → the operator email arrives → its link opens the evidence in Grafana**. On `poc`, the email arrived 2–3 minutes after the injection (smoke test on 2026-09-29: 162 s).

> **Teardown, 2026-09-29.** All stacks except Grafana were torn down. Before a rehearsal or the demo, redeploy `poc` (the first checklist row) and wait 30+ minutes of warm-up. On that first deploy SNS sends a new "Subscription Confirmation" email to every address: confirm it again (see [Subscriptions](#subscriptions)). If the deploy fails because a retained `poc` table already exists, see `docs/ci-cd.md` section 5.

## Checklist

Tick every line on the day. The demo is ready only when all are ticked.

| When | Check | How | Pass |
|------|-------|-----|------|
| Day before | `poc` runs the latest `main` | `gh workflow run deploy.yml --ref main -f stage=poc -f simulator=on`, then `gh run watch <id>` | the run succeeds |
| Day before | Operator email subscriptions confirmed | [Subscriptions](#subscriptions) | every address shows a subscription ARN, not `PendingConfirmation` |
| Day before | Team alarm subscriptions confirmed | [Subscriptions](#subscriptions) (`team_emails` in `cdk.json`) | as above; if `team_emails` is empty, nobody hears about a failing platform, so decide who watches the alarms |
| Day before | Fallback recording exists and plays | [Fallback](#fallback) | the file opens from the demo laptop, offline |
| T−60 min | Simulator running | `aws ecs describe-services --cluster simulator-poc --services simulator-poc --query 'services[0].[runningCount,desiredCount]' --output text` | `1 1`. If `0`, redeploy with `-f simulator=on` and restart the warm-up clock |
| T−35 min | Warm-up of 30+ minutes elapsed | [Warm-up](#warm-up) | the task started 30+ minutes ago, and the latest detection runs show `"source_outage": false` |
| T−30 min | Smoke test passed | `uv run python -m ops.smoke --stage poc --table sim-control-poc` | last line `PASS …` within 5 minutes; one email arrives |
| T−25 min | Reset run | `uv run python -m ops.reset --stage poc --table sim-control-poc --yes` | `closed 1 incidents, deleted N injections` |
| T−20 min | No stray incident after the reset | wait 5 minutes, then [Open incidents](#open-incidents) | no OPEN incident; if one appeared, run the reset again and wait another 5 minutes |
| T−10 min | Health alarms OK | `aws cloudwatch describe-alarms --alarm-names detection-errors-poc simulator-down-poc --query 'MetricAlarms[].[AlarmName,StateValue]' --output text` | both `OK`. The alarms reach nobody until `team_emails` is set in `cdk.json` (it is not yet), so this check is the only way to see them |
| T−8 min | Rehearsal emails cleared | in the operator inbox, archive or delete every `[POC] Pipe … incident …` email (the smoke test and any stray incident each sent one) | no `[POC]` incident email left, so step 4 of the script opens the demo's email |
| T−5 min | Screens ready | Grafana `facility-overview` open and signed in; mail client open on the operator inbox; this runbook and the fallback recording open | all visible |

## Commands

### Subscriptions

```bash
for t in OperatorTopicArn TeamAlarmsTopicArn; do
  arn=$(aws cloudformation describe-stacks --stack-name DetectionStack-poc \
    --query "Stacks[0].Outputs[?OutputKey=='$t'].OutputValue" --output text)
  echo "$t"; aws sns list-subscriptions-by-topic --topic-arn "$arn" \
    --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output text
done
```

A new address gets an "AWS Notification - Subscription Confirmation" email after the deploy that adds it. Nothing is delivered until someone clicks **Confirm subscription**. `poc` and each `poc-<initials>` stage need their own confirmation.

### Warm-up

```bash
aws ecs describe-tasks --cluster simulator-poc --tasks "$(aws ecs list-tasks --cluster simulator-poc \
  --service-name simulator-poc --desired-status RUNNING --query 'taskArns[0]' --output text)" \
  --query 'tasks[0].startedAt' --output text
aws logs tail /aws/lambda/detection-poc --since 5m --format short | cut -d' ' -f2- \
  | jq -Rc 'fromjson? | select(.incident_ops) | {run_id, source_outage}'
```

Detectors need at least two windows of history (`window_len_s` is 15 minutes), so the simulator must have run for at least 30 minutes. A redeploy that changes the simulator image restarts the task and the clock.

### Open incidents

```bash
for p in $(seq -f "P-%03g" 1 24); do
  aws dynamodb get-item --table-name incidents-poc --key "{\"pk\":{\"S\":\"$p\"},\"sk\":{\"S\":\"OPEN\"}}" \
    --query 'Item.incident_id.S' --output text | grep -v None | sed "s/^/$p /"
done
```

No output means no OPEN incident.

## Demo script

1. Show `facility-overview` in Grafana: 24 pipes streaming, every sensor's data-quality score at 1.0.
2. Inject the leak on P-007 (the calibrated demo pipe) for 20 minutes:
   ```bash
   STAGE=poc SIM_CONTROL_TABLE=sim-control-poc uv run python -m ops.inject leak --pipe P-007 --minutes 20
   ```
3. While waiting (2–3 minutes), explain the detectors: mass balance (flow out below flow in), methane rise against peers, and the peer outlier.
4. When the email `[POC] Pipe P-007 incident …` arrives, open it and click the evidence link. It opens `pipe-detail` on P-007 with the evidence sensors and the time range already set.
5. After the demo, reset: `uv run python -m ops.reset --stage poc --table sim-control-poc --yes`, wait 5 minutes, and reset again if an incident appeared.

## Fallback

**Switch to the recording** if any of these happens during the demo:
- no email within 5 minutes of the injection;
- `ops.inject` or Grafana returns an AWS error;
- a health alarm fires.

Say that you're switching to a recording of the same run made on the rehearsal day. Don't debug live.

**The recording** is a person's job and can't be scripted. Record one full rehearsal on `poc`, following the demo script above: the overview, the injection command, the email arriving, the link opening `pipe-detail`, then the reset. On a Mac, use Cmd+Shift+5 and record the whole screen with the mail client and Grafana side by side. Keep it under 5 minutes.

Store it outside git; screen recordings are too large for the repo. Record where it is here:

| Recording | Location | Recorded on | By |
|-----------|----------|-------------|----|
| Full rehearsal, P-007 leak | _to be filled in_ | _date_ | _name_ |

## After the demo

Stop the simulator and the detection schedule, so the stage costs nearly nothing:

```bash
gh workflow run deploy.yml --ref main -f stage=poc -f simulator=off
```

The tables and the SiteWise history stay. With `simulator=off`, `simulator-down-poc` does not notify anyone.
