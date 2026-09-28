"""Peer-group helpers shared by facility detectors (``dq_drift``, ``leak_methane_rise``). Pure, no I/O.

Peer groups come only from ``PipeMeta.peer_group`` (AD-14).
"""

from __future__ import annotations

from collections.abc import Sequence

from detect.core.models import FacilityWindow, PipeMeta


def median(xs: Sequence[float]) -> float:
    """Median of a non-empty sequence (the mean of the middle two for an even count)."""
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def peer_groups(fw: FacilityWindow) -> dict[str, list[PipeMeta]]:
    """``peer_group -> pipes``, pipes in pipe-id order."""
    groups: dict[str, list[PipeMeta]] = {}
    for pid in sorted(fw.pipes):
        groups.setdefault(fw.pipes[pid].peer_group, []).append(fw.pipes[pid])
    return groups
