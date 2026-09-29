"""Pure per-pipe process model: realism, determinism and injection effects (CAP-1, CAP-4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from facility.control import new_injection
from facility.model import load
from sim import physics

F = load()
T0 = 1_790_000_000  # integer UTC epoch seconds


def run(pipe_id, seconds, injections=(), t0=T0, seed=0):
    """Step one pipe for ``seconds``; returns {measurement: [(t, value, quality)]}."""
    state = physics.initial_state(F.pipe(pipe_id), seed)
    acts = [physics.activate(state, i) for i in injections]
    out: dict[str, list] = {m: [] for m in F.pipe(pipe_id).sensors}
    for t in range(t0, t0 + seconds):
        live = [a for a, i in zip(acts, injections) if i.active_at(datetime.fromtimestamp(t, UTC))]
        state, samples = physics.step(state, t, live)
        for s in samples:
            out[s.measurement].append((s.t, s.value, s.quality))
    return out


def inj(type_, pipe="P-007", measurement=None, start=T0, minutes=10, **params):
    s = datetime.fromtimestamp(start, UTC)
    return new_injection(
        f"{type_}-{pipe}", type_, pipe, s, s + timedelta(minutes=minutes), measurement=measurement, params=params, facility=F
    )


def values(series):
    return [v for _, v, _ in series]


def test_deterministic_and_seeded():
    assert run("P-003", 60) == run("P-003", 60)
    assert run("P-003", 60) != run("P-003", 60, seed=1)


def test_default_leak_rate_is_ten_percent_of_mean_flow():
    state = physics.initial_state(F.pipe("P-007"))
    act = physics.activate(state, inj("leak"))
    assert act.params["rate_kg_s"] == pytest.approx(0.1 * state.base.flow)
    assert physics.activate(state, inj("leak", rate_kg_s=3)).params["rate_kg_s"] == 3


def test_dq_stuck_repeats_last_value_good():
    out = run("P-003", 60, [inj("dq_stuck", pipe="P-003", measurement="flow_out", start=T0 + 10)])
    vals = values(out["flow_out"])
    assert all(v == vals[9] for v in vals[10:])
    assert all(q == "GOOD" for *_, q in out["flow_out"])
    assert values(out["flow_in"]) == values(run("P-003", 60)["flow_in"])


def test_dq_gap_no_samples_while_active():
    out = run("P-003", 60, [inj("dq_gap", pipe="P-003", measurement="methane_ppm", start=T0 + 10, minutes=0.5)])
    ts = [t for t, *_ in out["methane_ppm"]]
    assert ts == [t for t in range(T0, T0 + 60) if not T0 + 10 <= t < T0 + 40]
    assert len(out["flow_in"]) == 60


def test_injection_outside_window_not_applied():
    later = inj("leak", start=T0 + 3600)
    assert run("P-007", 60, [later]) == run("P-007", 60)
