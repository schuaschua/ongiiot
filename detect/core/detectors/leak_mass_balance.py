"""``leak_mass_balance``: mass going missing between the pipe's inlet and outlet (AD-5 pass 2, FR4).

A pipe detector. For every whole second in the trailing ``[window_end - avg_s, window_end)`` where both
``flow_in`` (> 0) and ``flow_out`` have a GOOD value, the loss fraction is ``(flow_in - flow_out) /
flow_in``; ``frac`` is its mean. Confidence ``0`` below ``min_frac``, else ``min(1, frac / full_frac)``.
No finding with fewer than ``min_points`` paired seconds.

Required sensors: ``flow_in`` and ``flow_out``. If either is untrusted (``dq < tunables.dq_min``) there
is no finding. A ``pressure_out`` drop is corroboration only. It is measured against ``pressure_in``
(which cancels the process pressure swings both share): the mean of ``pressure_out / pressure_in``
(paired by second) over the trailing ``avg_s`` against its mean over the rest of the window. A relative
drop of at least ``pressure_drop_frac`` is cited in the explanation and both pressure aliases join
``evidence_aliases``. If ``pressure_out`` (or its reference ``pressure_in``) is untrusted, the
explanation names the excluded sensor. Confidence never depends on the pressures.

Tunables (``tunables.detectors.leak_mass_balance``): ``avg_s``, ``min_points``, ``min_frac``,
``full_frac``, ``pressure_drop_frac``; plus ``tunables.dq_min``.
"""

from __future__ import annotations

import math
from typing import Literal

from detect.core.detectors._pass2 import check_avg_tunables, dq_min, dq_of, good_by_second, trusted, untrusted_note
from detect.core.models import Finding, PipeWindow, Tunables

ID = "leak_mass_balance"
KEYS = ("min_frac", "full_frac", "pressure_drop_frac")
REQUIRED = ("flow_in", "flow_out")
CORROBORATING = "pressure_out"
REFERENCE = "pressure_in"


class LeakMassBalance:
    id = ID
    version = "1"
    kind: Literal["pipe"] = "pipe"

    def check_tunables(self, t: Tunables) -> None:
        mine = check_avg_tunables(t, ID, KEYS)
        if not 0 < mine["min_frac"] <= mine["full_frac"] <= 1:
            raise ValueError(f"tunables.detectors.{ID}: need 0 < min_frac <= full_frac <= 1")
        if not 0 < mine["pressure_drop_frac"] < 1:
            raise ValueError(f"tunables.detectors.{ID}.pressure_drop_frac must be in (0, 1)")

    def detect(self, pw: PipeWindow, t: Tunables) -> list[Finding]:
        mine = t["detectors"][ID]
        floor = dq_min(t)
        p, w = pw.pipe, pw.window
        if not all(trusted(p, m, floor) for m in REQUIRED):
            return []
        end_s = w.end.timestamp()
        lo_s = end_s - mine["avg_s"]
        f_in = good_by_second(w.series.get(p.aliases["flow_in"], []), lo_s, end_s)
        f_out = good_by_second(w.series.get(p.aliases["flow_out"], []), lo_s, end_s)
        pairs = [(f_in[s], f_out[s]) for s in sorted(f_in.keys() & f_out.keys()) if f_in[s] > 0]
        if len(pairs) < mine["min_points"]:
            return []
        frac = sum((i - o) / i for i, o in pairs) / len(pairs)
        lost = sum(i - o for i, o in pairs) / len(pairs)
        if not (math.isfinite(frac) and math.isfinite(lost)) or frac < mine["min_frac"]:
            return []
        c = min(1.0, frac / mine["full_frac"])

        evidence = [p.aliases["flow_in"], p.aliases["flow_out"]]
        unit = p.contracts["flow_in"].unit if "flow_in" in p.contracts else ""
        lost_s = f"{lost:.3g} {unit}".rstrip()
        sentence = (
            f"flow_out is {frac * 100:.1f}% below flow_in ({lost_s} missing) on average over the last "
            f"{mine['avg_s']:g} s"
        )
        if CORROBORATING in p.aliases and REFERENCE in p.aliases:
            if not trusted(p, CORROBORATING, floor):
                sentence += f"; {untrusted_note(CORROBORATING, dq_of(p, CORROBORATING), floor)}"
            elif not trusted(p, REFERENCE, floor):
                sentence += (
                    f"; pressure_out was not checked because {untrusted_note(REFERENCE, dq_of(p, REFERENCE), floor)}"
                )
            else:
                drop = self._pressure_drop(pw, lo_s, end_s, mine["min_points"])
                if drop is not None and drop >= mine["pressure_drop_frac"]:
                    sentence += (
                        f", and pressure_out has dropped {drop * 100:.2f}% relative to pressure_in "
                        f"from earlier in the window"
                    )
                    evidence += [p.aliases[REFERENCE], p.aliases[CORROBORATING]]
        return [
            Finding(
                detector_id=ID,
                detector_version=self.version,
                pipe_id=p.pipe_id,
                evidence_aliases=tuple(evidence),
                window_start=w.start,
                window_end=w.end,
                confidence=c,
                explanation=sentence + ".",
            )
        ]

    @staticmethod
    def _pressure_drop(pw: PipeWindow, lo_s: float, end_s: float, min_points: float) -> float | None:
        """Relative drop of ``pressure_out / pressure_in``: the window before the trailing span vs the trailing span."""
        w, aliases = pw.window, pw.pipe.aliases

        def ratios(a: float, b: float) -> list[float]:
            p_out = good_by_second(w.series.get(aliases[CORROBORATING], []), a, b)
            p_in = good_by_second(w.series.get(aliases[REFERENCE], []), a, b)
            return [p_out[s] / p_in[s] for s in p_out.keys() & p_in.keys() if p_in[s] > 0]

        before = ratios(w.start.timestamp(), lo_s)
        after = ratios(lo_s, end_s)
        if len(before) < min_points or len(after) < min_points:
            return None
        base = sum(before) / len(before)
        trail = sum(after) / len(after)
        if not (math.isfinite(base) and math.isfinite(trail)) or base <= 0:
            return None
        return (base - trail) / base
