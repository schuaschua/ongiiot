"""``dq_flatline``: a railed sensor (AD-5, FR3).

The trailing run of GOOD samples all within ``rel_tol`` x (contract span) of the same contract
``min`` or ``max``. Confidence ``0`` below ``min_run_s``, else ``min(1, run_s / full_s)``. Only the
run ending at the sensor's last sample counts, so recovery clears the finding on the next run.

Tunables (``tunables.detectors.dq_flatline``): ``min_run_s``, ``full_s``, ``rel_tol``.
"""

from __future__ import annotations

from typing import Literal

from detect.core.detectors._trailing import check_run_tunables, confidence, good, run_seconds, trailing_run
from detect.core.models import Contract, Finding, PipeWindow, Tunables

ID = "dq_flatline"


def rail_of(value: float, contract: Contract, t: Tunables) -> str | None:
    """``"min"`` or ``"max"`` if ``value`` is railed at that contract limit, else ``None``.

    The single definition of "railed", shared with ``dq_stuck`` so the two never both fire.
    """
    tol = t["detectors"][ID]["rel_tol"] * abs(contract.max - contract.min)
    if abs(value - contract.min) <= tol:
        return "min"
    if abs(value - contract.max) <= tol:
        return "max"
    return None


class DqFlatline:
    id = ID
    version = "1"
    kind: Literal["pipe"] = "pipe"

    def check_tunables(self, t: Tunables) -> None:
        check_run_tunables(t, ID, extra=("rel_tol",))
        if t["detectors"][ID]["rel_tol"] < 0:
            raise ValueError(f"tunables.detectors.{ID}.rel_tol must be >= 0")

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
            rail = rail_of(samples[-1].value, contract, t)
            if rail is None:
                continue
            run = trailing_run(samples, lambda s: rail_of(s.value, contract, t) == rail)
            run_s = run_seconds(run)
            c = confidence(run_s, mine["min_run_s"], mine["full_s"])
            if c <= 0:
                continue
            limit = contract.min if rail == "min" else contract.max
            out.append(
                Finding(
                    detector_id=ID,
                    detector_version=self.version,
                    pipe_id=pw.pipe.pipe_id,
                    evidence_aliases=(alias,),
                    window_start=w.start,
                    window_end=w.end,
                    confidence=c,
                    explanation=(
                        f"{m} has been railed at its contract {rail}imum of {limit:g} {contract.unit} "
                        f"for the last {run_s:.0f} s."
                    ),
                )
            )
        return out
