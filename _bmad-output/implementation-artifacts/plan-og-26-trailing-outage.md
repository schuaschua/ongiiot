---
title: 'OG-26: A source that stops mid-window is a source outage'
type: 'bugfix'
ticket: 'OG-26'
created: '2026-09-29'
status: 'built'
route: 'oneshot'
route_source: 'auto'
review: 'quick'
review_source: 'auto'
lenses_ran: ['quick']
review_loop_iteration: 0
baseline_revision: '48e18288f6134ad8903dd6b2dd9af616cfd4620f'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The source-outage guard in `detect/handler.py` only counts aliases with *no samples* in the 15-minute window. When the simulator stops partway through a window, every alias still has samples, so each sensor's trailing `dq_gap` feeds an incident and all 24 pipes get opened and emailed. This was seen live on `poc-dj` at 06:25:37Z on 2026-09-29 (OG-26).

**Approach:** Count an alias as silent when it carries a `dq_gap` finding: either the gap rule for an alias with no data, or the detector's trailing silence of at least `min_gap_s`. The outage fires when at least half of the expected aliases are silent. The rest of the guard is unchanged: `dq_gap` findings don't feed incidents, no detector counts a quiet run, and the dq values are still written. The log line reports `aliases_silent`.

</frozen-after-approval>

## Implementation Notes

- Oneshot route: about 10 lines in `detect/handler.py` plus one test. The owner said to proceed without them (2026-09-29), so there was no human checkpoint.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass.

## Review Triage Log

Quick lens, 5 findings: 4 patched, 1 rejected.

- **Q-1, low, patch.** An alias sending only BAD or UNCERTAIN samples has a `dq_gap` finding, so it now counts as silent. This is intended: a facility-wide quality fault is a source-level problem, not 24 pipe incidents. The docs and docstring now say "no GOOD sample".
- **Q-2, low, reject.** The concern: a run whose window ends about 30 s after a stop could see only some aliases past `min_gap_s`. The simulator samples every alias in phase at 1 Hz and writes them in the same batch (`docs/ci-cd.md`, `dq_drift` notes), so their trailing silences are equal and they cross the threshold in the same run. Guarding against it would add hysteresis.
- **Q-3, low, patch.** `aliases_silent` wasn't asserted. The test now checks `aliases_silent == aliases == 144`.
- **Q-4, low, patch.** The doc sentence had two parentheticals in a row and didn't mention the stop case. It's rewritten.
- **Q-5, low, patch.** An overlong docstring line is rewrapped.
