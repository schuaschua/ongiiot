"""Shared maths for trailing-run detectors (``dq_flatline``, ``dq_stuck``). Pure, no I/O."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from detect.core.models import Sample, Tunables


def own_tunables(t: Tunables, detector_id: str, keys: Sequence[str]) -> Mapping[str, Any]:
    """``t["detectors"][detector_id]``, raising ``ValueError`` if it or any of ``keys`` is missing or not a finite number."""
    try:
        mine = t["detectors"][detector_id]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"tunables.detectors.{detector_id} is missing") from exc
    if not isinstance(mine, Mapping):
        raise ValueError(f"tunables.detectors.{detector_id} must be a mapping")
    for k in keys:
        v = mine.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"tunables.detectors.{detector_id}.{k} must be a finite number, got {v!r}")
    return mine


def check_run_tunables(t: Tunables, detector_id: str, extra: Sequence[str] = ()) -> Mapping[str, Any]:
    """Validate ``min_run_s`` (>= 0), ``full_s`` (> 0, >= ``min_run_s``) plus ``extra`` numeric keys."""
    mine = own_tunables(t, detector_id, ("min_run_s", "full_s", *extra))
    if mine["min_run_s"] < 0:
        raise ValueError(f"tunables.detectors.{detector_id}.min_run_s must be >= 0")
    if not mine["full_s"] > 0 or mine["full_s"] < mine["min_run_s"]:
        raise ValueError(f"tunables.detectors.{detector_id}.full_s must be > 0 and >= min_run_s")
    return mine


def good(samples: Sequence[Sample]) -> list[Sample]:
    """Only GOOD samples with a finite value count."""
    return [
        s
        for s in samples
        if s.quality == "GOOD" and s.value is not None and isinstance(s.value, (int, float)) and math.isfinite(s.value)
    ]


def trailing_run(samples: Sequence[Sample], same: Callable[[Sample], bool]) -> list[Sample]:
    """The longest run of ``samples`` ending at the last one for which ``same`` holds (empty if the last fails)."""
    i = len(samples)
    while i > 0 and same(samples[i - 1]):
        i -= 1
    return list(samples[i:])


def run_seconds(run: Sequence[Sample]) -> float:
    """Span of a run: last timestamp minus first (0 for fewer than two samples)."""
    return (run[-1].ts - run[0].ts).total_seconds() if len(run) > 1 else 0.0


def confidence(run_s: float, min_run_s: float, full_s: float) -> float:
    """``0`` below ``min_run_s``, otherwise ``min(1, run_s / full_s)``."""
    if run_s < min_run_s:
        return 0.0
    return min(1.0, run_s / full_s)
