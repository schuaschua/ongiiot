"""``outlier_peer``: a pipe that behaves unlike its peers (AD-5 pass 2, AD-14, FR5).

A facility detector that emits per-pipe findings. For each pipe it computes four dimensionless or
per-sensor behaviour features over the trailing ``[window_end - avg_s, window_end)`` from trusted sensors:

- ``pressure_ratio``: mean ``pressure_out`` / mean ``pressure_in`` over the seconds where both have a GOOD value;
- ``flow_ratio``: mean ``flow_out`` / mean ``flow_in``, paired the same way;
- ``temperature``: mean GOOD ``temperature``;
- ``methane_ppm``: mean GOOD ``methane_ppm``.

A ratio of means (not a mean of ratios), so one near-zero inlet reading cannot blow it up. A feature has
no value with fewer than ``min_points`` (paired) seconds, a non-positive inlet mean, or mismatched in/out
units. Each feature is scored against the *other* pipes of the pipe's ``peer_group`` with a robust z-score,
``z = (x - median(peers)) / (1.4826 x MAD(peers) + floor_<feature>)``, where the noise floor stops a
near-zero MAD from exploding. A feature is scored only when at least ``MIN_GROUP`` (4) pipes of the group,
the pipe itself included, have a value for it, and only against peers with the same sensor units.

Confidence is ``0`` below ``min_z``, else ``min(1, (|z| - min_z) / (full_z - min_z))`` on the worst
feature. One finding per pipe; its evidence is the raw aliases behind every feature with ``|z| >= min_z``.
A sensor with ``dq < tunables.dq_min`` is never used: the features that need it are skipped for that
pipe (and named in the explanation), and the pipe drops out of those features' peer sets (named too).

Tunables (``tunables.detectors.outlier_peer``): ``avg_s``, ``min_points``, ``min_z``, ``full_z``, and
``floor_pressure_ratio``, ``floor_flow_ratio``, ``floor_temperature``, ``floor_methane_ppm``; plus
``tunables.dq_min``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, NamedTuple

from detect.core.detectors._pass2 import check_avg_tunables, dq_min, dq_of, good_by_second, trusted, untrusted_note
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
KEYS = ("min_z", "full_z", *FLOORS.values())


class _Value(NamedTuple):
    """One pipe's feature: ``value`` or why there is none (``untrusted`` sensor names, or too little data)."""

    value: float | None
    untrusted: tuple[str, ...] = ()


class OutlierPeer:
    id = ID
    version = "1"
    kind: Literal["facility"] = "facility"
    requires: tuple[str, ...] = ()  # no single sensor silences the check
    feature_sensors = FEATURES  # the handler reports a feature skipped when one of its sensors is untrusted

    def check_tunables(self, t: Tunables) -> None:
        mine = check_avg_tunables(t, ID, KEYS)
        if not 0 < mine["min_z"] < mine["full_z"]:
            raise ValueError(f"tunables.detectors.{ID}: need 0 < min_z < full_z")
        for key in FLOORS.values():
            if not mine[key] > 0:
                raise ValueError(f"tunables.detectors.{ID}.{key} must be > 0")

    def detect(self, fw: FacilityWindow, t: Tunables) -> list[Finding]:
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
            bad = tuple(m for m in ms if not trusted(p, m, floor))
            if bad:
                return _Value(None, bad)
            if len(ms) == 1:
                vals = series(p, ms[0])
                if len(vals) < min_points:
                    return _Value(None)
                mean = math.fsum(vals.values()) / len(vals)
                return _Value(mean if math.isfinite(mean) else None)
            num, den = ms
            c_num, c_den = p.contracts.get(num), p.contracts.get(den)
            if c_num is None or c_den is None or c_num.unit != c_den.unit:
                return _Value(None)
            a, b = series(p, num), series(p, den)
            common = a.keys() & b.keys()
            if len(common) < min_points:
                return _Value(None)
            s_num, s_den = math.fsum(a[s] for s in common), math.fsum(b[s] for s in common)
            if not (math.isfinite(s_num) and math.isfinite(s_den)) or s_den <= 0:
                return _Value(None)
            r = s_num / s_den
            return _Value(r if math.isfinite(r) else None)

        def unit(p: PipeMeta, f: str) -> str | None:
            c = p.contracts.get(FEATURES[f][0])
            return c.unit if c is not None else None

        out: list[Finding] = []
        for members in peer_groups(fw).values():
            table = {f: {p.pipe_id: feature(p, f) for p in members} for f in FEATURES}
            for p in members:
                scored = []  # (|z|, feature, x, ref, z, peer ids, notes)
                skipped = []
                for f in FEATURES:
                    own = table[f][p.pipe_id]
                    if own is None:
                        continue
                    if own.untrusted:
                        bad = own.untrusted[0]
                        skipped.append(f"{f} was not scored because {untrusted_note(bad, dq_of(p, bad), floor)}")
                        continue
                    if own.value is None:
                        continue
                    peers = {
                        q.pipe_id: v.value
                        for q in members
                        if q.pipe_id != p.pipe_id
                        and (v := table[f][q.pipe_id]) is not None
                        and v.value is not None
                        and unit(q, f) == unit(p, f)
                    }
                    if len(peers) + 1 < MIN_GROUP:
                        continue
                    xs = list(peers.values())
                    ref = median(xs)
                    mad = median([abs(v - ref) for v in xs])
                    z = (own.value - ref) / (MAD_SCALE * mad + mine[FLOORS[f]])
                    if not math.isfinite(z):
                        continue
                    left_out = [
                        (q.pipe_id, v)
                        for q in members
                        if q.pipe_id != p.pipe_id and q.pipe_id not in peers and (v := table[f][q.pipe_id]) is not None
                    ]
                    scored.append((abs(z), f, own.value, ref, z, sorted(peers), left_out))
                flagged = sorted((s for s in scored if s[0] >= mine["min_z"]), key=lambda s: (-s[0], s[1]))
                if not flagged:
                    continue
                worst = flagged[0]
                c = min(1.0, (worst[0] - mine["min_z"]) / (mine["full_z"] - mine["min_z"]))
                parts = [f"{f} {x:.4g} vs peer median {ref:.4g} (z {z:+.1f})" for _, f, x, ref, z, _, _ in flagged]
                peer_ids = worst[5]
                sentence = (
                    f"Unlike its {len(peer_ids)} peers in {p.peer_group} ({', '.join(peer_ids)}) over the last "
                    f"{mine['avg_s']:g} s: {'; '.join(parts)}"
                )
                notes = []
                for pid, v in worst[6]:
                    if v.untrusted:
                        notes.append(f"{pid} left out: {', '.join(v.untrusted)} untrusted")
                    elif v.value is None:
                        notes.append(f"{pid} left out: too little data")
                    else:
                        notes.append(f"{pid} left out: different unit")
                if notes:
                    sentence += f" ({'; '.join(notes)})"
                if skipped:
                    sentence += f"; {'; '.join(skipped)}"
                used = {m for _, f, *_rest in flagged for m in FEATURES[f]}
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
