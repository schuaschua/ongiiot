---
review: currency / reality-check
target: ../ARCHITECTURE-SPINE.md
date: 2026-09-28
method: primary AWS / Grafana / PyPI / Docker Hub sources fetched on 2026-09-28; no training-data assertions
---

# Currency review: Architecture Spine, Pipe Integrity Streaming POC

## Verdict

**The spine passes with two items to act on.** Every named technology exists, is current and fits the POC as of 2026-09-28. Ingestion is far inside the quotas. Two things need action. First, the detection read is the one load path that nobody has measured, and ExecuteQuery has no published request-rate quota. Second, a 1-minute schedule with no concurrency guard can run two detection passes at the same time, which breaks AD-6.

## Already verified before this review (from `.memlog.md`)

These were not re-checked here: aws-cdk-lib 2.271.0; Lambda Python 3.14 (3.15 is preview only); Amazon Managed Grafana 12.4 with the SiteWise plugin; SiteWise Monitor closed to new customers; BatchPutAssetPropertyValue with aliases; the ExecuteQuery region exclusions.

## Findings

| # | Severity | Finding | Action |
| --- | --- | --- | --- |
| F1 | High | The detection read is the one path that could exceed a limit. Each run reads 50 pipes × 6 measurements × 900 s = **270,000 data points per minute**. ExecuteQuery returns at most 20,000 rows per page, so a run needs at least 14 sequential pages. The endpoints-and-quotas page lists no ExecuteQuery request-rate quota, and no latency figure is published. | Run a spike test on day 1 that measures ExecuteQuery page latency at full load. Make `BatchGetAssetPropertyValueHistory` the documented fallback for the reader adapter (sizing is under Q6). Keep this behind the adapter, as the Deferred section already allows. |
| F2 | Medium | EventBridge Scheduler supports `rate(1 minute)`, but it fires "with 60 second precision". If one detection run takes longer than about 60 s, or a run is delayed, two runs can overlap. Both could then open the same alert, which violates AD-6. | Set Lambda reserved concurrency to 1, or use a DynamoDB conditional write (`attribute_not_exists` on the open-alert key) in the alert manager. Set the flexible time window to `OFF`. |
| F3 | Low | The Lambda Python runtime bundles boto3. AWS says the bundled version "depends on the runtime version and your AWS Region" and recommends packaging the SDK yourself. The exact bundled version for python3.14 in eu-west-1 is not published. | Pin `boto3` in the Lambda bundle so that `iotsitewise.execute_query` and the batch APIs are guaranteed present. |
| F4 | Low | The Grafana deep link (AD-8) passes only `var-pipe_id` plus `from`/`to`, but the alias convention is `/lng1/<area>/<pipe_id>/<measurement>`. The dashboard cannot build the alias from `pipe_id` alone. | Pick one: add `var-area` to the fixed link format; have the dashboard query by asset (`externalId:` = pipe_id) instead of by alias; or match with SQL `LIKE '%/P-007/%'`. Record the choice in AD-8. |
| F5 | Info | On Grafana v9+ workspaces the SiteWise data source "might require you to install the appropriate plugin". The plugin is at v2.6.3 (released 2026-09-10). The AMG user-guide link to the plugin README points to an empty file (`src/README.md`, 0 bytes). | Add "install the SiteWise plugin" to the AMG setup step. Use grafana.com plugin docs as the reference. |

## Question-by-question evidence

### Q1. Is Python 3.14 supported on ECS Fargate base images and in the CDK Python bindings?

**Yes.**
- **Fargate** runs any OCI image; it has no Python-specific runtime. Docker Official Image `python:3.14-slim` was last updated 2026-09-25 and ships amd64 and arm64/v8. Python 3.14 is on 3.14.7, with EOL 2030-10-31. Python 3.15 is at rc2 (`3.15.0rc2-slim`, 2026-09-25) and is not GA. 3.14 is the right choice.
- **CDK Python bindings:** PyPI `aws-cdk-lib` 2.271.0 declares `requires_python >=3.10` and lists the classifiers 3.10–3.14.
- **CDK Lambda runtime constant:** `Runtime.PYTHON_3_14` exists in aws-cdk v2.271.0 `aws-lambda/lib/runtime.ts` (line 218). It supports inline code and SnapStart.
- Sources:
  - https://hub.docker.com/_/python (tags API: https://hub.docker.com/v2/repositories/library/python/tags/3.14-slim)
  - https://endoflife.date/python
  - https://pypi.org/project/aws-cdk-lib/2.271.0/
  - https://github.com/aws/aws-cdk/blob/v2.271.0/packages/aws-cdk-lib/aws-lambda/lib/runtime.ts

### Q2. Does the Lambda Python 3.14 runtime bundle boto3?

**Yes, with a caveat (F3).**
- Lambda docs: "Your code runs in an environment that includes the SDK for Python (Boto3)." They also say: "The version of the AWS SDK included in the Python runtime depends on the runtime version and your AWS Region." AWS recommends that you "always include the SDK modules your code uses … in your function's deployment package."
- `python3.14` runs on Amazon Linux 2023. It is deprecated on 2029-06-30.
- Sources:
  - https://docs.aws.amazon.com/lambda/latest/dg/lambda-python.html#python-sdk-included
  - https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html
  - https://aws.amazon.com/blogs/compute/python-3-14-runtime-now-available-in-aws-lambda/

### Q3. Does EventBridge Scheduler support a 1-minute rate?

**Yes (see F2 for overlap).**
- The syntax is `rate(value unit)`, and the valid units are minutes, hours and days. The docs state: "All schedule types on EventBridge Scheduler invoke their targets with 60 second precision."
- In CDK 2.271.0, `aws_scheduler.ScheduleExpression.rate(Duration.minutes(1))` delegates to `events.Schedule.rate`, which emits `rate(1 minute)`. The `aws-scheduler-targets` `LambdaInvoke` target exists in 2.271.0.
- Sources:
  - https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html
  - https://github.com/aws/aws-cdk/blob/v2.271.0/packages/aws-cdk-lib/aws-scheduler/lib/schedule-expression.ts

### Q4. Does the AMG SiteWise data source support read-only IAM and dashboard URL variables (`var-pipe_id`, `from`/`to`)?

**Yes (see F4 for the alias gap).**
- **Read-only IAM:** with service-managed permissions, AMG "attaches the AWS managed policy AWSIoTSiteWiseReadOnlyAccess". Policy v5 contains only `Describe*`, `Get*`, `List*`, `BatchGet*` and `iotsitewise:ExecuteQuery`, with no write actions. That satisfies AD-11. The policy includes `ExecuteQuery`, so the plugin's SQL modes work read-only.
- **Template variables:** the plugin supports Query, Custom, Data source, Constant and Text box variables. It interpolates `${var}` into the Asset, Property, **Property Alias** and SQL fields.
- **Query types:** property value, history, aggregates, interpolated values, and SQL (Builder SQL and Code SQL, both via `ExecuteQuery`).
- **URL variables and time range:** these are Grafana core features. "Grafana interprets query string parameters prefixed with `var-` as variables". The `from` and `to` parameters accept ms epoch or relative time.
- Sources:
  - https://docs.aws.amazon.com/grafana/latest/userguide/AMG-manage-permissions.html
  - https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSIoTSiteWiseReadOnlyAccess.html
  - https://docs.aws.amazon.com/grafana/latest/userguide/using-iotsitewise-in-AMG.html
  - https://github.com/grafana/iot-sitewise-datasource/blob/main/docs/sources/template-variables.md
  - https://github.com/grafana/iot-sitewise-datasource/blob/main/docs/sources/query-editor.md
  - https://github.com/grafana/iot-sitewise-datasource/releases (v2.6.3)
  - https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/create-dashboard-url-variables/

### Q5. Is eu-west-1 available for IoT SiteWise ExecuteQuery and Amazon Managed Grafana?

**Yes.**
- **SiteWise:** eu-west-1 has the `iotsitewise`, `api.`, `data.` and `monitor.` endpoints. The ExecuteQuery feature "is available in all Regions where AWS IoT SiteWise is available, except AWS GovCloud (US-West), Canada (Central), China (Beijing) and US East (Ohio)".
- **AMG:** `grafana.eu-west-1.amazonaws.com` is listed. The default quota is 5 workspaces per region.
- This review can clear the `[ASSUMPTION]` on the region's service support. The team still has to confirm eu-west-1 as the region.
- Sources:
  - https://docs.aws.amazon.com/general/latest/gr/iot-sitewise.html
  - https://docs.aws.amazon.com/iot-sitewise/latest/userguide/sql.html
  - https://docs.aws.amazon.com/general/latest/gr/grafana-service.html

### Q6. Do SiteWise quotas fit 50 pipes × 6 measurements at 1 Hz, with a detection read of 15-minute windows every minute?

**Ingestion fits with a wide margin. The read fits on paper, but its latency is unmeasured (F1).**

All figures are default quotas per region per account, from the SiteWise endpoints-and-quotas page. Load is 300 properties at 1 Hz.

**Ingestion**

| Quota | Limit | POC load | Headroom |
| --- | --- | --- | --- |
| Rate of data points ingested (TQV/s, adjustable) | 5,000 | 300 raw; about 5–6/s averaged when `_dq` and `alert_state` are written once a minute | about 16× |
| Request rate for BatchPutAssetPropertyValue (adjustable) | 1,000 req/s | 30 req/s (10 entries per request is the API max) | about 33× |
| BatchPut entries per property per second (not adjustable) | 10 | 1 | 10× |
| Data points per second per quality per property (not adjustable) | 10 | 1 | 10× |
| API shape | ≤10 entries per request, ≤10 values per entry | — | — |

**Detection read (per 1-minute run): 270,000 points**

| Path | Limit | Need per run | Assessment |
| --- | --- | --- | --- |
| ExecuteQuery | `maxResults` ≤ 20,000 per page. No request-rate quota is listed on the quotas page. The query statement can be up to 10,000 characters. | ≥ 14 sequential pages | Unknown latency, so spike-test it. Filter on `event_timestamp` and metadata so the scan stays small (query-optimize guide). |
| BatchGetAssetPropertyValueHistory | 16 entries per request; `maxResults` ≤ 20,000 or 4 MB per page; 200 req/s account; 30 per property per second; 100 MB/s retrieval | 19 requests (16 × 900 = 14,400 points each, one page) | Fits with large headroom and can run in parallel. This is the recommended fallback. |
| Metadata (DescribeAsset) | 150 req/s | 50 per run, or cached | Fits. Cache it per cold start. |

"Number of in-flight queries (30)" and "StartQuery (30/s)" are separate quotas. They look like the newer async query API, not ExecuteQuery, so they are not assumed to apply here.

Sources:
- https://docs.aws.amazon.com/general/latest/gr/iot-sitewise.html (quota table)
- https://docs.aws.amazon.com/iot-sitewise/latest/APIReference/API_BatchPutAssetPropertyValue.html
- https://docs.aws.amazon.com/iot-sitewise/latest/APIReference/API_PutAssetPropertyValueEntry.html
- https://docs.aws.amazon.com/iot-sitewise/latest/APIReference/API_ExecuteQuery.html
- https://docs.aws.amazon.com/iot-sitewise/latest/APIReference/API_BatchGetAssetPropertyValueHistory.html
- https://docs.aws.amazon.com/iot-sitewise/latest/userguide/query-optimize.html

## Remaining assertions not verified here (out of scope, still assumptions)

- IAM Identity Center can be enabled in the POC account for AMG login. This depends on the organization's policy; the spine already marks it `[ASSUMPTION]`.
- The actual boto3 version bundled in `python3.14` in eu-west-1. It can only be observed by deploying (F3 makes this moot).
