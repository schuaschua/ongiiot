---
stepsCompleted: ['step-01-validate-prerequisites', 'step-02-design-epics', 'step-03-create-stories', 'step-04-final-validation']
inputDocuments:
  - _bmad-output/specs/spec-og-iiot/SPEC.md
  - _bmad-output/specs/spec-og-iiot/stack.md
  - _bmad-output/specs/spec-og-iiot/roadmap.md
  - _bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md
  - _bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/.memlog.md
  - _bmad-output/planning-artifacts/briefs/brief-og-iiot-2026-09-28/brief.md
  - _bmad-output/implementation-artifacts/deferred-work.md
  - _bmad-output/implementation-artifacts/plan-facility-model-sitewise.md
  - _bmad-output/implementation-artifacts/plan-simulator.md
  - _bmad-output/implementation-artifacts/plan-cicd-github-actions.md
target: jira
---

# o&g iiot - Epic Breakdown

## Overview

This document breaks the o&g iiot 4-day POC into epics and stories. The requirements come from the spec (which stands in for a PRD), the architecture spine and the build plans. The stories will be pushed to Jira.

## Requirements Inventory

### Functional Requirements

- **FR1 (CAP-1):** Engineers see live sensor data for each selected pipe, from a simulated stream that behaves like the client's live AWS stream.
- **FR2 (CAP-2):** Every sensor is tied to its pipe, with a data contract (units, range, rate, owner) and pipe attributes (service, material, age).
- **FR3 (CAP-3):** Each sensor has a data-quality score covering drift, flatline, stuck values and gaps, and the score names any failing check.
- **FR4 (CAP-4):** The platform detects leak and methane-loss events in the stream.
- **FR5 (CAP-5):** Integrity engineers see which pipes behave differently from similar pipes (same service, material and age).
- **FR6 (CAP-6):** Every alert explains itself (confidence, contributing sensors). Related alerts on a pipe are grouped into one incident, and one click opens the raw trend.
- **FR7 (CAP-7):** Operators (consultants playing that role) receive an email when an incident opens. The email gives the pipe, the detectors, the confidence and a deep link.
- **FR8 (AD-15):** Demo operators can inject leaks and sensor faults on demand, cancel them, and reset the demo.
- **FR9 (AD-15):** A smoke test proves the whole chain works end to end: injection → incident → email → link.
- **FR10 (AD-16 amended):** Every deploy runs through GitHub Actions. PRs are tested; merging to `main` deploys `poc` after approval; developer stages deploy on demand.

### NonFunctional Requirements

- **NFR1:** The whole POC is built by 2–4 consultants in 4 days, with a demo at the end of day 4 to the client's operations and engineering leads.
- **NFR2:** AWS account `123456789012`, region `ap-southeast-1`. IoT SiteWise is used for ingestion and telemetry. Do not use Lookout for Equipment, Monitron or SiteWise anomaly detection.
- **NFR3:** Access to OT is read-only. Nothing is written back to OT.
- **NFR4:** All data is simulated. The facility is modelled by us. The ingestion path must accept the real stream later without a rebuild.
- **NFR5:** There is no black-box output. Every insight can be explained and traced to raw data.
- **NFR6:** Raw data is written once and never altered, with lineage to every derived value (MRV auditability).
- **NFR7:** The POC is demo-only. Alerts go only to an allowlist of consultants, and every email carries a "POC, simulated data" banner.
- **NFR8:** An injected leak reaches an email within 5 minutes.
- **NFR9:** No long-lived AWS keys are stored in GitHub; GitHub authenticates through OIDC.
- **NFR10:** We supply evidence for the client's own regulatory reporting. We are not the official record.

### Additional Requirements

- The facility model `facility.yaml` is the only authored source. It is parsed only by `facility/model.py` (AD-2, AD-14).
- Raw values enter only through `BatchPutAssetPropertyValue` by alias. The simulator checks at startup that every alias is associated with an asset (AD-1).
- Detectors are pure functions over fixed types and run in two passes: data-quality checks first, then leak and outlier checks (AD-5).
- There is at most one OPEN incident per pipe, enforced with conditional DynamoDB writes (AD-6). Email goes out through a `notified_at` outbox (AD-7). The deep link follows the literal Deep link v1 template (AD-8).
- Detection is single-flight: reserved concurrency 1, no retries, and the scheduled time is the run ID (AD-12). Derived properties follow v1 (AD-13).
- Demo operations cover a warm-up of at least 30 minutes, a reset, a smoke test, health alarms and a recorded fallback (AD-15).
- Stages are the shared `poc` plus `poc-<initials>` per developer. One Grafana workspace, provisioned through its API (AD-16).
- IAM gives each component its own role, and write boundaries are enforced for raw, derived, incidents and control data (AD-17).
- SiteWise is read through a single reader adapter, using ExecuteQuery with a BatchGet fallback (AD-18).
- The simulator runs on ECS Fargate, in a public-subnet VPC with no NAT.
- GitHub repo `schuaschua/ongiiot` (private). The GitHub `poc` environment requires an approval.
- Open questions to settle through spikes:
  - whether IAM can restrict SiteWise writes by alias prefix;
  - ExecuteQuery latency at full load;
  - whether IAM Identity Center can be used for Grafana login.

### UX Design Requirements

- **UX-DR1:** The `facility-overview` Grafana dashboard shows every pipe's `alert_state` and sensor data-quality scores at a glance.
- **UX-DR2:** The `pipe-detail` dashboard (uid `pipe-detail`) has variables `pipe_id`, `area` and multi-value `evidence`, plus a constant `alias_root`. It shows raw trends by alias and highlights the evidence sensors (AD-8).

### FR Coverage Map

| FR | Epic | Summary |
|----|------|---------|
| FR1 | Epic 1 | Live per-pipe view from the simulated stream |
| FR2 | Epic 1 | Sensor-to-pipe model and data contracts in SiteWise |
| FR3 | Epic 2 | Data-quality score per sensor, naming failing checks |
| FR4 | Epic 3 | Leak and methane-loss detection |
| FR5 | Epic 3 | Outliers among similar pipes |
| FR6 | Epic 3 | Explainable per-pipe incidents with one-click evidence |
| FR7 | Epic 3 | Incident email to the operator role |
| FR8 | Epic 4 | Inject, cancel, list and reset for the demo |
| FR9 | Epic 4 | End-to-end smoke test |
| FR10 | Epic 1 | GitHub Actions deploys with OIDC |

## Jira

Pushed 2026-09-28 to https://example.atlassian.net, project `OG`. Keep this table in step with Jira; do not re-create these issues.

| Item | Key | Status at push |
|------|-----|----------------|
| Epic 1 | OG-1 | In Progress |
| Epic 2 | OG-2 | Done |
| Epic 3 | OG-3 | Done |
| Epic 4 | OG-4 | In Progress |
| 1.1 | OG-5 | Done |
| 1.2 | OG-6 | Done |
| 1.3 | OG-7 | Done (merged in PR #1, `ee4936f`; poc not yet deployed) |
| 1.4 | OG-8 | Done (PR #2) |
| 1.5 | OG-9 | Done (PR #3) |
| 2.1 | OG-10 | Done (PR #4) |
| 2.2 | OG-11 | Done (PR #5) |
| 2.3 | OG-12 | Done (PR #6) |
| 2.4 | OG-13 | Done (PR #7) |
| 3.1 | OG-14 | Done (PR #8) |
| 3.2 | OG-15 | Done (PR #9) |
| 3.3 | OG-16 | Done (PR #10) |
| 3.4 | OG-17 | Done (PR #11) |
| 4.1 | OG-18 | Done (PR #12) |
| 4.2 | OG-19 | Done (PR #17) |
| 4.3 | OG-20 | Done (PR #18) |
| 4.4–4.5 | OG-21 – OG-22 | To Do |
| Task: trim tests, drop deploy test gate | OG-23 | Done (PR #14) |
| Task: incident alert state on facility overview | OG-24 | To Do |
| Task: operator email + leak simulation | OG-25 | Done (PR #15) |

## Epic List

### Epic 1: Live facility stream in AWS
Engineers open Grafana and watch all 24 modelled pipes stream live from SiteWise, with every sensor mapped to its pipe and contract. Every deploy runs through GitHub Actions.
**FRs covered:** FR1, FR2, FR10
**Status:** partly built. The facility model and SiteWise stack are done (f58358e); the simulator runtime is done (ce617b8).

### Epic 2: Trusted sensors: data-quality scoring
The OT and instrumentation team sees a live trust score for every sensor, with the failing check named (flatline, stuck, drift, gap). This lays down the detection run that later epics extend.
**FRs covered:** FR3

### Epic 3: Leak and outlier incidents with email
Integrity engineers and operators get one explainable incident per pipe when a leak or an outlier pipe is detected, receive an email, and open the evidence in one click.
**FRs covered:** FR4, FR5, FR6, FR7

### Epic 4: Demo-ready operations
Consultants can warm up, inject, list, cancel and reset scenarios. They can smoke-test the full chain, see health alarms, and fall back to a recording, so the day-4 demo runs reliably.
**FRs covered:** FR8, FR9

## Epic 1: Live facility stream in AWS

Engineers open Grafana and watch all 24 modelled pipes stream live from SiteWise, with every sensor mapped to its pipe and contract. Every deploy runs through GitHub Actions.

### Story 1.1: Model the facility in SiteWise

**Status: Done.** Commit f58358e, plan `plan-facility-model-sitewise.md`.

As an integrity engineer,
I want every modelled pipe to exist in SiteWise with its sensors, contracts and attributes,
So that every data point can be traced to a known pipe and sensor.

**Acceptance Criteria:**

**Given** `facility/facility.yaml` (24 pipes, 2 areas, 4 peer groups)
**When** it is loaded by `facility/model.py`
**Then** it validates against schema v1, and every invalid input raises `FacilityError`
**And** `cdk synth -c stage=<stage>` produces one Pipe asset per pipe and one Facility root asset, with raw, derived and attribute aliases per AD-13 and AD-14
**And** a custom resource seeds each pipe's attribute values, including contract overrides.

### Story 1.2: Generate a live simulated stream with fault and leak injection

**Status: Done.** Commit ce617b8, plan `plan-simulator.md`.

As a demo operator,
I want a realistic 1 Hz stream for every sensor and the ability to inject leaks and faults,
So that the platform has believable data and detection can be demonstrated.

**Acceptance Criteria:**

**Given** the shipped model
**When** the simulator runs for 60 s
**Then** all 144 raw aliases receive 60 GOOD values within their contracts, with strictly increasing timestamps
**And** a leak injected on P-007 shows a flow imbalance and a methane rise within 2 minutes, with no other pipe affected
**And** `ops/inject.py` creates and cancels injections, and each injection is logged to `injection_log` exactly once.

### Story 1.3: Deploy through GitHub Actions with OIDC

**Status: Done.** PR #1 (`ee4936f`), plan `plan-cicd-github-actions.md`. poc deploys by manual dispatch from `main` (no GitHub reviewers on Team plan).

As a consultant on the team,
I want every deploy to run through GitHub Actions without stored AWS keys,
So that deploys are repeatable, reviewed and safe for the shared demo stage.

**Acceptance Criteria:**

**Given** the one-time `GitHubOidcStack` is deployed to account 123456789012
**When** a PR targets `main`
**Then** tests, `cdk synth` and `cdk diff` against `poc` run, and nothing deploys
**And** a merge to `main` deploys `*-poc` only after approval in the GitHub `poc` environment
**And** a manual dispatch with `poc-<initials>` deploys that stage without approval, and any other stage name is refused before any AWS call
**And** the role trusts only `repo:schuaschua/ongiiot`, and no AWS secrets exist in GitHub.

### Story 1.4: Run the simulator on ECS Fargate

As a demo operator,
I want the simulator running continuously in AWS for each stage,
So that the stream is live without anyone's laptop being involved.

**Acceptance Criteria:**

**Given** stage `<stage>` is deployed through GitHub Actions
**When** `SimulatorStack-<stage>` is created
**Then** the `sim-control-<stage>` and `injection-log-<stage>` tables exist, and one Fargate task runs the image built from `uv.lock`
**And** the task role can write only raw aliases, describe time series, read `sim_control` and write `injection_log` (AD-17)
**And** within 2 minutes of deploy, SiteWise shows fresh values on every raw alias
**And** the first deploy settles whether SiteWise IAM can scope writes by alias prefix, with the result recorded against AD-17. The attribute seed write is confirmed at the same time.

### Story 1.5: Watch the live stream in Grafana

As an integrity engineer,
I want a Grafana view of any pipe's raw sensor trends,
So that I can see the live facility and later open alert evidence in one click.

**Acceptance Criteria:**

**Given** an Amazon Managed Grafana workspace owned by the `poc` stage, with the SiteWise plugin 2.6.3 and read-only access
**When** a consultant signs in
**Then** the `pipe-detail` dashboard (uid per AD-16) shows all 6 raw trends for the chosen `pipe_id`/`area`, and supports multi-value `evidence` highlighting (UX-DR2)
**And** `facility-overview` lists all pipes, with their latest values (UX-DR1, base layer)
**And** dashboards and the data source are provisioned by `infra/grafana_provision.py`, not by hand
**And** if IAM Identity Center cannot be enabled, the same dashboards run on self-hosted Grafana, and the result is recorded.

## Epic 2: Trusted sensors: data-quality scoring

The OT and instrumentation team sees a live trust score for every sensor, with the failing check named (flatline, stuck, drift, gap). This lays down the detection run that later epics extend.

### Story 2.1: Refresh sensor trust every minute

As an OT and instrumentation engineer,
I want every sensor's trust score recomputed every minute from the live window,
So that I always see current data quality.

**Acceptance Criteria:**

**Given** the stream is live (Epic 1)
**When** EventBridge Scheduler invokes the detection Lambda each minute with `scheduled_time`
**Then** the Lambda reads an aligned `FacilityWindow` through `detect/adapters/reader.py` (AD-9, AD-18) and writes `<measurement>_dq = 1.0` and an empty `_dq_fail` for every sensor, timestamped `window_end` (AD-13)
**And** runs are single-flight: reserved concurrency 1, no retries, `run_id = scheduled_time` (AD-12)
**And** the day-1 spike measures ExecuteQuery at full load (24 pipes × 6 sensors × 900 s), and switches to the BatchGet fallback if it cannot finish in 50 s. The result is recorded against AD-18
**And** the detection role cannot write raw aliases, `sim_control` or `injection_log` (AD-17).

### Story 2.2: Flag flatlined and stuck sensors

As an OT and instrumentation engineer,
I want flatlined and stuck sensors flagged with a lower trust score,
So that I stop trusting readings that no longer change.

**Acceptance Criteria:**

**Given** a `dq_flatline` or `dq_stuck` injection on one measurement
**When** the next two runs complete
**Then** that sensor's `_dq` falls below `tunables.dq_min`, and `_dq_fail` names `dq_flatline` or `dq_stuck`
**And** `dq_score.py` is the only place scores are computed: `dq = 1 − max(confidence)`
**And** the detectors are pure functions over the `models.py` types, with unit tests on synthetic windows (AD-5)
**And** other sensors keep `_dq = 1.0`.

### Story 2.3: Flag drifting and silent sensors

As an OT and instrumentation engineer,
I want drifting and silent sensors flagged,
So that slow calibration drift and dropped data are visible.

**Acceptance Criteria:**

**Given** a `dq_drift` injection
**When** the drift exceeds the detector's tunable threshold
**Then** `_dq_fail` names `dq_drift`
**And** given a `dq_gap` injection, an alias with no samples in the window scores `0.0` with `dq_gap`
**And** normal ingest lag alone never raises `dq_gap` (`ingest_lag_s`).

### Story 2.4: See trust scores on the facility overview

As an OT and instrumentation engineer,
I want the facility overview to show every sensor's trust score and failing checks,
So that I can spot bad sensors at a glance.

**Acceptance Criteria:**

**Given** the `facility-overview` dashboard
**When** a sensor's `_dq` is below `dq_min`
**Then** it is highlighted and shows its `_dq_fail` text (UX-DR1)
**And** clicking a pipe opens `pipe-detail` for that pipe.

## Epic 3: Leak and outlier incidents with email

Integrity engineers and operators get one explainable incident per pipe when a leak or an outlier pipe is detected, receive an email, and open the evidence in one click.

### Story 3.1: Detect leaks from mass balance and methane rise

As an integrity engineer,
I want leaks detected from the flow imbalance and a methane rise,
So that a loss of containment is caught within minutes.

**Acceptance Criteria:**

**Given** a `leak` injection on a pipe with the default rate
**When** two detection runs complete
**Then** `leak_mass_balance` and `leak_methane_rise` each return a `Finding` for that pipe, with a confidence, evidence aliases and a one-sentence explanation
**And** sensors with `dq < dq_min` are excluded, and the explanation says so (AD-5 pass 2)
**And** no finding is raised on any pipe without an injection over a 30-minute run.

### Story 3.2: Flag a pipe that behaves differently from its peers

As an integrity engineer,
I want a pipe flagged when it deviates from its peer group,
So that I notice problems no single threshold would catch.

**Acceptance Criteria:**

**Given** peer groups of at least 4 pipes from `facility.yaml`
**When** one pipe's behaviour deviates beyond the `outlier_peer` tunable
**Then** a facility-level `Finding` names that pipe, the comparison sensors and its peers in the explanation
**And** pipes are compared only within their `peer_group` (AD-14).

### Story 3.3: Group findings into one explainable incident per pipe

As an operator,
I want every finding on a pipe grouped into a single incident with its confidence and evidence,
So that one event never floods me with duplicate alerts.

**Acceptance Criteria:**

**Given** findings from several detectors on one pipe
**When** they arrive across runs
**Then** exactly one OPEN incident exists for that pipe (conditional write on `pk=<pipe_id>, sk=OPEN`), with `detectors[]`, the union of `evidence_aliases` and confidence = max (AD-6)
**And** the incident closes after `quiet_runs_to_close` runs in which its detectors ran successfully with no findings, and reopens within `reopen_grace_s` without a new ID
**And** `alert_state` and `alert_summary` are written every run for every pipe (AD-13)
**And** a replayed `run_id` makes no incident writes (AD-12).

### Story 3.4: Email operators with a one-click evidence link

As an operator (a consultant),
I want one email per incident with the confidence, the detectors and a link to the raw evidence,
So that I can act immediately and show the client the proof.

**Acceptance Criteria:**

**Given** an OPEN incident without `notified_at`
**When** the run ends
**Then** exactly one email in Email v1 format goes to the SNS operator topic, and `notified_at` is set conditionally (AD-7)
**And** only the `operator_emails` allowlist can subscribe, and every email carries the POC banner (NFR7)
**And** the deep link is built only by `deeplink.py` from the Deep link v1 template, and `test_deeplink_contract.py` checks it against `pipe-detail.json` (AD-8)
**And** an injected leak reaches an inbox within 5 minutes (NFR8).

## Epic 4: Demo-ready operations

Consultants can warm up, inject, list, cancel and reset scenarios. They can smoke-test the full chain, see health alarms, and fall back to a recording, so the day-4 demo runs reliably.

### Story 4.1: List running injections and record their true end

As a demo operator,
I want to list active and recent injections, and have cancels recorded as the real end,
So that I can stop any scenario without the original ID, and scoring uses true windows.

**Acceptance Criteria:**

**Given** injections in `sim_control`
**When** I run `ops/inject list`
**Then** active and recent injections show their ID, type, pipe, measurement and effective window
**And** when an injection is cancelled after it first applied, `injection_log` records the effective end (from deferred work).

### Story 4.2: Reset the demo between rehearsals

As a demo operator,
I want one command that clears incidents and injections,
So that every rehearsal starts clean.

**Acceptance Criteria:**

**Given** OPEN incidents and active injections
**When** I run `ops/reset.py`
**Then** detection closes all OPEN incidents with `closed_reason=reset`, and all `sim_control` items are deleted
**And** raw data and `injection_log` are untouched, and incidents closed by reset never reopen (AD-15).

### Story 4.3: Smoke-test the full chain before the demo

As a demo operator,
I want a single smoke test from injection to email to link,
So that I know the demo will work before the client arrives.

**Acceptance Criteria:**

**Given** a warmed-up stage
**When** I run `ops/smoke.py`
**Then** it injects a leak on a test pipe, waits for an OPEN incident with `notified_at`, and checks that the deep link resolves to `pipe-detail`
**And** it fails with a clear message if any step exceeds the 5-minute budget.

### Story 4.4: Alert the team when the platform is unhealthy

As a consultant on the team,
I want alarms when detection errors or the simulator stops,
So that problems are fixed before the demo.

**Acceptance Criteria:**

**Given** CloudWatch alarms for detection Lambda errors ≥ 1 in 5 minutes and simulator running tasks < 1
**When** either condition occurs
**Then** a notification goes to the `team-alarms-<stage>` topic, never to the operator topic (AD-15).

### Story 4.5: Prepare the demo runbook and fallback

As the demo lead,
I want a pre-demo checklist and a recorded fallback,
So that the day-4 demo succeeds even if AWS misbehaves.

**Acceptance Criteria:**

**Given** `docs/demo-runbook.md`
**When** the team prepares the demo
**Then** the checklist covers confirmed SNS subscriptions, 30+ minutes of warm-up, a passed smoke test and a reset run
**And** a screen recording of a full rehearsal (injection → incident → email → evidence) is stored for fallback.
