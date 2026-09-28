"""``outlier_peer``: a pipe that behaves unlike its peers (AD-5 pass 2, AD-14, FR5).

A facility detector that emits per-pipe findings. For each pipe it computes four dimensionless or
per-sensor behaviour features over the trailing ``[window_end - avg_s, window_end)`` from trusted sensors:

- ``pressure_ratio``: mean ``pressure_out`` / mean ``pressure_in`` over the seconds where both have a GOOD value;
- ``flow_ratio``: mean ``flow_out`` / mean ``flow_in``, paired the same way;
- ``temperature``: mean GOOD ``temperature``;
- ``methane_ppm``: mean GOOD ``methane_ppm``.

A ratio of means (not a mean of ratios), so one near-zero inlet reading cannot blow it up. A ratio has no
value when the in/out units differ ("unit mismatch"), when the inlet mean is below ``contract min +
min_den_frac x span`` of the inlet sensor (a shut-in or idling pipe), or when the outlet mean is negative.
Any feature has no value with fewer than ``min_points`` (paired) seconds. Ratios are dimensionless once
the pipe's own in/out units match, so peers' units are not compared for them; a single-sensor feature is
compared only with peers whose contract unit equals the pipe's (a missing contract is not comparable).

Each feature is scored against the *other* pipes of the pipe's ``peer_group`` with a robust z-score,
``z = (x - median(peers)) / (1.4826 x MAD(peers) + floor_<feature>)``, where the noise floor stops a
near-zero MAD from exploding. A feature is scored only when at least ``MIN_GROUP`` (4) pipes of the group,
the pipe itself included, have a comparable value for it. A pipe without a ``peer_group`` is not scored.

A feature is flagged when ``|z| > min_z``; confidence is ``min(1, (|z| - min_z) / (full_z - min_z))`` on
the worst flagged feature. One finding per pipe; its evidence is the raw aliases behind every flagged
feature, and the explanation gives, per flagged feature, its sensors, value, peer median, the peers
compared and those left out (with the reason), and z. A sensor with ``dq < tunables.dq_min`` is never
used: the features that need it are skipped for that pipe (every untrusted sensor is named), and the
pipe drops out of those features' peer sets. ``skipped`` reports every feature not scored per pipe
(untrusted, too little data, too few pipes) for the run summary's ``suppressed`` list.

Tunables (``tunables.detectors.outlier_peer``): ``avg_s``, ``min_points``, ``min_z``, ``full_z``,
``min_den_frac``, and ``floor_pressure_ratio``, ``floor_flow_ratio``, ``floor_temperature``,
``floor_methane_ppm``; plus ``tunables.dq_min``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, NamedTuple

from detect.core.detectors._pass2 import (
    check_avg_tunables,
    dq_min,
    dq_of,
    good_by_second,
    trusted,
    untrusted_note,
    untrusted_reason,
)
from detect.core.detectors._peers import median, peer_groups
from detect.core.models import FacilityWindow, Finding, PipeMeta, Tunables

ID = "outlier_peer"
MIN_GROUP = 4  # pipes with a value for the feature, the pipe itself included (AD-14)
MAD_SCALE = 1.4826  # MAD -> standard deviation for normal data

# feature -> the sensors it is computed from (numerator, denominator for a ratio).
FEATURES: Mapping[str, tuple[str, ...]] = {
    "pressure_ratio": ("pressure_out", "pressure_in"),
    "flow_ratio": ("flow_out", "flow_in"),
    "temperature": ("temperature",),
    "methane_ppm": ("methane_ppm",),
}
FLOORS = {f: f"floor_{f}" for f in FEATURES}
KEYS = ("min_z", "full_z", "min_den_frac", *FLOORS.values())


def label(f: str) -> str:
    """``flow_ratio (flow_out/flow_in)``; a single-sensor feature is its sensor's name."""
    ms = FEATURES[f]
    return f"{f} ({'/'.join(ms)})" if len(ms) > 1 else f


class _Value(NamedTuple):
    """One pipe's feature: ``value``, or why there is none (``untrusted`` sensors, or a ``reason``)."""

    value: float | None
    untrusted: tuple[str, ...] = ()
    reason: str = "too little data"


class _Scored(NamedTuple):
    feature: str
    x: float
    ref: float
    z: float
    peers: list[str]
    notes: list[str]


class OutlierPeer:
    id = ID
    version = "1"
    kind: Literal["facility"] = "facility"
    requires: tuple[str, ...] = ()  # no single sensor silences the check; see ``skipped``

    def check_tunables(self, t: Tunables) -> None:
        mine = check_avg_tunables(t, ID, KEYS)
        for key in KEYS:
            if not math.isfinite(mine[key]):
                raise ValueError(f"tunables.detectors.{ID}.{key} must be finite")
        if not 0 < mine["min_z"] < mine["full_z"]:
            raise ValueError(f"tunables.detectors.{ID}: need 0 < min_z < full_z")
        if not 0 <= mine["min_den_frac"] < 1:
            raise ValueError(f"tunables.detectors.{ID}.min_den_frac must be in [0, 1)")
        for key in FLOORS.values():
            if not mine[key] > 0:
                raise ValueError(f"tunables.detectors.{ID}.{key} must be > 0")

    def detect(self, fw: FacilityWindow, t: Tunables) -> list[Finding]:
        mine = t["detectors"][ID]
        floor = dq_min(t)
        w = fw.window
        out: list[Finding] = []
        for p, scored, _ in self._evaluate(fw, t):
            flagged = sorted((s for s in scored if abs(s.z) > mine["min_z"]), key=lambda s: (-abs(s.z), s.feature))
            if not flagged:
                continue
            c = min(1.0, (abs(flagged[0].z) - mine["min_z"]) / (mine["full_z"] - mine["min_z"]))
            parts = []
            for s in flagged:
                left = f" ({'; '.join(s.notes)})" if s.notes else ""
                parts.append(
                    f"{label(s.feature)} {s.x:.4g} vs median {s.ref:.4g} of peers {', '.join(s.peers)}{left}, z {s.z:+.1f}"
                )
            sentence = f"Unlike its peers in {p.peer_group} over the last {mine['avg_s']:g} s: {'; '.join(parts)}"
            for f in FEATURES:
                bad = self._untrusted(p, f, floor)
                if bad:
                    notes = " and ".join(untrusted_note(m, dq_of(p, m), floor) for m in bad)
                    sentence += f"; {f} was not scored because {notes}"
            used = {m for s in flagged for m in FEATURES[s.feature]}
            evidence = [a for m, a in p.aliases.items() if m in used]  # in the pipe's sensor order
            out.append(
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
            )
        return out

    def skipped(self, fw: FacilityWindow, t: Tunables) -> dict[str, list[str]]:
        """``pipe_id -> ["<feature> skipped: <reason>", ...]`` for every feature not scored on a pipe that has
        its sensors (untrusted, too little data, unit mismatch, too few pipes); read by the handler."""
        out: dict[str, list[str]] = {}
        for p, _, reasons in self._evaluate(fw, t):
            if reasons:
                out[p.pipe_id] = reasons
        return out

    @staticmethod
    def _untrusted(p: PipeMeta, f: str, floor: float) -> tuple[str, ...]:
        ms = FEATURES[f]
        if any(m not in p.aliases for m in ms):
            return ()
        return tuple(m for m in ms if not trusted(p, m, floor))

    def _evaluate(self, fw: FacilityWindow, t: Tunables) -> list[tuple[PipeMeta, list[_Scored], list[str]]]:
        """Per pipe with a peer group: its scored features and the reasons for the features it skipped."""
        mine = t["detectors"][ID]
        floor = dq_min(t)
        w = fw.window
        end_s = w.end.timestamp()
        lo_s = end_s - mine["avg_s"]
        min_points = mine["min_points"]

        def series(p: PipeMeta, m: str) -> dict[int, float]:
            return good_by_second(w.series.get(p.aliases[m], []), lo_s, end_s)

        def feature(p: PipeMeta, f: str) -> _Value | None:
            """``None`` when the pipe lacks a sensor of the feature (not compared at all)."""
            ms = FEATURES[f]
            if any(m not in p.aliases for m in ms):
                return None
            bad = self._untrusted(p, f, floor)
            if bad:
                return _Value(None, bad)
            if len(ms) == 1:
                vals = series(p, ms[0])
                if len(vals) < min_points:
                    return _Value(None)
                mean = math.fsum(vals.values()) / len(vals)
                return _Value(mean) if math.isfinite(mean) else _Value(None)
            num, den = ms
            c_num, c_den = p.contracts.get(num), p.contracts.get(den)
            if c_num is None or c_den is None or not c_num.unit or c_num.unit != c_den.unit:
                return _Value(None, reason="unit mismatch")
            a, b = series(p, num), series(p, den)
            common = a.keys() & b.keys()
            if len(common) < min_points:
                return _Value(None)
            m_num = math.fsum(a[s] for s in common) / len(common)
            m_den = math.fsum(b[s] for s in common) / len(common)
            if not (math.isfinite(m_num) and math.isfinite(m_den)):
                return _Value(None)
            if m_den <= 0 or m_den < c_den.min + mine["min_den_frac"] * (c_den.max - c_den.min):
                return _Value(None, reason=f"{den} too low")
            if m_num < 0:
                return _Value(None, reason=f"{num} negative")
            r = m_num / m_den
            return _Value(r) if math.isfinite(r) else _Value(None)

        def unit(p: PipeMeta, f: str) -> str | None:
            c = p.contracts.get(FEATURES[f][0])
            return c.unit if c is not None and c.unit else None

        def comparable(p: PipeMeta, q: PipeMeta, f: str) -> bool:
            # Ratios are dimensionless once each pipe's own in/out units match (checked in ``feature``).
            return len(FEATURES[f]) > 1 or (unit(p, f) is not None and unit(p, f) == unit(q, f))

        def why(v: _Value) -> str:
            return f"{', '.join(v.untrusted)} untrusted" if v.untrusted else v.reason

        out = []
        for group, members in peer_groups(fw).items():
            if not group:
                continue  # no peer group: nothing to compare with (AD-14 groups only by peer_group)
            table = {f: {p.pipe_id: feature(p, f) for p in members} for f in FEATURES}
            for p in members:
                scored: list[_Scored] = []
                reasons: list[str] = []
                for f in FEATURES:
                    own = table[f][p.pipe_id]
                    if own is None:
                        continue
                    if own.value is None:
                        if own.untrusted:
                            detail = ", ".join(untrusted_reason(m, dq_of(p, m), floor) for m in own.untrusted)
                        else:
                            detail = own.reason
                        reasons.append(f"{f} skipped: {detail}")
                        continue
                    peers: dict[str, float] = {}
                    notes: list[str] = []
                    for q in members:
                        v = table[f][q.pipe_id]
                        if q.pipe_id == p.pipe_id or v is None:
                            continue
                        if v.value is None:
                            notes.append(f"{q.pipe_id} left out: {why(v)}")
                        elif not comparable(p, q, f):
                            notes.append(f"{q.pipe_id} left out: unit mismatch")
                        else:
                            peers[q.pipe_id] = v.value
                    if len(peers) + 1 < MIN_GROUP:
                        reasons.append(f"{f} skipped: only {len(peers) + 1} pipes with data")
                        continue
                    xs = list(peers.values())
                    ref = median(xs)
                    mad = median([abs(v - ref) for v in xs])
                    z = (own.value - ref) / (MAD_SCALE * mad + mine[FLOORS[f]])
                    if math.isfinite(z):
                        scored.append(_Scored(f, own.value, ref, z, sorted(peers), notes))
                out.append((p, scored, reasons))
        return out
