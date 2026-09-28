"""``dq_flatline``: a railed sensor (AD-5, FR3).

The trailing run of GOOD samples all at or beyond the same contract limit, within ``rel_tol`` x
(contract span): ``value <= min + tol`` or ``value >= max - tol``. Confidence ``0`` below
``min_run_s``, else ``min(1, run_s / full_s)``. Only the run ending at the sensor's last GOOD sample
counts (a non-GOOD sample or a gap over 2 sample periods ends it), so recovery clears the finding
on the next run.

Tunables (``tunables.detectors.dq_flatline``): ``min_run_s``, ``full_s``, ``rel_tol`` (``0 <= rel_tol < 0.5``).
"""

from __future__ import annotations

from typing import Literal

from detect.core.detectors._trailing import (
    check_run_tunables,
    confidence,
    last_good,
    max_gap_s,
    own_tunables,
    run_seconds,
    trailing_run,
    with_unit,
)
from detect.core.models import Contract, Finding, PipeWindow, Tunables

ID = "dq_flatline"


def check_rel_tol(t: Tunables) -> None:
    """``tunables.detectors.dq_flatline.rel_tol`` present and in ``[0, 0.5)`` (0.5 would rail every value)."""
    rel_tol = own_tunables(t, ID, ("rel_tol",))["rel_tol"]
    if not 0 <= rel_tol < 0.5:
        raise ValueError(f"tunables.detectors.{ID}.rel_tol must be >= 0 and < 0.5")


def rail_of(value: float, contract: Contract, t: Tunables) -> str | None:
    """``"min"`` or ``"max"`` if ``value`` is at or beyond that contract limit (within tolerance), else ``None``.

    The single definition of "railed", shared with ``dq_stuck`` so the two never both fire.
    """
    tol = t["detectors"][ID]["rel_tol"] * abs(contract.max - contract.min)
    if value <= contract.min + tol:
        return "min"
    if value >= contract.max - tol:
        return "max"
    return None


class DqFlatline:
    id = ID
    version = "1"
    kind: Literal["pipe"] = "pipe"

    def check_tunables(self, t: Tunables) -> None:
        check_run_tunables(t, ID, extra=("rel_tol",))
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
            rail = rail_of(last.value, contract, t)
            if rail is None:
                continue
            run = trailing_run(samples, lambda s: rail_of(s.value, contract, t) == rail, max_gap_s(contract))
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
                        f"{m} has been railed at its contract {rail}imum of {with_unit(limit, contract.unit)} "
                        f"for the last {run_s:.0f} s."
                    ),
                )
            )
        return out
