"""Pure per-pipe process model: realism, determinism and injection effects (CAP-1, CAP-4)."""

from __future__ import annotations

import math
import statistics as st
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


def test_normal_values_within_contract_and_mass_balanced():
    for p in F.pipes:
        out = run(p.id, 3600)
        for m, series in out.items():
            c = p.contracts[m]
            assert len(series) == 3600
            assert all(q == "GOOD" for *_, q in series)
            assert all(c.min <= v <= c.max for v in values(series)), (p.id, m)
        imbalance = [a - b for a, b in zip(values(out["flow_in"]), values(out["flow_out"]))]
        mean_flow = st.mean(values(out["flow_in"]))
        assert abs(st.mean(imbalance)) < 0.002 * mean_flow
        assert all(po < pi for pi, po in zip(values(out["pressure_in"]), values(out["pressure_out"])))


def test_peers_look_similar():
    for group, ids in F.peer_groups().items():
        means = [st.mean(values(run(pid, 120)["flow_in"])) for pid in ids]
        assert max(means) / min(means) < 1.25, group
        temps = [st.mean(values(run(pid, 120)["temperature"])) for pid in ids]
        assert max(temps) - min(temps) < 10.0, group


def test_ambient_temperature_cycles_slowly():
    p = F.pipe("P-001")
    base = physics.baseline(p)
    state = physics.initial_state(p)
    temps = []
    for h in range(24):
        state, samples = physics.step(state, T0 + h * 3600)
        temps.append(next(s.value for s in samples if s.measurement == "temperature"))
    assert max(temps) - min(temps) > base.amplitude  # a visible daily swing


def test_deterministic_and_seeded():
    assert run("P-003", 60) == run("P-003", 60)
    assert run("P-003", 60) != run("P-003", 60, seed=1)


def test_default_leak_rate_is_ten_percent_of_mean_flow():
    state = physics.initial_state(F.pipe("P-007"))
    act = physics.activate(state, inj("leak"))
    assert act.params["rate_kg_s"] == pytest.approx(0.1 * state.base.flow)
    assert physics.activate(state, inj("leak", rate_kg_s=3)).params["rate_kg_s"] == 3


def test_leak_effects_clear_within_two_minutes():
    normal = run("P-007", 120)
    leaked = run("P-007", 120, [inj("leak")])
    base = physics.baseline(F.pipe("P-007"))
    rate = 0.1 * base.flow

    def imbalance(out):
        return [a - b for a, b in zip(values(out["flow_in"]), values(out["flow_out"]))]

    n_imb, l_imb = imbalance(normal), imbalance(leaked)
    band = 6 * st.pstdev(n_imb) + abs(st.mean(n_imb))
    assert st.mean(l_imb[-60:]) == pytest.approx(rate, rel=0.05)
    assert min(l_imb[-60:]) > band
    assert values(leaked["flow_in"]) == values(normal["flow_in"])  # upstream unchanged

    n_ch4, l_ch4 = values(normal["methane_ppm"]), values(leaked["methane_ppm"])
    assert min(l_ch4[-30:]) > max(n_ch4) + 6 * st.pstdev(n_ch4)
    assert st.mean(l_ch4[-30:]) - st.mean(n_ch4) > 500  # ~500+ ppmv above ~2 ppmv background
    assert st.mean(n_ch4) == pytest.approx(2.0, abs=0.5)

    n_po, l_po = values(normal["pressure_out"]), values(leaked["pressure_out"])
    assert st.mean(l_po[-60:]) < st.mean(n_po[-60:]) - 6 * st.pstdev([a - b for a, b in zip(n_po, values(normal["pressure_in"]))])


def test_dq_flatline_constant():
    out = run("P-003", 60, [inj("dq_flatline", pipe="P-003", measurement="pressure_in", start=T0 + 10)])
    vals = values(out["pressure_in"])
    assert len(set(vals[10:])) == 1 and len(set(vals[:10])) == 10
    assert all(q == "GOOD" for *_, q in out["pressure_in"])


def test_dq_stuck_repeats_last_value_good():
    out = run("P-003", 60, [inj("dq_stuck", pipe="P-003", measurement="flow_out", start=T0 + 10)])
    vals = values(out["flow_out"])
    assert all(v == vals[9] for v in vals[10:])
    assert all(q == "GOOD" for *_, q in out["flow_out"])
    assert values(out["flow_in"]) == values(run("P-003", 60)["flow_in"])


def test_dq_drift_linear_from_start_at():
    start = T0 + 30
    normal = values(run("P-003", 300)["temperature"])
    drifted = values(run("P-003", 300, [inj("dq_drift", pipe="P-003", measurement="temperature", start=start, per_min=0.6)])["temperature"])
    offsets = [d - n for d, n in zip(drifted, normal)]
    assert all(o == 0 for o in offsets[:30])
    for i in range(30, 300):
        assert offsets[i] == pytest.approx(0.6 * (i - 30) / 60.0, abs=1e-9)


def test_dq_gap_no_samples_while_active():
    out = run("P-003", 60, [inj("dq_gap", pipe="P-003", measurement="methane_ppm", start=T0 + 10, minutes=0.5)])
    ts = [t for t, *_ in out["methane_ppm"]]
    assert ts == [t for t in range(T0, T0 + 60) if not T0 + 10 <= t < T0 + 40]
    assert len(out["flow_in"]) == 60


def test_injection_outside_window_not_applied():
    later = inj("leak", start=T0 + 3600)
    assert run("P-007", 60, [later]) == run("P-007", 60)


def test_default_drift_rate():
    state = physics.initial_state(F.pipe("P-003"))
    drift = inj("dq_drift", pipe="P-003", measurement="pressure_in")
    rate = physics.activate(state, drift).params["per_min"]
    assert rate > 0
    normal = values(run("P-003", 121)["pressure_in"])
    drifted = values(run("P-003", 121, [drift])["pressure_in"])
    assert drifted[120] - normal[120] == pytest.approx(2 * rate, rel=1e-9)


def test_24h_no_injection_within_every_contract():
    # one pipe per service plus every pipe with contract overrides
    pipes = {"P-001", "P-005", "P-008", "P-010"} | {"P-004", "P-007", "P-016", "P-022"}
    for pid in sorted(pipes):
        p = F.pipe(pid)
        state = physics.initial_state(p)
        lo = {m: math.inf for m in p.sensors}
        hi = {m: -math.inf for m in p.sensors}
        for t in range(T0, T0 + 86400):
            state, samples = physics.step(state, t)
            for s in samples:
                lo[s.measurement] = min(lo[s.measurement], s.value)
                hi[s.measurement] = max(hi[s.measurement], s.value)
        for m in p.sensors:
            c = p.contracts[m]
            assert c.min <= lo[m] and hi[m] <= c.max, (pid, m, lo[m], hi[m])
