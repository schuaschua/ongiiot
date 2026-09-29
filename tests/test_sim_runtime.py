"""Simulator runtime against fakes: 1 Hz stream, injections, injection_log, startup checks (AD-1, AD-3, AD-9, AD-15)."""

from __future__ import annotations

import json
import os
import signal
import statistics as st
from datetime import UTC, datetime, timedelta


from facility.control import cancel_of, new_injection, to_item
from facility.model import load
from sim import main as sim_main
from sim.main import Simulator
from tests.sim_fakes import FakeClock, FakeSiteWise, FakeTable

STAGE = "poc-dj"
F = load(stage=STAGE)
T0 = 1_790_000_000
ENV = {"STAGE": STAGE, "SIM_CONTROL_TABLE": "sim_control-poc-dj", "INJECTION_LOG_TABLE": "injection_log-poc-dj"}


def make(control=(), t0=T0 + 0.25, log=None, sitewise=None, **kw):
    clock = FakeClock(t0)
    fake = sitewise or FakeSiteWise()
    ctrl = FakeTable([to_item(i) for i in control])
    log = log if log is not None else FakeTable()
    sim = Simulator(F, fake, ctrl, log, clock=clock, sleep=clock.sleep, **kw)
    return sim, fake, ctrl, log, clock


def leak(pipe="P-007", start=T0, minutes=10, iid="01LEAK", **params):
    s = datetime.fromtimestamp(start, UTC)
    return new_injection(iid, "leak", pipe, s, s + timedelta(minutes=minutes), params=params, facility=F)


def series(fake, pipe, m):
    return [v["value"]["doubleValue"] for v in fake.values().get(F.alias(pipe, m), [])]


def test_sixty_seconds_every_raw_alias_one_good_value_per_second():
    sim, fake, *_ = make()
    assert sim.run(max_ticks=60) == 60
    sent = fake.values()
    raw = {F.alias(p.id, m): p.contracts[m] for p in F.pipes for m in p.sensors}
    assert len(raw) == 144
    assert set(sent) == set(raw)
    for alias, vals in sent.items():
        ts = [v["timestamp"]["timeInSeconds"] for v in vals]
        assert ts == list(range(T0, T0 + 60)), alias
        assert all(v["quality"] == "GOOD" for v in vals)
        c = raw[alias]
        assert all(c.min <= v["value"]["doubleValue"] <= c.max for v in vals), alias
    assert all(len(call) <= 10 and all(len(e["propertyValues"]) <= 10 for e in call) for call in fake.calls)
    assert not any("/derived/" in a or "/attr/" in a for a in sent)


def test_leak_acceptance_p007_only():
    base_sim, base, *_ = make()
    base_sim.run(max_ticks=120)
    sim, fake, _, log, _ = make(control=[leak()])
    sim.run(max_ticks=120)

    imb_n = [a - b for a, b in zip(series(base, "P-007", "flow_in"), series(base, "P-007", "flow_out"))]
    imb_l = [a - b for a, b in zip(series(fake, "P-007", "flow_in"), series(fake, "P-007", "flow_out"))]
    band = abs(st.mean(imb_n)) + 6 * st.pstdev(imb_n)
    assert min(imb_l[-60:]) > band
    ch4_n, ch4_l = series(base, "P-007", "methane_ppm"), series(fake, "P-007", "methane_ppm")
    assert min(ch4_l[-30:]) > max(ch4_n) + 6 * st.pstdev(ch4_n)
    assert st.mean(series(fake, "P-007", "pressure_out")[-60:]) < st.mean(series(base, "P-007", "pressure_out")[-60:])

    changed = {a for a in base.values() if base.values()[a] != fake.values()[a]}
    assert changed == {F.alias("P-007", m) for m in ("flow_out", "pressure_out", "methane_ppm")}
    assert list(log.items) == ["01LEAK"]


def test_injection_log_exactly_once_across_restart():
    log = FakeTable()
    inj = leak(minutes=30)
    sim, _, _, log, _ = make(control=[inj], log=log)
    sim.run(max_ticks=20)
    assert log.writes == 1
    row = log.items["01LEAK"]
    assert row["pipe_id"] == "P-007" and row["type"] == "leak" and row["stage"] == STAGE
    assert float(row["params"]["rate_kg_s"]) > 0 and row["applied_at"] == "2026-09-21T14:13:20.000Z"
    # restart later: same log table, new process state
    sim2, fake2, *_ = make(control=[inj], log=log, t0=T0 + 100.7)
    sim2.run(max_ticks=10)
    assert log.writes == 1 and len(log.items) == 1
    # restart resumes at wall-clock now; no backfill
    assert {v["timestamp"]["timeInSeconds"] for vs in fake2.values().values() for v in vs} == set(range(T0 + 100, T0 + 110))


def test_cancel_stops_injection_at_cancellers_end():
    inj = leak(minutes=30)
    cancel = cancel_of(inj, "01CANCEL", datetime.fromtimestamp(T0 + 30, UTC))
    sim, fake, _, log, _ = make(control=[inj, cancel])
    sim.run(max_ticks=120)
    ch4 = series(fake, "P-007", "methane_ppm")
    assert max(ch4[:30]) > 100  # leak ran
    imb = [a - b for a, b in zip(series(fake, "P-007", "flow_in"), series(fake, "P-007", "flow_out"))]
    assert st.mean(imb[5:30]) > 1.0
    assert abs(st.mean(imb[30:])) < 0.2  # stopped at the canceller's end_at
    assert list(log.items) == ["01LEAK"]


# -- entry point --------------------------------------------------------------------------------


def run_main(env=ENV, sitewise=None, max_ticks=3, **kw):
    clock = FakeClock(T0)
    fake = sitewise or FakeSiteWise()
    code = sim_main.main(
        env,
        sitewise=fake,
        control_table=FakeTable(),
        log_table=FakeTable(),
        clock=clock,
        sleep=clock.sleep,
        max_ticks=max_ticks,
        install_signals=False,
        **kw,
    )
    return code, fake


def logs(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_main_checks_every_alias_then_runs(capsys):
    code, fake = run_main()
    assert code == 0
    assert sorted(fake.described) == sorted(F.alias(p.id, m) for p in F.pipes for m in p.sensors)
    lines = logs(capsys)
    assert all(line["stage"] == STAGE for line in lines)
    assert any(line["msg"] == "simulator started" and line["config_hash"] == F.config_hash() for line in lines)


def test_main_refuses_to_start_on_unassociated_alias(capsys):
    alias = F.alias("P-012", "temperature")
    code, fake = run_main(sitewise=FakeSiteWise(unassociated=[alias]))
    assert code == 3
    assert fake.calls == []
    err = [line for line in logs(capsys) if line["level"] == "ERROR"]
    assert err and alias in err[0]["error"]


def all_aliases():
    return [F.alias(p.id, m) for p in F.pipes for m in p.sensors]


def test_fresh_stage_starts_and_verifies_after_first_write(capsys):
    # A new stage: no series exist until written; after the first flush all are associated.
    fake = FakeSiteWise(pending=all_aliases())
    code, fake = run_main(sitewise=fake, max_ticks=12)
    assert code == 0
    assert fake.calls  # it wrote
    lines = logs(capsys)
    assert any(line["msg"] == "aliases pending first write" and line["count"] == len(all_aliases()) for line in lines)
    assert not [line for line in lines if line["level"] == "ERROR"]
    # each alias described twice: at startup (pending) and after the first write
    assert sorted(fake.described) == sorted(all_aliases() * 2)


def test_sigterm_stops_cleanly_and_flushes():
    clock = FakeClock(T0)
    fake = FakeSiteWise()

    def term(c):
        if int(c.t) == T0 + 3:
            os.kill(os.getpid(), signal.SIGTERM)

    clock.on_sleep = term
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        code = sim_main.main(
            ENV, sitewise=fake, control_table=FakeTable(), log_table=FakeTable(), clock=clock, sleep=clock.sleep
        )
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])
    assert code == 0
    ts = {v["timestamp"]["timeInSeconds"] for vs in fake.values().values() for v in vs}
    assert ts == set(range(T0, T0 + 3))
