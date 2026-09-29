---
title: 'Story 4.5: Prepare the demo runbook and fallback'
type: 'chore'
ticket: 'OG-22'
created: '2026-09-29'
status: 'done'
route: 'oneshot'
route_source: 'auto'
review: 'none'
review_source: 'auto'
lenses_ran: []
review_loop_iteration: 0
baseline_revision: '9124d49a5ff3f9dda2343d92f57b58dcf52bdcec'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The team has no single pre-demo checklist or fallback, so the day-4 demo depends on memory and on AWS behaving (Story 4.5, OG-22; AD-15).

**Approach:** Write `docs/demo-runbook.md` with the checklist (confirmed SNS subscriptions, 30+ minutes of warm-up, a passed smoke test, a reset run, health alarms OK), the demo script, when to switch to the fallback, and how to record and store the fallback.

</frozen-after-approval>

## Implementation Notes

- The owner said to proceed without check-ins, so the plan was self-approved.
- Docs only. Every read-only command in the runbook was run against `poc` on 2026-09-29: the subscriptions (the operator topic confirmed, the team topic empty), the task start time, both alarms `OK`, and no OPEN incidents. `ops.smoke` and `ops.reset` passed on `poc` the same day (smoke in 162 s).
- **Review skipped:** docs-only, and every command was checked live.
- **Not done here:** the screen recording itself has to be made by a person during a rehearsal. The runbook has a table for its location. The Jira issue notes this as a manual follow-up.
- Merged: PR #21, merge commit `a80e4c0`.
