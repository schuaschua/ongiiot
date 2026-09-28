"""``dq_drift``: a sensor slowly walking away from its peers (AD-5, FR3).

A facility detector. For each sensor, the residual is its GOOD value minus the median of the same
measurement's GOOD values on the *other* pipes of its ``peer_group`` at the same timestamp
(timestamps matched to the whole second). A least-squares line is fitted to the residual over the
trailing ``fit_s`` seconds of the window, and its slope, divided by the sensor's contract span
(max - min), gives ``rate`` in fraction of span per minute.

Confidence ``0`` if ``rate < min_rate`` or the line explains less than ``min_r2`` of the residual's
variance (the residual must keep moving in one direction: process wander, a step such as a leak, or a
change shared by the whole peer group is not drift), else ``min(1, rate / full_rate)``. No finding
with fewer than 2 peers with data in the fit span or fewer than ``min_points`` residual points, and
none if the fit overflows (non-finite rate or R²). The explanation names the fewest peers behind any
residual point. Samples are taken from ``[window_end - fit_s, window_end)`` (the half-open window, AD-9).

Tunables (``tunables.detectors.dq_drift``): ``fit_s``, ``min_points``, ``min_rate``, ``full_rate``,
``min_r2``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal

from detect.core.detectors._peers import median as _median
from detect.core.detectors._peers import peer_groups
from detect.core.detectors._trailing import is_good, own_tunables
from detect.core.models import FacilityWindow, Finding, PipeMeta, Tunables

ID = "dq_drift"
MIN_PEERS = 2
KEYS = ("fit_s", "min_points", "min_rate", "full_rate", "min_r2")


def fit_line(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """``(slope, r2)`` of the least-squares line through the points (``r2`` 0 for a flat residual)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    # Products, not ``** 2``: a float power raises OverflowError, a product overflows to inf (checked by the caller).
    sxx = sum((x - mx) * (x - mx) for x in xs)
    syy = sum((y - my) * (y - my) for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx <= 0:
        return 0.0, 0.0
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return slope, r2


class DqDrift:
    id = ID
    version = "1"
    kind: Literal["facility"] = "facility"

    def check_tunables(self, t: Tunables) -> None:
        mine = own_tunables(t, ID, KEYS)
        if not 0 < mine["fit_s"] <= t["window_len_s"]:
            raise ValueError(f"tunables.detectors.{ID}.fit_s must be > 0 and <= window_len_s")
        if not float(mine["min_points"]).is_integer() or not 3 <= mine["min_points"] <= mine["fit_s"]:
            raise ValueError(f"tunables.detectors.{ID}.min_points must be a whole number, >= 3 and <= fit_s")
        if not 0 < mine["min_rate"] <= mine["full_rate"]:
            raise ValueError(f"tunables.detectors.{ID}: need 0 < min_rate <= full_rate")
        if not 0 <= mine["min_r2"] <= 1:
            raise ValueError(f"tunables.detectors.{ID}.min_r2 must be in [0, 1]")

    def detect(self, fw: FacilityWindow, t: Tunables) -> list[Finding]:
        mine = t["detectors"][ID]
        w = fw.window
        end_s = w.end.timestamp()
        lo_s = end_s - mine["fit_s"]

        def good_by_second(alias: str) -> dict[int, float]:
            """GOOD values in ``[end - fit_s, end)``, keyed by whole second (the last one wins)."""
            out: dict[int, float] = {}
            for s in w.series.get(alias, []):
                ts = s.ts.timestamp()
                if lo_s <= ts < end_s and is_good(s):
                    out[math.floor(ts)] = float(s.value)
            return out

        groups = peer_groups(fw)

        out: list[Finding] = []
        for members in groups.values():
            for m in sorted({m for p in members for m in p.aliases}):
                data: Mapping[str, dict[int, float]] = {
                    p.pipe_id: good_by_second(p.aliases[m]) for p in members if m in p.aliases
                }
                for p in members:
                    if m not in p.aliases:
                        continue
                    f = self._one(p, m, data, mine, w.start, w.end, end_s)
                    if f is not None:
                        out.append(f)
        return out

    def _one(self, p: PipeMeta, m: str, data, mine, start, end, end_s) -> Finding | None:
        contract = p.contracts.get(m)
        if contract is None:
            return None
        span = contract.max - contract.min
        if not math.isfinite(span) or span <= 0:
            return None
        peers = {pid: vals for pid, vals in data.items() if pid != p.pipe_id and vals}
        if len(peers) < MIN_PEERS:
            return None
        xs: list[float] = []
        ys: list[float] = []
        used = len(peers)  # fewest peers behind any residual point
        for sec, v in sorted(data[p.pipe_id].items()):
            others = [vals[sec] for vals in peers.values() if sec in vals]
            if len(others) < MIN_PEERS:
                continue
            used = min(used, len(others))
            xs.append(sec - end_s)
            ys.append(v - _median(others))
        if len(xs) < max(mine["min_points"], 3):
            return None
        slope, r2 = fit_line(xs, ys)
        rate = abs(slope) * 60.0 / span
        if not (math.isfinite(rate) and math.isfinite(r2)) or rate < mine["min_rate"] or r2 < mine["min_r2"]:
            return None
        c = min(1.0, rate / mine["full_rate"])
        if c <= 0:
            return None
        direction = "up" if slope > 0 else "down"
        amount = f"{abs(slope) * 60.0:.3g}"
        per_min = f"{amount} {contract.unit}/min" if contract.unit else f"{amount} per min"
        return Finding(
            detector_id=ID,
            detector_version=self.version,
            pipe_id=p.pipe_id,
            evidence_aliases=(p.aliases[m],),
            window_start=start,
            window_end=end,
            confidence=c,
            explanation=(
                f"{m} has drifted {direction} from the median of its {used} peer pipes "
                f"by {per_min} over the last {mine['fit_s']:.0f} s."
            ),
        )
