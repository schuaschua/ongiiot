---
title: 'CI/CD: GitHub Actions with AWS OIDC'
type: 'chore'
ticket: ''
created: '2026-09-28'
status: 'built'
route: 'full'
route_source: 'auto'
review: 'thorough'
review_source: 'auto'
lenses_ran: ['blind-hunter', 'edge-case-hunter', 'verification-gap', 'intent-alignment']
review_loop_iteration: 1
baseline_revision: 'ce617b86fee35d843727bcd54964506626fd44c6'
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Deploys are manual, from one laptop. The human wants every deploy to run through GitHub Actions for the private repo `schuaschua/ongiiot` into AWS account `123456789012`, region `ap-southeast-1`, with no long-lived AWS keys in GitHub.

**Approach:**
- A one-time CDK `GitHubOidcStack` creates the GitHub OIDC provider and a deploy role trusted only by this repo.
- A PR workflow runs the tests, `cdk synth` and `cdk diff`.
- A deploy workflow deploys `poc` on push to `main`, gated by a GitHub environment approval, and deploys `poc-<initials>` stages through manual `workflow_dispatch`.

## Boundaries & Constraints

**Always:**
- `aud` must be `sts.amazonaws.com`. Decision (review pass 1, human): trust is scoped by environment, not repo-wide. The deploy role trusts only `sub` `repo:schuaschua/ongiiot:environment:poc` and `repo:schuaschua/ongiiot:environment:dev`. A separate diff role trusts only `repo:schuaschua/ongiiot:pull_request`.
- The deploy role's permissions only let it assume the CDK bootstrap roles (`cdk-hnb659fds-*-123456789012-ap-southeast-1`) and read the bootstrap version parameter. The diff role may assume only the CDK lookup role (`cdk-hnb659fds-lookup-role-123456789012-ap-southeast-1`) and read the bootstrap version parameter. Neither gets direct resource permissions.
- Decision (review pass 1, human): deploy.yml runs the test suite in its own job, and every deploy (`poc` and `dev`) needs it to pass before approval is requested or AWS is touched.
- Decision (post-build, human, 2026-09-28): GitHub Team cannot require reviewers on private-repo environments (API 422). `poc` therefore deploys only by manual `workflow_dispatch` from `main`; starting the run is the approval. Nothing deploys on push. This replaces "deploys `poc` on push to `main`, gated by a GitHub environment approval" in the Approach.
- Docs state the residual risk plainly: all stages share one account and one set of bootstrap roles, so a run holding `dev` credentials can technically reach `poc`. The `poc` approval is enforced for PRs and branch runs without an environment, not against an edited workflow on the `dev` path.
- The `poc` stage deploys only from `main`, through the GitHub environment `poc`. Developer stages must match `poc-<initials>` (the `facility/model.py` stage rule) and use their own environment `dev`, which has no approval.
- Workflows pin action major versions checked against their current releases, use `uv` from `uv.lock`, and pin the CDK CLI to one exact version.
- Workflow permissions are least privilege: `id-token: write` only where AWS is used, `contents: read`.
- Deploy only this stage's stacks (`*-<stage>`), never `GitHubOidcStack`.
- Update the spine's AD-16, Deferred and Open Questions entries to match.

**Never:**
- No AWS access keys or secrets in the repo or in GitHub.
- No automatic deploy from PRs or feature branches.
- No changes to the simulator, facility model or SiteWise stack behaviour.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| PR opened | any PR to `main` | the test, synth and `poc` diff jobs run; nothing deploys | the job fails on any error |
| Merge to main | push to `main` | `ci.yml` runs; nothing deploys | none |
| Manual poc deploy | dispatch `stage=poc` from `main` | tests pass, then deploys `*-poc` in environment `poc` | failing tests stop the run before AWS; a failed deploy fails the run |
| poc from a branch | dispatch `stage=poc` from any ref but `main` | refused before any AWS call | the job fails with a message |
| Branch or PR run without an environment | a job on any branch or PR asks for the deploy role | cannot assume the deploy role | AWS denies it |
| PR diff | PR job assumes the diff role | can assume only the lookup role; `cdk diff` runs read-only | cannot assume the deploy role |
| Manual dev deploy | dispatch with stage `poc-dj` | deploys `*-poc-dj` with no approval | no error |
| Bad dispatch stage | `poc`, `prod` or `poc-DJ!` via dispatch | refused before any AWS call | the job fails with a message |
| Other repo | a token from a fork or another repo | cannot assume the role | AWS denies it |
| OIDC stack deploy | run once locally with profile `ongiiot-poc` | outputs the role ARN | no error |

</frozen-after-approval>

## Code Map

- `infra/app.py` -- `build_app(app)` reads context `stage` (default `poc`), calls `load(DEFAULT_PATH, stage=stage)` which validates the stage, and adds `SiteWiseAssetsStack-<stage>`. Add `GitHubOidcStack` (fixed name, no suffix) only when context `oidc` is `"true"`; leave stage behaviour untouched.
- `facility/model.py:42` -- `STAGE_RE = ^poc(-[a-z0-9]{1,12})?$`. It also accepts `poc`, so the dispatch check must additionally require the `poc-` prefix. Import it; do not edit.
- `infra/stacks/sitewise_assets.py`, `infra/stacks/seed_attributes.py` -- stage stacks. `Code.from_asset` only: no Docker bundling and no context lookups, so `cdk synth` needs no AWS credentials. Do not change.
- `tests/test_sitewise_stack.py` -- test idiom to copy: `cdk.App()` + stack + `Template.from_stack(...)`; module-scoped fixtures.
- `cdk.json` -- `"app": "uv run python -m infra.app"`; no change.
- `pyproject.toml` -- PyYAML is already a runtime dep (for workflow tests); aws-cdk-lib 2.271.0. No change.
- AWS facts (checked 2026-09-28): bootstrap qualifier `hnb659fds`, version 32; the bootstrap roles trust the account with `sts:AssumeRole` + `sts:TagSession`; the only OIDC provider is EKS; no `*-poc` stacks are deployed yet.
- GitHub facts: org `schuaschua` is on the Team plan (required reviewers work on private repos); the repo has no environments yet.
- Pinned versions (current releases on 2026-09-28): `actions/checkout@v7`, `astral-sh/setup-uv@v10`, `actions/setup-node@v7` (Node 22), `aws-actions/configure-aws-credentials@v6`, CDK CLI `aws-cdk@2.1143.0`.

## Tasks & Acceptance

**Execution:**
- [x] `infra/stacks/github_oidc.py` -- `GitHubOidcStack`: a native OIDC provider for `https://token.actions.githubusercontent.com` (client id `sts.amazonaws.com`). Role `ongiiot-github-deploy`: trust `aud` StringEquals, `sub` StringEquals the list [`repo:schuaschua/ongiiot:environment:poc`, `repo:schuaschua/ongiiot:environment:dev`]; the inline policy allows only `sts:AssumeRole` + `sts:TagSession` on `cdk-hnb659fds-*-123456789012-ap-southeast-1` and `ssm:GetParameter` on `/cdk-bootstrap/hnb659fds/version`. Role `ongiiot-github-diff`: `sub` StringEquals `repo:schuaschua/ongiiot:pull_request`; it may assume only `cdk-hnb659fds-lookup-role-123456789012-ap-southeast-1`, plus the same parameter read. Outputs `DeployRoleArn` and `DiffRoleArn`; 1 h max session; env pinned. -- OIDC auth, scoped by path.
- [x] `infra/app.py` -- instantiate the stack only when `-c oidc=true`; the docstring must not claim CI cannot change its own trust. -- stage deploys stay clean.
- [x] `.github/workflows/ci.yml` -- triggers `pull_request` and `push` on `main`; top-level `permissions: contents: read`. Jobs: `test` (setup-uv, `uv sync --locked`, `uv run pytest -q`); `synth` (plus setup-node, `npx --yes aws-cdk@2.1143.0 synth -c stage=poc --quiet`); `diff`, PR only, no `environment`, `id-token: write`, assumes `vars.AWS_DIFF_ROLE_ARN` in `vars.AWS_REGION`, runs `cdk diff "*-poc" -c stage=poc --method template`. -- PR gate, read-only.
- [x] `.github/workflows/deploy.yml` -- triggers `push` to `main` and `workflow_dispatch` (input `stage`). A `test` job (no AWS: setup-uv, `uv sync --locked`, `uv run pytest -q`). A `resolve` job with no AWS access derives the stage and environment: push gives `poc`/`poc`; dispatch validates with `STAGE_RE.fullmatch` plus the `poc-` prefix via `uv run python -c` and gives `<stage>`/`dev`, otherwise it fails with a message. A `deploy` job `needs: [test, resolve]`, with `environment` from `resolve`, `concurrency: deploy-<stage>` and `cancel-in-progress: false`, `id-token: write`, assumes `vars.AWS_DEPLOY_ROLE_ARN`, runs `cdk deploy "*-$STAGE" -c "stage=$STAGE" --require-approval never`. -- deploy flow, tested first.
- [x] `tests/test_github_oidc_stack.py` -- template assertions: the provider; each role's exact `sub` list and `aud`; each role's exact policy (the diff role cannot reach any non-lookup bootstrap role); both outputs; not present without `oidc=true`, present with it. -- matrix rows "Other repo", "Branch or PR run", "PR diff", "OIDC stack deploy".
- [x] `tests/test_workflows.py` -- parse both YAML files. Assert triggers, per-job permissions, `deploy` needs `test` and `resolve`, the environment wiring, which role variable each AWS job uses, pinned action majors and CDK version, no `secrets.`, no `${{ inputs.* }}` inside `run:`, the deploy target, and `--method template` on diff. Run the stage check on `poc`, `prod`, `poc-DJ!`, `poc-`, `poc-dj!`, `poc-` + 13 characters, `poc-dj\n` (all refused) and `poc-dj` (accepted). Run the `resolve` output step under bash for push and dispatch, and assert the parsed `GITHUB_OUTPUT` (`poc`/`poc`, `poc-dj`/`dev`). -- matrix rows.
- [x] `docs/ci-cd.md` -- runbook: the one-time `cdk deploy GitHubOidcStack -c oidc=true --profile ongiiot-poc`. GitHub setup: environment `poc` (required reviewer, branch rule `main`), environment `dev`, and variables `AWS_DEPLOY_ROLE_ARN`, `AWS_DIFF_ROLE_ARN`, `AWS_REGION`. State that step 2 must be finished before the first merge that adds `deploy.yml`, because GitHub auto-creates a missing environment unprotected. Cover how to dispatch a dev stage, and a "What this does and does not protect" section with the residual risk from Boundaries. -- operability.
- [x] `_bmad-output/planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md` -- amend AD-16 (deploys run in GitHub Actions, trust scoped by environment, PR diff read-only, tests before deploy, the residual shared-bootstrap risk); replace the Deferred CI/CD line (keep per-stage bootstrap isolation as deferred); add Stack rows (GitHub Actions + OIDC, CDK CLI 2.1143.0, Node 22); add an Open Questions row for who approves `poc`. -- keep the spine true.

**Acceptance Criteria:**
- Given the repo, when `uv run pytest -q` runs, then all tests pass, including the new ones.
- Given `-c oidc=true`, when `cdk synth` runs, then `GitHubOidcStack` synthesizes; without it, only the stage stacks synthesize.
- Given both workflow files, when actionlint runs, then it reports no errors.

## Implementation Notes

- The dispatch stage check uses a folded YAML scalar (`run: >-`) because the inline `": "` in the f-string is not valid in a plain YAML scalar. `tests/test_workflows.py` pulls the check code out of `deploy.yml` and runs it, so the test and the workflow cannot drift apart.
- The deploy command quotes the context flag (`-c "stage=$STAGE"`) to satisfy shellcheck inside actionlint.
- The OIDC role also sets `max_session_duration` to 1 h. `oidc` context accepts `"true"` (CLI) or `True` (tests).
- Verified: `uv run pytest -q` 210 passed; synth with `-c oidc=true` gives `GitHubOidcStack` + `SiteWiseAssetsStack-poc`, and without it only the stage stack; actionlint (local 1.7.12 and the `rhysd/actionlint:latest` image) reports no errors.

- Pass 2 implementation (2026-09-28): rebuilt from `.work/diff-cicd-pass1.patch` plus the amendments. Verified: `uv run pytest -q` 222 passed; synth with `-c oidc=true` gives `GitHubOidcStack` + `SiteWiseAssetsStack-poc`, without it only the stage stack; actionlint 1.7.12 and `rhysd/actionlint:latest` report no errors. Mutation checks: `fullmatch`→`match` fails the `poc-dj\n` case, and swapping the dispatch environment fails `test_resolve_outputs`.
- Review pass 1: code reverted after two intent_gap findings (see Review Triage Log); the pass-1 diff is kept at `.work/diff-cicd-pass1.patch` for reuse.

## Plan Change Log

- **Pass 1 → intent_gap (A, B), resolved by the human.** Triggers: ECH-4/BH-1/IA-a/IA-b (repo-wide trust bypasses the `poc` gate; PR diff held deploy credentials) and ECH-2/BH-3 (deploy did not wait for tests). Amended: trust scoped to `environment:poc`/`environment:dev`, a read-only diff role for `pull_request` using `cdk diff --method template`, a `test` job gating `deploy`. Carried patches folded into tasks: `fullmatch` stage check plus extra cases (ECH-3), `resolve` outputs run under bash (VG-1), no "CI cannot change its own trust" claim (ECH-5), environments before the first merge (BH-14). Known-bad state avoided: any branch or PR can deploy `poc` without approval. KEEP: the pass-1 structure that worked (the `resolve` job with no AWS, input only through `env:`, folded `>-` scalar for the stage check, the test pulling the check code out of `deploy.yml`, `-c "stage=$STAGE"` quoting, the native OIDC provider, the `oidc` context gate in `infra/app.py`, the exact-policy template tests).

- **Post-build → gate redesign, decided by the human.** Trigger: setting required reviewers on environment `poc` failed with HTTP 422 (the billing plan does not support it; the planning note "Team plan supports required reviewers" was wrong). Amended: `deploy.yml` is dispatch-only; `stage=poc` is accepted only from `refs/heads/main`; resolve maps `poc`→`poc`, else `dev`; tests, runbook and AD-16 updated. Known-bad state avoided: every merge to `main` deploying `poc` with no approval. KEEP: everything else from pass 2.

## Review Triage Log

**Pass 1 (review_loop_iteration 0 → 1).** Counts: high 5, medium 11, low 11, false 3, maybe-false 0. Outcome: two intent_gap entries, so the code is reverted (the diff is kept at `.work/diff-cicd.patch`) and the human is asked.

| Id | Finding | Verdict | Route | Evidence |
|----|---------|---------|-------|----------|
| ECH-4 | `sub` `repo:…:*` lets any branch or PR run assume the deploy role and deploy `*-poc` with no `poc` approval | high | intent_gap (A) | Trust is `StringLike repo:schuaschua/ongiiot:*` (`github_oidc.py`). The deploy role reaches `cfn-exec` (admin), and the gate lives only in editable YAML. The `:*` scope is written into the frozen Always block. |
| ECH-6 | Dispatch "refuses poc" only in the dispatched ref's own workflow | high | intent_gap (A) | Same root cause: an edited `deploy.yml` on a branch drops the check and still gets the role. |
| BH-1 | Anyone who can push a branch deploys `poc` without approval | high | intent_gap (A) | Duplicate of ECH-4, confirmed. |
| IA-a | Approval gate is at the YAML layer, not the AWS trust | high | intent_gap (A) | Same root cause as ECH-4. |
| IA-b | PR `diff` job holds deploy-equivalent credentials while running PR code | high | intent_gap (A) | `diff` assumes the one deploy role; the PR's `infra.app` runs with those credentials. |
| ECH-2 | `deploy.yml` on push does not wait for `ci` tests or synth | medium | intent_gap (B) | The two workflows fire on the same push independently; no `needs` or `workflow_run`. The intent does not say whether the gate is PR checks or a re-test. |
| BH-3 | Same: deploy does not wait for CI; dispatch deploys untested branches | medium | intent_gap (B) | Duplicate of ECH-2. |
| IA-f | R3c (tests before deploy) not implemented | medium | intent_gap (B) | Duplicate of ECH-2. |
| VG-o1 | Same: only the human approval sits between failing tests and `poc` | medium | intent_gap (B) | Duplicate of ECH-2. |
| ECH-5 | Runbook says CI "can never change its own trust" | medium | patch (carried) | False: the deploy role → `cfn-exec` (admin) can update `GitHubOidcStack`. The fix is rewording in `docs/ci-cd.md` and the `infra/app.py` docstring. |
| BH-2 | Same false claim | medium | patch (carried) | Duplicate of ECH-5. |
| BH-14 | If `poc` is not created first, GitHub auto-creates it unprotected, so the first merge deploys with no approval | medium | patch (carried) | This is GitHub behaviour for an undefined environment, and the runbook does not order setup before the first merge. The fix is a runbook line. |
| IA-d | Approval and variables are out-of-repo manual state | medium | patch (carried) | Same root cause as BH-14. |
| VG-1 | `resolve` event→stage/environment mapping only substring-tested | medium | patch (carried) | `test_deploy_resolve_has_no_aws` asserts that strings are present; swapping the branches passes. The fix runs the step under bash, as `test_dispatch_stage_check` does. |
| BH-4 | Grafana provisioning (AD-16) has no pipeline step or permissions | medium | defer | `infra/grafana_provision.py` does not exist yet (Story 1.5); not caused by this change. |
| ECH-3 | `STAGE_RE.match` accepts `"poc-dj\n"` | low | patch (carried) | Verified: `re.match` gives True, because `$` matches before a trailing newline. Output still resolves to `poc-dj`, so there is no escalation. The fix is `fullmatch` in the workflow snippet (the model stays untouched), plus test cases. |
| BH-8 | Same, plus missing cases `poc-dj!` and 13 characters | low | patch (carried) | Duplicate of ECH-3. |
| VG-o2 | Same trailing-newline gap | low | patch (carried) | Duplicate of ECH-3. |
| ECH-1 | `diff` fails on fork or Dependabot PRs | low | reject | Private org repo, no `dependabot.yml`; unlikely in everyday use, and the fix adds guards. |
| BH-7 | Same | low | reject | Duplicate of ECH-1. |
| BH-9 | A developer can dispatch another developer's stage | low | reject | Inherent to the intent's no-approval `dev` path; the fix adds an actor-mapping mechanism. |
| BH-11 | No `timeout-minutes`; a hung deploy holds the slot for 6 h | low | reject | CloudFormation times out on its own; rare in everyday use. |
| BH-12 | Region and account repeated across code, variables and docs | low | reject | Follows the existing `infra/app.py` constant style; no current divergence. |
| BH-13 | No test that `*-poc` never matches `GitHubOidcStack`, and none for the `"TRUE"` context value | low | reject | The glob `*-poc` cannot match `GitHubOidcStack`; `.lower()` is trivially correct. |
| IA-c | "Every deploy via GitHub Actions" is policy only; laptop access is unchanged | low | reject | The intent's reading R2a covers this, and removing admin laptop access is outside a POC. |
| IA-e | Tests check YAML text, not GitHub runtime semantics | low | reject | A true limit of static tests; actionlint plus the first real run cover it. |
| BH-10 | Actions pinned to majors, not SHAs | low | reject | The frozen Always block specifies major-version pinning. |
| BH-5 | Spine contradicts itself (only in Actions, yet OIDC stack deployed locally) | false | reject | The new AD-16 bullet names the local OIDC deploy explicitly; teardown remains in Deferred. |
| BH-6 | Deploy fails if the GitHub OIDC provider already exists | false | reject | Checked 2026-09-28: the account has only the EKS provider. |
| IA-g | Account pinned only via a repository variable | false | reject | Stage stacks deploy to the account of the assumed role, which exists only in 123456789012. |

**Pass 2 (review_loop_iteration 1).** Counts: high 0, medium 3, low 12, false 2, maybe-false 0, plus 7 carried. Outcome: 4 patches applied; no loopback.

| Id | Finding | Verdict | Route | Evidence |
|----|---------|---------|-------|----------|
| ECH2-7 | Runbook says a PR job cannot get the deploy role "whatever the workflow file says" | medium | patch | False as written: a same-repo PR workflow that declares `environment: dev` gets sub `…:environment:dev`, which the deploy role trusts. The fix rewords `docs/ci-cd.md` and adds the case to "Not protected". |
| ECH2-6 | The "read-only" diff role reaches `ReadOnlyAccess`, so an edited PR workflow can read data across the account | medium | patch | Verified 2026-09-28: the lookup role has `ReadOnlyAccess` attached. The runbook did not list this exposure. The fix is a doc addition. |
| BH2-2 | Same data-read exposure | medium | patch | Duplicate of ECH2-6. |
| ECH2-8 | Runbook line 10: "jobs without one of these environments cannot assume either role" | low | patch | Contradicts the diff role's `pull_request` trust (no environment). Direct wording fix. |
| BH2-9 | Same contradiction | low | patch | Duplicate of ECH2-8. |
| ECH2-2 | `ci.yml` `cancel-in-progress: true` cancels runs on pushes to `main` | low | patch | The group `ci-${{ github.ref }}` is shared by main pushes. One-line expression fix. |
| BH2-11b | Same | low | patch | Duplicate of ECH2-2. |
| ECH2-1 | An older commit's deploy can run after a newer one's | low | reject | Needs two pushes to main within minutes and both approved in reverse order; the approval page shows the commit. The fix adds workflow-level ordering. |
| ECH2-4 | `diff` is red until the repo variables are set | low | reject | One-time; the runbook orders setup before merging. |
| BH2-3 | OIDC `sub` customisation (`job_workflow_ref`) not weighed | low | reject | The human chose environment-scoped trust in pass 1; this would add a GitHub-side configuration dependency. |
| BH2-4 | `poc` approver sees no diff before approving | low | reject | The PR's `diff` job already showed the change before merge; the fix adds a job. |
| BH2-6 | No teardown workflow | false | reject | Teardown is already in the spine's Deferred list (manual `cdk destroy` per stage). |
| BH2-10 | Docs-only pushes trigger a deploy; tests run twice | low | reject | The human chose tests inside `deploy.yml`; a paths filter would add behaviour. |
| BH2-11a | Approver may approve a replaced pending run | false | reject | GitHub's approval page names the run's commit; the runbook documents replacement. |
| BH2-13 | Step-order and `stage=poc` end-to-end test gaps | low | reject | Both steps run in the same job before `deploy`; either failure stops it. The verification-gap lens found no gaps. |
| IA2 | Intent audit: environment-scoped trust, two roles, tests before deploy | low | reject | Descriptive; each divergence matches the pass-1 human decisions and the documented residual risk. |
| VG2 | No verification gaps | — | none | Lens reported no gaps. |
| ECH2-3 | `diff` fails on fork PRs | low | reject | carried (ECH-1): private org repo; unlikely in everyday use. |
| BH2-1 | Same | low | reject | carried (ECH-1). |
| VG2-o1 | Same | low | reject | carried (ECH-1). |
| ECH2-5 | Existing GitHub OIDC provider blocks the deploy | false | reject | carried (BH-6): only the EKS provider exists. |
| BH2-8 | Same | false | reject | carried (BH-6). |
| BH2-5 | Grafana provisioning not in the CI deploy | medium | defer | carried (BH-4): already in `deferred-work.md` for Story 1.5; not deferred again. |
| BH2-7 | SHA pinning | low | reject | carried (BH-10): the frozen block specifies major-version pins. |
| BH2-12 | Region defined in several places | low | reject | carried (BH-12). |

## Design Notes

- Pass workflow inputs into `run:` steps only through `env:` (for example `STAGE: ${{ inputs.stage }}`), never by inline interpolation. This prevents script injection from the dispatch input.
- Stage check, reusing the model's rule:
  ```bash
  uv run python -c 'import os,sys; from facility.model import STAGE_RE; s=os.environ["STAGE"]; sys.exit(0 if s.startswith("poc-") and STAGE_RE.fullmatch(s) else f"refused stage {s!r}: dispatch needs poc-<initials>")'
  ```
- A push to `main` never reads `inputs.stage`, so `poc` can only come from `main`. The `poc` environment's branch rule (runbook) adds a second guard.
- With an `environment:` set, GitHub's OIDC `sub` is `repo:<owner>/<repo>:environment:<name>`; for a `pull_request` job without an environment, it is `repo:<owner>/<repo>:pull_request`. Use StringEquals, not wildcards.
- `cdk diff --method template` compares templates using the lookup role (ReadOnlyAccess in this account), so the diff role never needs the deploy role.
- GitHub concurrency keeps at most one pending run per group; an older queued run can be superseded, but a running deploy is never cancelled.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `npx --yes aws-cdk@2.1143.0 synth -c oidc=true -c stage=poc --quiet` -- expected: exit 0; `cdk.out` holds `GitHubOidcStack` and `SiteWiseAssetsStack-poc`.
- `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest` -- expected: no errors.
