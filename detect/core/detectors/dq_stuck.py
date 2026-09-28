"""``dq_stuck``: a frozen sensor (AD-5, FR3).

The trailing run of GOOD samples with exactly the same value, when that value is *not* railed
(a railed run belongs to ``dq_flatline`` only, via ``dq_flatline.rail_of``, so the two never both
fire on one sensor). Confidence ``0`` below ``min_run_s``, else ``min(1, run_s / full_s)``.

Tunables (``tunables.detectors.dq_stuck``): ``min_run_s``, ``full_s``. The railed test uses
``dq_flatline``'s own ``rel_tol`` through ``rail_of``.
"""

from __future__ import annotations

from typing import Literal

from detect.core.detectors._trailing import check_run_tunables, confidence, good, run_seconds, trailing_run
from detect.core.detectors.dq_flatline import rail_of
from detect.core.models import Finding, PipeWindow, Tunables

ID = "dq_stuck"


class DqStuck:
    id = ID
    version = "1"
    kind: Literal["pipe"] = "pipe"

    def check_tunables(self, t: Tunables) -> None:
        check_run_tunables(t, ID)

    def detect(self, pw: PipeWindow, t: Tunables) -> list[Finding]:
        mine = t["detectors"][ID]
        w = pw.window
        out: list[Finding] = []
        for m in sorted(pw.pipe.aliases):
            alias = pw.pipe.aliases[m]
            contract = pw.pipe.contracts.get(m)
            samples = good(w.series.get(alias, []))
            if contract is None or not samples:
                continue
            value = samples[-1].value
            if rail_of(value, contract, t) is not None:
                continue
            run = trailing_run(samples, lambda s: s.value == value)
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
                    explanation=f"{m} has repeated exactly {value:g} {contract.unit} for the last {run_s:.0f} s.",
                )
            )
        return out
