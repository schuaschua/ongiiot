# Walkthrough: CI/CD with GitHub Actions and AWS OIDC

Target: commit `7046ae7` on `feat/cicd` · Plan: [plan-cicd-github-actions.md](../implementation-artifacts/plan-cicd-github-actions.md)

Current block: **2 — Broad strokes**

## Blocks

- [x] **1 — Intent** (accepted, unchanged)
- [ ] **2 — Broad strokes** (in progress)
- [ ] **3 — AWS trust: two roles** (unvisited)
- [ ] **4 — Deploy flow: test → resolve → deploy** (unvisited)
- [ ] **5 — PR gate** (unvisited)
- [ ] **6 — Tests** (unvisited)
- [ ] **7 — Periphery** (unvisited)

---

## 1 — Intent

Source: the plan's frozen `## Intent`, verbatim. The Boundaries were amended in review pass 1 by human decision: environment-scoped trust, a read-only diff role, tests before deploy.

**Problem:** Deploys are manual, from one laptop. The human wants every deploy to run through GitHub Actions for the private repo `schuaschua/ongiiot` into AWS account `123456789012`, region `ap-southeast-1`, with no long-lived AWS keys in GitHub.

**Approach:**
- A one-time CDK `GitHubOidcStack` creates the GitHub OIDC provider and a deploy role trusted only by this repo.
- A PR workflow runs the tests, `cdk synth` and `cdk diff`.
- A deploy workflow deploys `poc` on push to `main`, gated by a GitHub environment approval, and deploys `poc-<initials>` stages through manual `workflow_dispatch`.

## 2 — Broad strokes

The change adds one account-level CDK stack that lets GitHub get short-lived AWS credentials, plus two workflows that use them. Open these in order:

1. [github_oidc.py:40](../../infra/stacks/github_oidc.py#L40) — `GitHubOidcStack`: the OIDC provider and the two roles GitHub may assume.
2. [deploy.yml:62](../../.github/workflows/deploy.yml#L62) — the `deploy` job: which environment it runs in, and the single `cdk deploy` command.
3. [ci.yml:38](../../.github/workflows/ci.yml#L38) — the PR `diff` job: read-only credentials, template diff against `poc`.
4. [app.py:32](../../infra/app.py#L32) — the `-c oidc=true` switch that keeps the OIDC stack out of every stage deploy.
5. [ci-cd.md:70](../../docs/ci-cd.md#L70) — what this protects and what it does not.

## 3 — AWS trust: two roles

GitHub signs a token whose `sub` names the repo and either the job's environment or `pull_request`. Each role accepts only exact `sub` values. The deploy role accepts only jobs in the `poc` or `dev` environments and may only assume the CDK bootstrap roles. The diff role accepts only PR jobs and may only assume the CDK lookup role.

- Trust subjects: [github_oidc.py:28](../../infra/stacks/github_oidc.py#L28)
- What each role may assume: [github_oidc.py:33](../../infra/stacks/github_oidc.py#L33)
- Shared role builder (trust + inline policy + 1 h session): [github_oidc.py:67](../../infra/stacks/github_oidc.py#L67)

## 4 — Deploy flow: test → resolve → deploy

Three jobs. `test` and `resolve` have no AWS access. `resolve` turns the event into a stage and an environment: a push gives `poc`/`poc`; a dispatch gives `poc-<initials>`/`dev`, after a stage check. `deploy` waits for both, runs in that environment (so `poc` pauses for approval), and deploys only `*-<stage>`.

- Tests first: [deploy.yml:21](../../.github/workflows/deploy.yml#L21)
- Stage check for dispatch: [deploy.yml:42](../../.github/workflows/deploy.yml#L42)
- Event → stage/environment: [deploy.yml:49](../../.github/workflows/deploy.yml#L49)
- Deploy job, concurrency, command: [deploy.yml:62](../../.github/workflows/deploy.yml#L62), [deploy.yml:86](../../.github/workflows/deploy.yml#L86)

## 5 — PR gate

`ci.yml` runs on PRs and pushes to `main`: tests, synth with no AWS access, and on PRs only a template diff against `poc` using the diff role. It cancels superseded runs only for PRs.

- Concurrency: [ci.yml:14](../../.github/workflows/ci.yml#L14)
- Synth: [ci.yml:36](../../.github/workflows/ci.yml#L36)
- Diff job: [ci.yml:38](../../.github/workflows/ci.yml#L38), [ci.yml:56](../../.github/workflows/ci.yml#L56)

## 6 — Tests

Stack tests check the synthesized IAM exactly: subjects, grants, outputs. Workflow tests parse the YAML, and they also run the two pieces of logic that can run locally: the stage-check code, taken from `deploy.yml`, and the `resolve` bash step.

- Trust and policy exactness: [test_github_oidc_stack.py:67](../../tests/test_github_oidc_stack.py#L67), [test_github_oidc_stack.py:93](../../tests/test_github_oidc_stack.py#L93), [test_github_oidc_stack.py:105](../../tests/test_github_oidc_stack.py#L105)
- Resolve step run under bash: [test_workflows.py:157](../../tests/test_workflows.py#L157)
- Stage check cases: [test_workflows.py:185](../../tests/test_workflows.py#L185)

## 7 — Periphery

- [app.py:32](../../infra/app.py#L32) — OIDC stack only with `-c oidc=true`.
- [ci-cd.md](../../docs/ci-cd.md) — one-time setup runbook; environments before the first merge.
- [ARCHITECTURE-SPINE.md:202](../planning-artifacts/architecture/architecture-og-iiot-2026-09-28/ARCHITECTURE-SPINE.md#L202) — AD-16 amended; Stack, Deferred and Open Questions updated.
- [deferred-work.md](../implementation-artifacts/deferred-work.md) — Grafana provisioning in CI deferred to Story 1.5.
