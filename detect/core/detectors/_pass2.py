"""Shared helpers for the pass-2 detectors (``leak_*``). Pure, no I/O.

Pass 2 runs after ``dq_score`` has filled ``PipeMeta.dq`` (AD-5). A sensor is trusted only if its
``dq`` is present and at least ``tunables.dq_min``; a sensor without a score is not trusted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from detect.core.detectors._trailing import is_good, own_tunables
from detect.core.models import PipeMeta, Sample, Tunables


def dq_min(t: Tunables) -> float:
    """``tunables.dq_min``, raising ``ValueError`` unless it is a number in [0, 1]."""
    v = t.get("dq_min") if isinstance(t, Mapping) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise ValueError(f"tunables.dq_min must be a number in [0, 1], got {v!r}")
    return float(v)


def check_avg_tunables(t: Tunables, detector_id: str, extra: Sequence[str]) -> Mapping[str, Any]:
    """Validate ``0 < avg_s``, whole ``1 <= min_points <= avg_s``, ``window_len_s - avg_s >= min_points`` (room
    for a baseline before the trailing span), ``dq_min``, plus ``extra`` numbers."""
    mine = own_tunables(t, detector_id, ("avg_s", "min_points", *extra))
    wl = t.get("window_len_s") if isinstance(t, Mapping) else None
    if isinstance(wl, bool) or not isinstance(wl, (int, float)) or not math.isfinite(wl):
        raise ValueError(f"tunables.window_len_s must be a finite number, got {wl!r} (needed by {detector_id})")
    if not 0 < mine["avg_s"] < wl:
        raise ValueError(f"tunables.detectors.{detector_id}.avg_s must be > 0 and < window_len_s")
    if not float(mine["min_points"]).is_integer() or not 1 <= mine["min_points"] <= mine["avg_s"]:
        raise ValueError(f"tunables.detectors.{detector_id}.min_points must be a whole number, >= 1 and <= avg_s")
    if wl - mine["avg_s"] < mine["min_points"]:
        raise ValueError(f"tunables.detectors.{detector_id}: window_len_s - avg_s must leave at least min_points seconds")
    dq_min(t)
    return mine


def dq_of(pipe: PipeMeta, m: str) -> float | None:
    v = pipe.dq.get(m)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def trusted(pipe: PipeMeta, m: str, floor: float) -> bool:
    """The sensor exists and its dq is at least ``floor`` (``dq_min``)."""
    d = dq_of(pipe, m)
    return m in pipe.aliases and d is not None and d >= floor


def good_by_second(samples: Sequence[Sample], lo_s: float, hi_s: float) -> dict[int, float]:
    """GOOD values with ``lo_s <= ts < hi_s`` (epoch s), keyed by whole second (the last one wins)."""
    out: dict[int, float] = {}
    for s in samples:
        ts = s.ts.timestamp()
        if lo_s <= ts < hi_s and is_good(s):
            out[math.floor(ts)] = float(s.value)
    return out


def untrusted_note(m: str, d: float | None, floor: float) -> str:
    """``"pressure_out was excluded as untrusted (dq 0.20 < dq_min 0.5)"``."""
    shown = "none" if d is None else f"{d:.2f}"
    return f"{m} was excluded as untrusted (dq {shown} < dq_min {floor:g})"
