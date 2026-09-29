---
title: 'OG-28: Alert the team when detection stops running'
type: 'feature'
ticket: 'OG-28'
created: '2026-09-30'
status: 'done'
route: 'oneshot'
route_source: 'auto'
review: 'none'
review_source: 'auto'
lenses_ran: []
review_loop_iteration: 0
baseline_revision: 'dacb55b717306d588b850f71dcca27b9d8629eef'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** If the detection schedule stops invoking the Lambda, no alarm fires: `detection-errors` sees no errors, and the overview tiles keep their last (green) value (deferred-work items 16 and 18; OG-28).

**Approach:** Add a third alarm to `DetectionStack-<stage>`, `detection-stalled-<stage>`: `AWS/Lambda` `Invocations` Sum < 1 over 5 minutes, 1 of 1, missing data breaching. It sends ALARM and OK to `team-alarms-<stage>` only, and its actions are disabled with `-c simulator=off` (the schedule is DISABLED then), like `simulator-down`. Update AD-15, `docs/ci-cd.md` and the runbook's alarm check.

</frozen-after-approval>

## Implementation Notes

- Oneshot, self-approved; the owner said to proceed without check-ins, then asked for OG-28 now.
- Tests extend the existing alarm tests (exact properties; the switch toggles `ActionsEnabled`); 176 pass.
- Live on `poc-dj` (2026-09-29, a fresh deploy from `3922152`): the schedule was disabled at 16:31:50Z. `detection-stalled-poc-dj` went OK → ALARM at 16:47:34Z, **about 16 minutes later**, and the team topic published it. CloudWatch evaluates missing data late here, as it did for `simulator-down`. The schedule was re-enabled at 16:47:55Z and `poc-dj` was torn down right after (cost-sensitive owner), so the ALARM → OK transition wasn't watched. The other alarms have shown their OK path live.
- A faster variant (60 s periods, 5 of 5) might fire in about 8–10 minutes, but it wasn't tried: a second live test costs another deploy. The runbook's T−35 check reads detection runs from the logs directly, so a stopped schedule is caught there before the demo anyway.
