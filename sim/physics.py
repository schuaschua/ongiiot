"""Pure, seeded per-pipe process model (CAP-1, CAP-4).

``step(state, t, active) -> (state, samples)`` has no clock, no I/O and no hidden RNG:
randomness for a step is derived from ``(seed, pipe_id, t)``, so the same inputs always give
the same outputs and pipes are independent of each other.

Model per pipe:
- steady state from ``service``, fitted inside the pipe's contracts, with a small seeded
  per-pipe offset so peers look similar but not identical;
- a slow daily ambient temperature cycle (UTC);
- a shared process disturbance (AR(1)) moving both flows and both pressures together, plus
  small independent sensor noise;
- mass balance: ``flow_out = flow_in - leak``.

Injection effects (spine catalogue):
- ``leak``: ``flow_out`` drops by the rate, ``pressure_out`` drops, ``methane_ppm`` rises
  toward a plateau (about 6000 ppmv per unit leak fraction, so 600 ppmv for a 10% leak);
- ``dq_flatline``: constant value (the measurement's contract minimum, a railed sensor);
- ``dq_stuck``: the last emitted value repeated, quality GOOD;
- ``dq_drift``: linear offset ``per_min * minutes since start_at``;
- ``dq_gap``: no sample.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from facility.control import Injection
from facility.model import Contract, Pipe

DAY_S = 86400.0
PEAK_UTC_S = 6 * 3600.0  # ambient peak ~14:00 in Singapore
METHANE_BG_PPM = 2.0
METHANE_PPM_PER_LEAK_FRACTION = 6000.0
DEFAULT_LEAK_FRACTION = 0.10  # Design Notes: default rate_kg_s = 10% of mean flow
DEFAULT_DRIFT_FRACTION_PER_MIN = 0.01

# Leak dynamics (first-order time constants, seconds).
TAU_PRESSURE_S = 10.0
TAU_METHANE_S = 20.0
LEAK_DP_PER_FRACTION = 0.10  # extra pressure_out drop as a fraction of pressure_in per unit leak fraction

# AR(1) process disturbance.
AR_PHI = 0.98
AR_FLOW_REL = 0.01  # stationary std as a fraction of mean flow
AR_PRESSURE_REL = 0.003
AR_METHANE_PPM = 0.1

# Independent sensor noise (1 sigma).
NOISE_FLOW_REL = 0.002
NOISE_PRESSURE_REL = 0.0005
NOISE_TEMPERATURE_K = 0.05
NOISE_METHANE_PPM = 0.02
NOISE_METHANE_LEAK_REL = 0.05

# Service nominals: pressure_in Pa abs, flow kg/s, temperature K (mean), daily amplitude K, dp fraction.
SERVICE_NOMINALS: Mapping[str, Mapping[str, float]] = MappingProxyType(
    {
        "feed_gas": {"pressure_in": 6.0e6, "flow": 30.0, "temperature": 300.0, "amplitude": 3.0, "dp": 0.03},
        "boil_off_gas": {"pressure_in": 2.5e5, "flow": 5.0, "temperature": 140.0, "amplitude": 1.5, "dp": 0.04},
        "fuel_gas": {"pressure_in": 3.0e6, "flow": 20.0, "temperature": 295.0, "amplitude": 3.0, "dp": 0.03},
        "flare_header": {"pressure_in": 1.5e5, "flow": 1.5, "temperature": 290.0, "amplitude": 5.0, "dp": 0.05},
    }
)
_FALLBACK_SERVICE = "feed_gas"


@dataclass(frozen=True)
class Baseline:
    """Per-pipe steady state, fitted inside its contracts."""

    pipe_id: str
    sensors: tuple[str, ...]
    contracts: Mapping[str, Contract]
    pressure_in: float
    pressure_out: float
    flow: float
    temperature: float
    amplitude: float
    phase_s: float
    methane_bg: float

    def nominal(self, measurement: str) -> float:
        return {
            "pressure_in": self.pressure_in,
            "pressure_out": self.pressure_out,
            "flow_in": self.flow,
            "flow_out": self.flow,
            "temperature": self.temperature,
            "methane_ppm": self.methane_bg,
        }[measurement]


@dataclass(frozen=True)
class Active:
    """An injection being applied, with its params resolved to concrete values."""

    injection_id: str
    type: str
    start_t: float
    measurement: str | None = None
    params: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class PipeState:
    seed: int
    base: Baseline
    ar_flow: float = 0.0  # relative
    ar_pressure: float = 0.0  # relative
    ar_methane: float = 0.0  # ppm
    leak_dp: float = 0.0  # Pa, current extra pressure_out drop
    methane_excess: float = 0.0  # ppm above background
    last: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))  # last emitted values
    held: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))  # injection_id -> stuck value


@dataclass(frozen=True)
class Sample:
    measurement: str
    t: float  # UTC epoch seconds
    value: float
    quality: str = "GOOD"


# -- construction ------------------------------------------------------------------------


def _rng(*parts: object) -> random.Random:
    return random.Random(":".join(str(p) for p in parts))


def _fit(v: float, c: Contract | None, margin: float = 0.01) -> float:
    if c is None:
        return v
    span = c.max - c.min
    return min(max(v, c.min + margin * span), c.max - margin * span)


def baseline(pipe: Pipe, seed: int = 0) -> Baseline:
    nom = SERVICE_NOMINALS.get(pipe.service, SERVICE_NOMINALS[_FALLBACK_SERVICE])
    r = _rng(seed, pipe.id, "baseline")
    jitter = lambda rel: 1.0 + r.uniform(-rel, rel)
    c = pipe.contracts

    flow_caps = [c[m] for m in ("flow_in", "flow_out") if m in c]
    flow = nom["flow"] * jitter(0.05)
    for fc in flow_caps:
        flow = min(max(flow, fc.min + 0.02 * (fc.max - fc.min)), fc.min + 0.6 * (fc.max - fc.min))

    p_in = _fit(nom["pressure_in"] * jitter(0.03), c.get("pressure_in"))
    p_out = _fit(p_in * (1.0 - nom["dp"] * jitter(0.1)), c.get("pressure_out"))
    if p_out >= p_in:
        p_out = p_in * (1.0 - nom["dp"])

    temp_c = c.get("temperature")
    amplitude = nom["amplitude"]
    temperature = _fit(nom["temperature"] + r.uniform(-2.0, 2.0), temp_c, margin=0.1)
    if temp_c is not None:
        amplitude = min(amplitude, 0.05 * (temp_c.max - temp_c.min))

    mc = c.get("methane_ppm")
    methane_bg = METHANE_BG_PPM * jitter(0.1)
    if mc is not None and methane_bg < mc.min + 1.0:
        methane_bg = mc.min + 1.0

    return Baseline(
        pipe_id=pipe.id,
        sensors=tuple(pipe.sensors),
        contracts=pipe.contracts,
        pressure_in=p_in,
        pressure_out=p_out,
        flow=flow,
        temperature=temperature,
        amplitude=amplitude,
        phase_s=r.uniform(-1800.0, 1800.0),
        methane_bg=methane_bg,
    )


def initial_state(pipe: Pipe, seed: int = 0) -> PipeState:
    return PipeState(seed=seed, base=baseline(pipe, seed))


def activate(state: PipeState, inj: Injection) -> Active:
    """Resolve an injection's params against this pipe (defaults per Design Notes)."""
    params = dict(inj.params)
    b = state.base
    if inj.type == "leak":
        params.setdefault("rate_kg_s", DEFAULT_LEAK_FRACTION * b.flow)
    elif inj.type == "dq_drift":
        m = inj.measurement
        c = b.contracts.get(m)
        span = (c.max - c.min) if c else 0.0
        params.setdefault("per_min", max(DEFAULT_DRIFT_FRACTION_PER_MIN * abs(b.nominal(m)), 0.001 * span))
    return Active(
        injection_id=inj.injection_id,
        type=inj.type,
        start_t=inj.start_at.timestamp(),
        measurement=inj.measurement,
        params=MappingProxyType(params),
    )


# -- dynamics ------------------------------------------------------------------------------


def _ar(x: float, sigma: float, r: random.Random) -> float:
    return AR_PHI * x + r.gauss(0.0, sigma * math.sqrt(1.0 - AR_PHI**2))


def step(state: PipeState, t: float, active: Sequence[Active] = ()) -> tuple[PipeState, list[Sample]]:
    """Advance one second to time ``t`` and return the samples emitted at ``t``."""
    b = state.base
    r = _rng(state.seed, b.pipe_id, int(t))

    ar_flow = _ar(state.ar_flow, AR_FLOW_REL, r)
    ar_pressure = _ar(state.ar_pressure, AR_PRESSURE_REL, r)
    ar_methane = _ar(state.ar_methane, AR_METHANE_PPM, r)

    # Physical truth.
    flow_in = b.flow * (1.0 + ar_flow)
    leak_rate = sum(a.params["rate_kg_s"] for a in active if a.type == "leak")
    leak_rate = min(leak_rate, 0.9 * flow_in)
    leak_fraction = leak_rate / b.flow if b.flow > 0 else 0.0
    flow_out = flow_in - leak_rate

    p_in = b.pressure_in * (1.0 + ar_pressure)
    flow_ratio = flow_in / b.flow if b.flow > 0 else 1.0
    dp = (b.pressure_in - b.pressure_out) * flow_ratio**2
    target_leak_dp = b.pressure_in * LEAK_DP_PER_FRACTION * leak_fraction
    leak_dp = state.leak_dp + (target_leak_dp - state.leak_dp) * (1.0 - math.exp(-1.0 / TAU_PRESSURE_S))
    p_out = p_in - dp - leak_dp

    temperature = b.temperature + b.amplitude * math.cos(2.0 * math.pi * ((t - PEAK_UTC_S - b.phase_s) % DAY_S) / DAY_S)

    mc = b.contracts.get("methane_ppm")
    plateau = METHANE_PPM_PER_LEAK_FRACTION * leak_fraction
    if mc is not None:
        plateau = max(0.0, min(plateau, 0.9 * mc.max - b.methane_bg))
    methane_excess = state.methane_excess + (plateau - state.methane_excess) * (1.0 - math.exp(-1.0 / TAU_METHANE_S))
    methane = b.methane_bg + ar_methane + methane_excess

    # Sensor readings.
    readings = {
        "pressure_in": p_in * (1.0 + r.gauss(0.0, NOISE_PRESSURE_REL)),
        "pressure_out": p_out * (1.0 + r.gauss(0.0, NOISE_PRESSURE_REL)),
        "flow_in": flow_in + r.gauss(0.0, NOISE_FLOW_REL * b.flow),
        "flow_out": flow_out + r.gauss(0.0, NOISE_FLOW_REL * b.flow),
        "temperature": temperature + r.gauss(0.0, NOISE_TEMPERATURE_K),
        "methane_ppm": max(
            0.0,
            methane + r.gauss(0.0, NOISE_METHANE_PPM) + r.gauss(0.0, NOISE_METHANE_LEAK_REL * methane_excess),
        ),
    }

    # Sensor faults, in start order.
    held = dict(state.held)
    gaps: set[str] = set()
    live_ids = set()
    for a in sorted(active, key=lambda a: (a.start_t, a.injection_id)):
        m = a.measurement
        if m is None or m not in readings:
            continue
        live_ids.add(a.injection_id)
        if a.type == "dq_flatline":
            c = b.contracts.get(m)
            readings[m] = c.min if c is not None else 0.0
        elif a.type == "dq_stuck":
            if a.injection_id not in held:
                held[a.injection_id] = state.last.get(m, readings[m])
            readings[m] = held[a.injection_id]
        elif a.type == "dq_drift":
            readings[m] += a.params["per_min"] * max(0.0, t - a.start_t) / 60.0
        elif a.type == "dq_gap":
            gaps.add(m)
    held = {k: v for k, v in held.items() if k in live_ids}

    samples = [Sample(m, t, readings[m]) for m in b.sensors if m not in gaps]
    last = dict(state.last)
    last.update({s.measurement: s.value for s in samples})

    new_state = replace(
        state,
        ar_flow=ar_flow,
        ar_pressure=ar_pressure,
        ar_methane=ar_methane,
        leak_dp=leak_dp,
        methane_excess=methane_excess,
        last=MappingProxyType(last),
        held=MappingProxyType(held),
    )
    return new_state, samples
