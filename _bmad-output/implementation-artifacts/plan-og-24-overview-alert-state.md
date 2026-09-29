---
title: 'OG-24: Show incident alert state on the facility overview'
type: 'feature'
ticket: 'OG-24'
created: '2026-09-29'
status: 'built'
route: 'oneshot'
route_source: 'auto'
review: ''
review_source: ''
lenses_ran: []
review_loop_iteration: 0
baseline_revision: 'a80e4c027f349d5bfecc8255d8608851b2613882'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** During a live leak on P-007, `pipe-detail` showed the fault but `facility-overview` did not. Detection writes `derived/alert_state` every minute (Story 3.3), but no overview panel reads it (UX-DR1; OG-24).

**Approach:** Add a per-pipe prototype stat panel, `__PIPE_ID__ alert`, at the top of each pipe's block on `dashboards/facility-overview.json`. It reads `${alias_root}/__AREA__/__PIPE_ID__/derived/alert_state` (lastNotNull). It shows green "no incident" at 0 and red "N detectors" at 1 or more, and it links to `pipe-detail` like the other tiles. The existing Grafana custom resource provisions it (the prototype expander clones it per pipe). No detection change. `alert_summary` is a JSON string that a stat tile can't render usefully, so the detector names stay on `pipe-detail` and in the email.

</frozen-after-approval>

## Implementation Notes

- Oneshot route: the owner said to proceed without check-ins, so the plan was self-approved.
