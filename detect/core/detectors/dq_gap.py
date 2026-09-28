"""``dq_gap``: a sensor that has gone silent (AD-5, FR3, AD-13).

``silence_s = window_end - (last GOOD sample at or before window_end)``, or the whole window when the
sensor has no GOOD sample in it. Confidence ``0`` below ``min_gap_s``, else
``min(1, silence_s / full_gap_s)``. Only the trailing silence counts: a gap earlier in the window with
data since gives no finding. An alias with no samples at all scores 1.0 here, which is the single
source of the AD-13 no-sample rule while this detector runs (``dq_score`` adds its own rule finding
only for an alias that has no ``dq_gap`` finding, e.g. when this detector failed).

Tunables (``tunables.detectors.dq_gap``): ``min_gap_s``, ``full_gap_s``
(``0 <= min_gap_s <= full_gap_s <= window_len_s``, ``full_gap_s > 0``).
"""

from __future__ import annotations

from typing import Literal

from detect.core.detectors._trailing import confidence, is_good, own_tunables
from detect.core.models import Finding, PipeWindow, Tunables

ID = "dq_gap"


class DqGap:
    id = ID
    version = "1"
    kind: Literal["pipe"] = "pipe"

    def check_tunables(self, t: Tunables) -> None:
        mine = own_tunables(t, ID, ("min_gap_s", "full_gap_s"))
        if not 0 <= mine["min_gap_s"] <= mine["full_gap_s"] or not 0 < mine["full_gap_s"] <= t["window_len_s"]:
            raise ValueError(f"tunables.detectors.{ID}: need 0 <= min_gap_s <= full_gap_s <= window_len_s and full_gap_s > 0")

    def detect(self, pw: PipeWindow, t: Tunables) -> list[Finding]:
        mine = t["detectors"][ID]
        w = pw.window
        whole = (w.end - w.start).total_seconds()
        out: list[Finding] = []
        for m in sorted(pw.pipe.aliases):
            alias = pw.pipe.aliases[m]
            last = None
            for s in reversed(w.series.get(alias, [])):
                if s.ts <= w.end and is_good(s):
                    last = s
                    break
            silence_s = whole if last is None else min(whole, max(0.0, (w.end - last.ts).total_seconds()))
            c = confidence(silence_s, mine["min_gap_s"], mine["full_gap_s"])
            if c <= 0:
                continue
            explanation = (
                f"{m} sent no GOOD samples in the window."
                if last is None
                else f"{m} has sent no GOOD samples for the last {silence_s:.0f} s of the window."
            )
            out.append(
                Finding(
                    detector_id=ID,
                    detector_version=self.version,
                    pipe_id=pw.pipe.pipe_id,
                    evidence_aliases=(alias,),
                    window_start=w.start,
                    window_end=w.end,
                    confidence=c,
                    explanation=explanation,
                )
            )
        return out
