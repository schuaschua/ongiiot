---
review: adversarial
target: ../ARCHITECTURE-SPINE.md
reviewed: '2026-09-28'
method: 'For each hole, build two units one level down that each obey every AD to the letter, then show they still fail to fit together.'
---

# Adversarial Review: Architecture Spine, Pipe Integrity Streaming POC

## Verdict

**Not yet a safe build substrate.** The paradigm and the ownership rules (AD-3, AD-4, AD-6) are sound. But the spine names its shared data (facility.yaml, the `alerts` item, `sim_control`, `alert_state`, `_dq`, the Finding window, the deep link) without fixing any of their shapes. It also assumes that detection runs never overlap. I found 16 holes. Five of them are critical: two consultants working in parallel on day 1 would build parts that fail to fit together on day 3, and the failure would show up in the demo, as duplicate alerts, lost or duplicate emails, or dead deep links.

Severity key:
- **C:** breaks a capability's success criterion in the demo.
- **H:** causes integration rework.
- **M:** causes local confusion that a convention fixes.

## Holes

### H1 (C): Overlapping 1-minute runs create duplicate OPEN alerts and duplicate emails

- **Unit A:** the alert manager, following AD-6, looks up the OPEN alert for `(P-007, leak_mass_balance)`. If it finds none, it puts a new item with a fresh ULID and calls the notifier.
- **Unit B:** `infra/` wires EventBridge Scheduler at `rate(1 minute)` to the Lambda, with the default settings (async invocation, 2 Lambda retries, no concurrency limit).
- **Clash:** reading 15-minute windows for 300 streams through ExecuteQuery can take more than 60 s. The spine's own Deferred section admits this. EventBridge Scheduler also delivers at least once, and Lambda async retries replay a failed run. Two invocations then both read "no OPEN alert", both put an item, and both send an email. That produces two OPEN alerts for one event, which breaks CAP-6 ("no duplicate alerts") and AD-6. Each unit obeyed every AD. AD-6 states the invariant but specifies no mechanism to enforce it.
- **Fix (tighten AD-6, new AD-12 "Single-flight detection"):**
  - Set the detection Lambda's reserved concurrency to 1, set Scheduler retry attempts to 0, and set Lambda async max retry attempts to 0. The next scheduled run is the retry, which matches the Errors convention.
  - Enforce "one OPEN per pipe and detector" in DynamoDB, not in code. An *open-slot item* with key `pk = "<pipe_id>#<detector_id>"`, `sk = "OPEN"` is created with `ConditionExpression attribute_not_exists(pk)`. A `ConditionalCheckFailed` error means "already open → update instead". Closing is a `TransactWriteItems` call that deletes the slot and puts the history item (see H3).
  - Each run passes the Scheduler's `scheduled_time` (from the event payload, not `now()`) as the run id. Any write that carries an older run id than the one already stored is dropped.

### H2 (C): The alert commit and the email send are not atomic, so emails are lost or doubled

- **Unit A:** the alert manager writes the OPEN item, then calls `notifier.publish()` in the same process.
- **Unit B:** `infra/` (a reasonable reading of "the notifier publishes … only when an alert changes from nothing to OPEN") wires a DynamoDB Stream from `alerts` to a notifier Lambda that fires on INSERT with `status = OPEN`.
- **Clash:** both units obey AD-7. Together they send two emails per alert. The spine does not say how the notifier learns about the transition. Even with only Unit A, the design loses emails: if the DynamoDB put succeeds and SNS then fails, the next run sees the alert already OPEN. The alert never "changes from nothing to OPEN" again, so it never sends an email, which breaks CAP-7 ("every alert raised … reaches the operator").
- **Fix (tighten AD-7):**
  - The notifier is an in-process adapter that the handler calls after the alert manager commits. It is not stream-triggered, and no DynamoDB Stream exists on `alerts`.
  - Sending is driven by an outbox field, not by the transition. Every OPEN alert item has `notified_at` (null at creation). At the end of each run, the notifier publishes for every OPEN alert with `notified_at = null`, then sets `notified_at` with `ConditionExpression attribute_not_exists(notified_at)`.
  - This gives at-least-once delivery with a very small duplicate window, and it recovers from SNS failures on the next run.

### H3 (C): The `alerts` item shape and key schema are undefined

- **Unit A (alert manager):** `PK = alert_id` (ULID). Finding "the OPEN alert for a pipe and detector" then needs a Scan with a filter.
- **Unit B (infra):** `PK = pipe_id`, `SK = detector_id`. That fits "one per pipe and detector", but a new alert after a CLOSED one overwrites the history, and ULID alert IDs become pointless.
- **Clash:** the CDK table definition and the adapter's key expressions don't match. AD-8 says each alert "stores `evidence_aliases` and its evidence time window" but gives no field names, types or time format. AD-9 says alert timestamps are "ISO 8601" but gives no precision and no rule for `Z` versus an offset, so string sort order breaks.
- **Fix (new convention "Alert item v1"):**

| Field | Type | Rule |
| --- | --- | --- |
| `pk` | S | `<pipe_id>#<detector_id>` |
| `sk` | S | `OPEN` for the live slot; `<alert_id>` for closed history |
| `alert_id` | S | ULID, minted by the alert manager at open |
| `pipe_id`, `detector_id` | S | per the conventions |
| `status` | S | `OPEN` \| `CLOSED` |
| `opened_at`, `last_seen`, `closed_at` | S | UTC `YYYY-MM-DDTHH:MM:SS.sssZ` (fixed width, always `Z`) |
| `evidence_from`, `evidence_to` | S | same format; `evidence_from` = the first finding's `window_start` (never moves); `evidence_to` = max `window_end` seen |
| `evidence_aliases` | L of S | union over the alert's lifetime, sorted |
| `confidence` | N | the latest finding's confidence (not max); stored unrounded |
| `explanation` | S | the latest finding's sentence |
| `notified_at` | S \| absent | see H2 |
| `last_run` | S | the scheduled_time of the last run that touched it (see H1) |

- Also state that "update evidence" means *union aliases and widen the window*. Without that, one consultant replaces the aliases and another appends to them.

### H4 (C): The Grafana deep link cannot be built as specified

- **Unit A (notifier):** `https://<ws>.grafana-workspace.eu-west-1.amazonaws.com/d/pipe/pipe?var-pipe_id=P-007&from=2026-09-28T10:00:00Z&to=…`
- **Unit B (dashboards):** the dashboard has uid `pipe-detail`, and its variable `pipe` is a SiteWise **asset** picker, because that is how the SiteWise Grafana data source selects assets. The variable's values are asset IDs, and `from`/`to` are read as epoch ms or relative (`now-15m`).
- **Clash:** AD-8 fixes the ingredients but not the format. Specific failures:
  1. The dashboard UID, slug and base URL are not specified.
  2. `var-` naming and whether the variable holds a pipe ID or a SiteWise asset ID are not specified.
  3. The time encoding is not specified. Alerts use ISO strings, while Grafana wants epoch ms.
  4. AD-8 says the link is built "only from" fields including `evidence_aliases`, but the link carries no aliases. The operator lands on all six trends with the contributing sensors not highlighted, which weakens CAP-6's "sensors that contributed".
  5. Aliases contain `<area>`, but the link carries only `pipe_id`. A dashboard that builds alias queries from `pipe_id` cannot resolve the area.
  6. No padding is specified. A 1-minute evidence window shows almost nothing.
- **Fix (tighten AD-8 with a literal format):**
  ```
  ${GRAFANA_URL}/d/pipe-detail/pipe-detail?orgId=1
    &var-pipe_id=<pipe_id>
    &var-area=<area>
    &var-evidence=<alias>&var-evidence=<alias>...
    &from=<epoch_ms(evidence_from) - 300000>
    &to=<epoch_ms(evidence_to) + 300000>
  ```
  - `GRAFANA_URL` is a CDK env var.
  - The dashboard JSON must declare uid `pipe-detail`, a text/custom variable `pipe_id` (the value is `P-xxx`, not an asset ID), `area`, and a multi-value `evidence` variable used to highlight or filter panels.
  - One function, `detect/adapters/deeplink.py::build_link(alert)`, is the only builder.
  - A test in `tests/` loads `dashboards/pipe-detail.json` and asserts the uid and the variable names. This is the "shared" in "shared by the notifier and the dashboards", and without it the word means nothing.

### H5 (C): The `alert_state` and `_dq` property formats are undefined, and Grafana cannot see alerts or failing checks at all

- **Unit A (alert manager):** mirrors open alerts to `alert_state` as a STRING, for example `"leak_mass_balance,dq_drift"`, and writes only when something changes.
- **Unit B (dashboards):** the facility overview uses a state-timeline or threshold panel on `alert_state` as INTEGER 0/1, and uses "last value in range".
- **Clash:** the SiteWise property data type is fixed in the asset model that `infra/` generates, so one of the units fails at `BatchPut` time (a type mismatch) or at render time. More fundamentally, Grafana reads only SiteWise (the Paradigm), so confidence, explanation and failing-check names never reach the dashboard. CAP-3 requires that each sensor "shows a score **and names any failing check**", and a numeric `_dq` cannot name a check. Similarly, if `alert_state` is written only on change and the dashboard range excludes the last change, the pipe shows no state.
- **Fix (new convention "Derived properties v1", tightening AD-3):**
  - `alert_state`: INTEGER, the count of OPEN alerts on the pipe. It is written **every run for every pipe**, at timestamp `scheduled_time`.
  - `alert_summary`: STRING (≤ 1024 bytes, the SiteWise limit), a JSON array `[{"detector_id","confidence","alert_id"}]`, written every run.
  - `<measurement>_dq`: DOUBLE in [0,1], where 1 is fully trustworthy. It is written every run at `scheduled_time`.
  - `<measurement>_dq_fail`: STRING, a comma-separated list of failing `dq_*` detector IDs, or empty.
  - Derived aliases are `/lng1/<area>/<pipe_id>/derived/<name>`. The `derived/` segment keeps them out of the raw namespace, so an IAM policy can deny the simulator role `derived/*` and deny the detection role everything except `derived/*`. That makes AD-3 enforceable instead of honour-system.
  - All derived properties are declared in `facility.yaml`'s generator, not hand-added.

### H6 (H): The `_dq` score has no owner and no derivation

- **Unit A (`dq_flatline` author):** emits a Finding with `confidence = 0.9`, meaning "90% sure it's flatlined".
- **Unit B (handler/SiteWise writer):** sets `pressure_in_dq = confidence` of the dq finding, and 1.0 when there is no finding.
- **Clash:** the score is now 0.9 for a sensor that is almost certainly broken, which inverts its meaning. Another author reading the same rule might emit `confidence` as the quality score. AD-5's Finding has no score field, and nothing says which module turns Findings into `_dq`.
- **Fix:** add `detect/core/dq_score.py::score(findings_for_sensor) -> (float, list[detector_id])` as the single owner, with `dq = 1 - max(confidence of dq_* findings naming that alias)`. The handler calls it, and detectors never compute scores.

### H7 (H): The detector call granularity and the `window`/`metadata` types are undefined

- **Unit A (`leak_mass_balance`):** written as per-pipe: `window` is `dict[measurement, list[(ts, value)]]` for one pipe, and `metadata` is that pipe's attributes.
- **Unit B (handler):** calls every detector once per run with the whole facility, because `outlier_peer` needs all pipes. `window` is `dict[alias, pandas.Series]`.
- **Clash:** both match `detect(window, metadata) -> list[Finding]` exactly and still cannot be plugged together. Other open questions in the same area:
  - Does the window include SiteWise quality flags (GOOD/BAD/UNCERTAIN)? `dq_gap` needs to know.
  - Are Finding `window_start`/`window_end` epoch nanoseconds (the SiteWise format) or ISO (the alert format)? AD-9 covers both ends of the pipe but not the middle.
- **Fix (tighten AD-5):** define `detect/core/models.py` with the following types, which every detector imports:
  - `FacilityWindow { start, end: datetime(UTC, tz-aware); series: dict[alias, list[Sample(ts: datetime, value: float|None, quality: str)]] }`
  - `FacilityMetadata { pipes: dict[pipe_id, PipeMeta(area, service, material, age_years, peer_group, sensors: dict[measurement, Contract])] }`
  - `Finding`

  Every detector is called once per run with the whole facility and filters to what it needs. Finding times are tz-aware `datetime`, and conversion to epoch or ISO happens only in adapters.

### H8 (H): Closing on the quiet period collides with AD-5's failure isolation

- **Unit A (handler):** per AD-5, when `leak_mass_balance` raises, the handler logs the exception and passes the other detectors' findings to the alert manager.
- **Unit B (alert manager):** per AD-6, any OPEN alert with no finding for longer than the quiet period is CLOSED.
- **Clash:** a detector that crashes for a few runs looks exactly like a quiet detector. The live leak alert closes. When the detector recovers, a new ULID is minted and a second email goes out, which breaks CAP-6 ("no duplicate alerts for the same event"). The same happens when the SiteWise reader returns a partial window.
- **Fix (tighten AD-6):** the handler passes the alert manager `ran_ok: set[detector_id]` and `covered_pipes`. The quiet-period clock advances only for `(pipe, detector)` pairs where the detector ran successfully and the pipe's data was present. Also state the reopen rule: a finding within N minutes of a CLOSE reopens the same alert (same `alert_id`, no new email) instead of creating a new one. This damps flapping near the threshold.

### H9 (H): CAP-6 "groups related alerts on the same pipe into one" contradicts AD-6's per-detector key

- **Unit A (`leak_mass_balance`):** fires on P-007 during the injected leak.
- **Unit B (`outlier_peer` and `dq_drift`):** also fire on P-007, because a real leak makes the pipe look like an outlier and pulls pressure off trend.
- **Clash:** AD-6 keys on `(pipe, detector)`, so the demo leak produces three alerts and three emails for one event. The spec's success test says "no pipe shows duplicate alerts for the same event".
- **Fix:** decide it explicitly. The recommended option is to key the incident on `pipe_id`. There is one OPEN *incident* per pipe, holding `detectors[]` and per-detector confidence, `evidence_aliases` is the union, and one email goes out per incident. The alternative is to keep per-detector alerts but add `incident_id` (grouping by pipe while any alert is open) and send email per incident. Either way, record it as an AD. Right now each unit can pick one and both comply.

### H10 (H): The facility.yaml schema has no owner and no parser

- **Unit A (`infra/` generator):** reads `pipes: [{id, area, attributes:{service, material, age}, sensors:[{measurement, alias, unit, range:[min,max], rate_hz, owner}]}]` and uses the explicit `alias`.
- **Unit B (`sim/`):** reads `pipes: {P-007: {area, service, material, install_year, sensors: {flow_in: {...}}}}` and *derives* the alias from the convention string.
- **Clash:** both read "the single authored model", but they parse two different mental schemas. A pipe whose authored alias deviates from the convention gets data that `infra/` never mapped. Per the memlog, unassociated aliases then create disassociated data streams, and the account setting that allows them is irreversible.
- **Fix (tighten AD-2):**
  - Add `facility/model.py`: a pydantic model plus `load() -> Facility` as the only parser, imported by `sim/` and `infra/`. Update the dependency graph to allow `sim → facility/` and `infra → facility/`, both as imports.
  - The alias is *derived* by `Facility.alias(pipe, measurement)` and never authored.
  - Add explicit `peer_group` (see H12) and `age_years` fields.
  - Add a CI/pytest check that validates the file.

### H11 (H): Tunables are "in facility.yaml", but detectors must not have "their own copy"

- **Unit A (detection Lambda):** bundles `facility.yaml` in the Lambda asset and reads thresholds and the quiet period from it. The Config convention says tunables live there.
- **Unit B (a reviewer applying AD-2):** rejects this, because AD-2 says detectors read metadata from SiteWise "never from their own copy". Unit B puts thresholds into SiteWise attributes instead, and the alert manager's quiet period ends up hardcoded.
- **Clash:** thresholds come from two different places, and the "same value everywhere" quiet period is not the same everywhere.
- **Fix:** split the model explicitly.
  - *Facility metadata* (pipes, attributes, contracts) is read at runtime from SiteWise attributes. The attribute names follow a convention: `service`, `material`, `age_years`, `peer_group` on the pipe; `<measurement>_unit`, `<measurement>_min`, `<measurement>_max`, `<measurement>_rate_hz`, `<measurement>_owner`.
  - *Tunables* are rendered by CDK from `facility.yaml` into one `TUNABLES_JSON` environment variable on the Lambda. That is the only runtime source, and detectors receive tunables as a third argument or inside `metadata`, never by reading the environment themselves (AD-5 purity).

### H12 (M): The peer-group definition for CAP-5 is unowned

- **Unit A (facility author):** gives pipes `age: 12`, `age: 14` and `age: 15`, and expects them to be peers.
- **Unit B (`outlier_peer`):** groups by the exact tuple `(service, material, age)`, so every group has one member and nothing is ever flagged.
- **Fix:** add an explicit `peer_group` attribute in `facility.yaml`, validated to have at least 3 members. `outlier_peer` groups only by `peer_group`.

### H13 (H): The `sim_control` item shape and its mutation path are undefined

- **Unit A (injection CLI in `sim/`):** writes a single item `{pk: "control", injections: [{type:"leak", pipe:"P-007", rate:…}]}` and overwrites the list on each command.
- **Unit B (simulator loop):** reads one item per injection (`pk = injection_id`) and, after applying one, **deletes it** to mark it consumed.
- **Clash:** the shapes don't match. Even with a shared shape, a CLI overwrite racing a simulator delete loses an injection, a lost update in the middle of the demo. AD-9 only says "a control item that only `sim/` reads". It says nothing about who writes, how items are removed, or how duration is expressed. AD-4 is also honour-system: nothing stops the detection role from reading `sim_control`, which *is* ground truth.
- **Fix (new convention "sim_control v1"):**
  - There is one item per injection: `pk = injection_id` (ULID), `type` ∈ {`leak`, `dq_flatline`, `dq_stuck`, `dq_drift`, `dq_gap`}, `pipe_id`, `measurement?`, `params{}`, `start_at`, `end_at` (UTC ISO). Items are immutable once written.
  - The CLI is the only writer, and "stop" means writing a new item with an earlier `end_at` (cancel by `supersedes`). The simulator never writes the table.
  - The simulator polls every ≤ 5 s and applies any item where `start_at ≤ now < end_at`. It appends to its injection log on first apply.
  - IAM gives the detection role no access to `sim_control` or to the injection log, which makes AD-4 enforceable.

### H14 (H): Evaluation windows race with ingestion latency

- **Unit A (SiteWise reader):** queries `[now − 15 min, now]`.
- **Unit B (`dq_gap`):** flags any sensor missing more than N seconds of samples in the window.
- **Clash:** the most recent few seconds are not yet queryable through ExecuteQuery, so every sensor looks like it has a gap at the tail on every run. That produces a permanent `dq_gap` alert on all 300 sensors. Using `now()` also means two overlapping runs (H1) evaluate unaligned windows and write `_dq` at out-of-order timestamps.
- **Fix (tighten AD-9):** `window_end = floor_minute(scheduled_time) − ingest_lag` (a tunable, default 60 s), and `window_start = window_end − window_len` (a tunable, default 15 min). All derived writes are timestamped `window_end`. Derived properties are then idempotent: the same run replayed writes the same TQV at the same timestamp, which SiteWise overwrites.

### H15 (M): The raw-then-asset deployment order can permanently mis-route aliases

- **Unit A (`infra/`):** deploys the Fargate simulator service and the SiteWise asset stack as independent stacks.
- **Unit B (`sim/`):** starts publishing by alias immediately.
- **Clash:** if the simulator publishes before the assets and their aliases exist, SiteWise either rejects the data or, if disassociated streams have been enabled, creates orphan streams. The team then has to re-associate them, and the account setting cannot be undone.
- **Fix (add to AD-10):**
  - The simulator stack depends on the asset stack.
  - At startup the simulator calls `DescribeTimeSeries` for each alias and refuses to start if any alias is unassociated.
  - The disassociated-streams mode is never enabled in the POC account.
  - The simulator image is a CDK asset built from the same commit as the asset models, so a change to `facility.yaml` redeploys both.

### H16 (M): Units and mass balance

- **Unit A (facility author):** declares `flow_in`/`flow_out` in m³/s (SI, volumetric).
- **Unit B (`leak_mass_balance`):** assumes mass flow in kg/s, as its name suggests.
- **Clash:** at different pressures and temperatures, a volumetric imbalance appears with no leak at all. The spine also says "SI", but `methane_ppm` is not an SI unit.
- **Fix:** give each measurement a fixed unit in the conventions table (`flow_*` kg/s mass flow, `pressure_*` Pa (absolute), `temperature` K, `methane_ppm` ppm(v), explicitly excepted from SI). `facility.yaml` validation rejects any other unit.

## Proposed spine changes (summary)

| Change | Closes |
| --- | --- |
| **New AD-12 Single-flight detection:** reserved concurrency 1, no Scheduler/Lambda retries, the run id is `scheduled_time`, and the OPEN slot is enforced by a DynamoDB conditional write | H1, H14 |
| **Tighten AD-6:** Alert item v1 schema; evidence = union aliases plus a widening window; close only for detectors that ran and pipes that had data; a reopen grace window | H3, H8 |
| **Decide grouping (new AD or tighten AD-6):** one open incident per pipe, or `incident_id` plus email per incident | H9 |
| **Tighten AD-7:** in-process notifier, no DynamoDB Stream, outbox via `notified_at` with a conditional set | H2 |
| **Tighten AD-8:** literal link template, dashboard uid `pipe-detail`, variables `pipe_id`/`area`/`evidence`, epoch ms ±5 min padding, a single builder, and a contract test on the dashboard JSON | H4 |
| **Tighten AD-3:** Derived properties v1 (types, the `derived/` alias segment, write-every-run, `_dq_fail`, `alert_summary`), enforced by IAM on alias prefixes | H5 |
| **Tighten AD-5:** `models.py` with FacilityWindow, FacilityMetadata and Finding; whole-facility call; tz-aware datetimes in the core; `dq_score.py` as the single score owner | H6, H7 |
| **Tighten AD-2:** `facility/model.py` as the only parser, derived aliases, `peer_group`, a SiteWise attribute-name convention, and tunables delivered as `TUNABLES_JSON` | H10, H11, H12 |
| **Tighten AD-9 / new convention sim_control v1:** immutable per-injection items, a single writer, no detection-role access | H13 |
| **Tighten AD-9:** aligned windows with `ingest_lag` | H14 |
| **Tighten AD-10:** stack ordering, alias association check, no disassociated streams | H15 |
| **Conventions table:** a fixed unit per measurement | H16 |

## What held up

- AD-1 (ingestion by alias) and AD-11 (read-only) have no exploitable seam.
- AD-4 is correct in intent. It needs only IAM enforcement (H13) to stop being honour-system.
- The dependency rules between `sim/`, `detect/core/` and `detect/adapters/` are clean. The only missing edges are the imports of the shared `facility/model.py` parser (H10).
