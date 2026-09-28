"""Shared maths for trailing-run detectors (``dq_flatline``, ``dq_stuck``). Pure, no I/O."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from detect.core.models import Contract, Sample, Tunables

FALLBACK_MAX_GAP_S = 2.0  # used when the contract has no usable rate


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
    """Validate ``0 <= min_run_s <= full_s < window_len_s`` (``full_s > 0``) plus ``extra`` numeric keys."""
    mine = own_tunables(t, detector_id, ("min_run_s", "full_s", *extra))
    if mine["min_run_s"] < 0:
        raise ValueError(f"tunables.detectors.{detector_id}.min_run_s must be >= 0")
    if not 0 < mine["full_s"] < t["window_len_s"] or mine["full_s"] < mine["min_run_s"]:
        raise ValueError(f"tunables.detectors.{detector_id}: need min_run_s <= full_s < window_len_s and full_s > 0")
    return mine


def is_good(s: Sample) -> bool:
    """GOOD quality with a finite value."""
    v = s.value
    return s.quality == "GOOD" and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def last_good(samples: Sequence[Sample]) -> Sample | None:
    for s in reversed(samples):
        if is_good(s):
            return s
    return None


def max_gap_s(contract: Contract) -> float:
    """Longest gap inside one run: 2 x the contract's sample period (2 s without a usable rate)."""
    rate = contract.rate_hz
    if isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0:
        return 2.0 / rate
    return FALLBACK_MAX_GAP_S


def trailing_run(samples: Sequence[Sample], same: Callable[[Sample], bool], max_gap: float) -> list[Sample]:
    """The run of GOOD samples ending at the last GOOD sample for which ``same`` holds.

    A non-GOOD (or valueless) sample ends the run, and so does a gap longer than ``max_gap`` seconds
    between consecutive samples. Empty if the last GOOD sample fails ``same``.
    """
    i = len(samples) - 1
    while i >= 0 and not is_good(samples[i]):
        i -= 1
    if i < 0 or not same(samples[i]):
        return []
    run = [samples[i]]
    j = i - 1
    while j >= 0 and is_good(samples[j]) and same(samples[j]) and (run[-1].ts - samples[j].ts).total_seconds() <= max_gap:
        run.append(samples[j])
        j -= 1
    run.reverse()
    return run


def run_seconds(run: Sequence[Sample]) -> float:
    """Span of a run: last timestamp minus first (0 for fewer than two samples)."""
    return (run[-1].ts - run[0].ts).total_seconds() if len(run) > 1 else 0.0


def confidence(run_s: float, min_run_s: float, full_s: float) -> float:
    """``0`` below ``min_run_s``, otherwise ``min(1, run_s / full_s)``."""
    if run_s < min_run_s:
        return 0.0
    return min(1.0, run_s / full_s)


def with_unit(value: float, unit: str | None) -> str:
    """``"12.5 K"``, or just ``"12.5"`` when the unit is empty."""
    return f"{value:g} {unit}" if unit else f"{value:g}"
