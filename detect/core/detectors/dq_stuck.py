"""``dq_stuck``: a frozen sensor (AD-5, FR3).

The trailing run of GOOD samples with exactly the same value, when that value is *not* railed
(a railed run belongs to ``dq_flatline`` only, via ``dq_flatline.rail_of``, so the two never both
fire on one sensor). A non-GOOD sample or a gap over 2 sample periods ends the run. Confidence
``0`` below ``min_run_s``, else ``min(1, run_s / full_s)``. Assumes non-quantized readings.

Tunables (``tunables.detectors.dq_stuck``): ``min_run_s``, ``full_s``. The railed test uses
``dq_flatline``'s own ``rel_tol`` through ``rail_of``, so ``check_tunables`` validates it too.
"""

from __future__ import annotations

from typing import Literal

from detect.core.detectors._trailing import (
    check_run_tunables,
    confidence,
    last_good,
    max_gap_s,
    run_seconds,
    trailing_run,
    with_unit,
)
from detect.core.detectors.dq_flatline import check_rel_tol, rail_of
from detect.core.models import Finding, PipeWindow, Tunables

ID = "dq_stuck"


class DqStuck:
    id = ID
    version = "1"
    kind: Literal["pipe"] = "pipe"

    def check_tunables(self, t: Tunables) -> None:
        check_run_tunables(t, ID)
        check_rel_tol(t)

    def detect(self, pw: PipeWindow, t: Tunables) -> list[Finding]:
        mine = t["detectors"][ID]
        w = pw.window
        out: list[Finding] = []
        for m in sorted(pw.pipe.aliases):
            alias = pw.pipe.aliases[m]
            contract = pw.pipe.contracts.get(m)
            samples = w.series.get(alias, [])
            last = last_good(samples)
            if contract is None or last is None:
                continue
            value = last.value
            if rail_of(value, contract, t) is not None:
                continue
            run = trailing_run(samples, lambda s: s.value == value, max_gap_s(contract))
            run_s = run_seconds(run)
            c = confidence(run_s, mine["min_run_s"], mine["full_s"])
            if c <= 0:
                continue
            out.append(
                Finding(
                    detector_id=ID,
                    detector_version=self.version,
                    pipe_id=pw.pipe.pipe_id,
                    evidence_aliases=(alias,),
                    window_start=w.start,
                    window_end=w.end,
                    confidence=c,
                    explanation=f"{m} has repeated exactly {with_unit(value, contract.unit)} for the last {run_s:.0f} s.",
                )
            )
        return out
