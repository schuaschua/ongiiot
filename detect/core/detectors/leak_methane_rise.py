"""``leak_methane_rise``: methane around a pipe well above its peers' (AD-5 pass 2, FR4).

A facility detector that emits per-pipe findings (peers are needed). For each pipe, ``mean`` is the mean
of its GOOD ``methane_ppm`` samples in the trailing ``[window_end - avg_s, window_end)`` (at least
``min_points`` of them). ``excess = mean - median`` of the same trailing means on the *other* pipes of
its ``peer_group`` (``PipeMeta.peer_group``). Confidence ``0`` below ``min_ppm``, else
``min(1, excess / full_ppm)``. No finding with fewer than 2 usable peers.

Required sensor: the pipe's own ``methane_ppm``; if untrusted (``dq < tunables.dq_min``) there is no
finding. A peer whose ``methane_ppm`` is untrusted is left out of the median and named in the explanation.

Tunables (``tunables.detectors.leak_methane_rise``): ``avg_s``, ``min_points``, ``min_ppm``,
``full_ppm``; plus ``tunables.dq_min``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal

from detect.core.detectors._pass2 import check_avg_tunables, dq_min, good_by_second, trusted
from detect.core.detectors._peers import median, peer_groups
from detect.core.models import FacilityWindow, Finding, PipeMeta, Tunables

ID = "leak_methane_rise"
KEYS = ("min_ppm", "full_ppm")
M = "methane_ppm"
MIN_PEERS = 2


class LeakMethaneRise:
    id = ID
    version = "1"
    kind: Literal["facility"] = "facility"

    def check_tunables(self, t: Tunables) -> None:
        mine = check_avg_tunables(t, ID, KEYS)
        if not 0 < mine["min_ppm"] <= mine["full_ppm"]:
            raise ValueError(f"tunables.detectors.{ID}: need 0 < min_ppm <= full_ppm")

    def detect(self, fw: FacilityWindow, t: Tunables) -> list[Finding]:
        mine = t["detectors"][ID]
        floor = dq_min(t)
        w = fw.window
        end_s = w.end.timestamp()
        lo_s = end_s - mine["avg_s"]

        def trailing_mean(p: PipeMeta) -> float | None:
            vals = good_by_second(w.series.get(p.aliases[M], []), lo_s, end_s)
            if len(vals) < mine["min_points"]:
                return None
            mean = sum(vals.values()) / len(vals)
            return mean if math.isfinite(mean) else None

        out: list[Finding] = []
        for members in peer_groups(fw).values():
            with_sensor = [p for p in members if M in p.aliases]
            means: Mapping[str, float | None] = {
                p.pipe_id: trailing_mean(p) for p in with_sensor if trusted(p, M, floor)
            }
            untrusted = {p.pipe_id for p in with_sensor if not trusted(p, M, floor)}
            for p in with_sensor:
                mean = means.get(p.pipe_id)
                if p.pipe_id in untrusted or mean is None:
                    continue
                peers = [v for pid, v in means.items() if pid != p.pipe_id and v is not None]
                if len(peers) < MIN_PEERS:
                    continue
                ref = median(peers)
                excess = mean - ref
                if not math.isfinite(excess) or excess < mine["min_ppm"]:
                    continue
                c = min(1.0, excess / mine["full_ppm"])
                unit = p.contracts[M].unit if M in p.contracts else ""
                u = f" {unit}" if unit else ""
                sentence = (
                    f"methane_ppm averaged {mean:.3g}{u} over the last {mine['avg_s']:g} s, "
                    f"{excess:.3g}{u} above the median of its {len(peers)} peer pipes"
                )
                skipped = sorted(untrusted - {p.pipe_id})
                if skipped:
                    sentence += f" ({', '.join(skipped)} excluded as untrusted, dq < dq_min {floor:g})"
                out.append(
                    Finding(
                        detector_id=ID,
                        detector_version=self.version,
                        pipe_id=p.pipe_id,
                        evidence_aliases=(p.aliases[M],),
                        window_start=w.start,
                        window_end=w.end,
                        confidence=c,
                        explanation=sentence + ".",
                    )
                )
        return out
