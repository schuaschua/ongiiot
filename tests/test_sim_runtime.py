"""Simulator runtime against fakes: 1 Hz stream, injections, injection_log, startup checks (AD-1, AD-3, AD-9, AD-15)."""

from __future__ import annotations

import json
import os
import signal
import statistics as st
from datetime import UTC, datetime, timedelta

import pytest

from facility.control import cancel_of, new_injection, to_item
from facility.model import load
from sim import main as sim_main
from sim.main import ConfigError, Simulator, settings_from_env
from tests.sim_fakes import FakeClientError, FakeClock, FakeSiteWise, FakeTable

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


def test_flush_cadence():
    sim, fake, *_ = make(flush_s=5)
    sim.run(max_ticks=11)
    # flushes after t0+5 (6 values per alias) and t0+10 (5 values); the final flush on exit is empty
    flushed_seconds = {v["timestamp"]["timeInSeconds"] for c in fake.calls for e in c for v in e["propertyValues"]}
    assert flushed_seconds == set(range(T0, T0 + 11))
    assert len(fake.calls) == 2 * 15  # 144 entries per flush -> 15 requests of <= 10 entries
    first = {v["timestamp"]["timeInSeconds"] for c in fake.calls[:15] for e in c for v in e["propertyValues"]}
    assert first == set(range(T0, T0 + 6))


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


def test_injection_not_applied_outside_window():
    sim, fake, _, log, _ = make(control=[leak(start=T0 + 3600)])
    sim.run(max_ticks=30)
    base_sim, base, *_ = make()
    base_sim.run(max_ticks=30)
    assert fake.values() == base.values()
    assert log.items == {}


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


def test_polls_at_most_every_five_seconds_read_only():
    sim, _, ctrl, *_ = make()
    sim.run(max_ticks=60)
    assert ctrl.scans == 12 and ctrl.writes == 0


def test_injection_added_mid_run_picked_up_within_poll_interval():
    sim, _, ctrl, log, clock = make()

    def add_on_sleep(c):
        if int(c.t) == T0 + 7 and "01LEAK" not in ctrl.items:
            ctrl.items["01LEAK"] = to_item(leak())

    clock.on_sleep = add_on_sleep
    sim.run(max_ticks=20)
    assert "01LEAK" in log.items
    applied = datetime.strptime(log.items["01LEAK"]["applied_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert applied.timestamp() - (T0 + 7) <= 5


def test_invalid_control_items_are_ignored():
    bad = to_item(leak()) | {"type": "burst", "pk": "01BAD"}
    sim, fake, ctrl, log, _ = make()
    ctrl.items["01BAD"] = bad
    sim.run(max_ticks=5)
    assert log.items == {} and len(fake.values()) == 144


def test_control_scan_paginates():
    items = [leak(iid=f"01L{i}", pipe=p) for i, p in enumerate(("P-001", "P-002", "P-003"))]
    sim, _, ctrl, log, _ = make(control=items)
    ctrl.page_size = 1
    sim.run(max_ticks=2)
    assert set(log.items) == {"01L0", "01L1", "01L2"}


def test_clock_going_backwards_never_resends_a_timestamp():
    sim, fake, _, _, clock = make()
    jumped = []

    def back(c):
        if int(c.t) == T0 + 5 and not jumped:
            jumped.append(1)
            c.t -= 3

    clock.on_sleep = back
    sim.run(max_ticks=10)
    for vals in fake.values().values():
        ts = [v["timestamp"]["timeInSeconds"] for v in vals]
        assert ts == sorted(set(ts))


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


def test_stops_after_first_write_when_an_alias_has_no_asset_property(capsys):
    orphan = F.alias("P-012", "temperature")
    fake = FakeSiteWise(pending=[a for a in all_aliases() if a != orphan], orphan=[orphan])
    code, fake = run_main(sitewise=fake, max_ticks=30)
    assert code == 3
    lines = logs(capsys)
    err = [line for line in lines if line["level"] == "ERROR"]
    assert err[0]["msg"] == "refusing to continue" and orphan in err[0]["error"]
    # it stopped at the first flush (after 6 ticks), not after 30
    assert any(line["msg"] == "simulator stopped" and line["ticks"] < 30 for line in lines)


def test_still_missing_after_first_write_is_refused(capsys):
    alias = F.alias("P-012", "temperature")
    code, _ = run_main(sitewise=FakeSiteWise(missing=[alias]), max_ticks=12)
    assert code == 3
    err = [line for line in logs(capsys) if line["level"] == "ERROR"]
    assert err[0]["msg"] == "refusing to continue" and alias in err[0]["error"]


def test_main_config_errors(capsys):
    code, _ = run_main(env={"STAGE": STAGE})
    assert code != 0
    assert "SIM_CONTROL_TABLE" in logs(capsys)[0]["error"]
    with pytest.raises(ConfigError):
        settings_from_env(ENV | {"POLL_S": "6"})
    s = settings_from_env(ENV | {"FLUSH_S": "2", "SIM_SEED": "7"})
    assert (s.flush_s, s.poll_s, s.seed, s.stage) == (2.0, 5.0, 7, STAGE)
    assert settings_from_env(ENV).flush_s == 5.0


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


def test_non_retryable_flush_failure_is_logged_and_stream_continues(caplog):
    fake = FakeSiteWise(entry_errors=["InvalidRequestException"])
    sim, fake, *_ = make(sitewise=fake, flush_s=5)
    assert sim.run(max_ticks=12) == 12
    assert any(r.levelname == "ERROR" and "flush failed" in r.getMessage() for r in caplog.records)
    later = {v["timestamp"]["timeInSeconds"] for vs in fake.values().values() for v in vs if v["timestamp"]["timeInSeconds"] > T0 + 5}
    assert later == set(range(T0 + 6, T0 + 12))


def test_control_scan_failure_keeps_active_injection_and_loop_running():
    sim, fake, ctrl, log, _ = make(control=[leak(minutes=30)])
    sim.run(max_ticks=10)
    ctrl.fail_scans = 3
    assert sim.run(max_ticks=30) == 30
    assert ctrl.fail_scans == 0
    imb = [a - b for a, b in zip(series(fake, "P-007", "flow_in"), series(fake, "P-007", "flow_out"))]
    assert min(imb[10:]) > 1.0  # the leak stayed applied through the failed scans
    assert log.writes == 1


def test_transient_injection_log_failure_retried_and_written_once():
    log = FakeTable()
    log.fail_puts = 2
    sim, *_ = make(control=[leak()], log=log)
    sim.run(max_ticks=10)
    assert log.fail_puts == 0 and log.writes == 1 and list(log.items) == ["01LEAK"]
    assert log.items["01LEAK"]["applied_at"] == "2026-09-21T14:13:22.000Z"  # third tick


def test_main_refuses_to_start_when_describe_fails(capsys):
    class Denied(FakeSiteWise):
        def describe_time_series(self, alias):
            raise FakeClientError("AccessDeniedException", "no")

    code, fake = run_main(sitewise=Denied())
    assert code == 3 and fake.calls == []
    err = [line for line in logs(capsys) if line["level"] == "ERROR"]
    assert err[0]["msg"] == "refusing to start" and "AccessDeniedException" in err[0]["error"]


def test_json_log_reserved_keys_win():
    import logging

    fmt = sim_main.JsonFormatter({"stage": STAGE, "config_hash": "h"})
    rec = logging.LogRecord("sim", logging.INFO, __file__, 1, "real", None, None)
    rec.fields = {"msg": "x", "level": "x", "ts": "x", "stage": "x", "config_hash": "x", "pipe_id": "P-007"}
    line = json.loads(fmt.format(rec))
    assert (line["msg"], line["level"], line["stage"], line["config_hash"]) == ("real", "INFO", STAGE, "h")
    assert line["ts"] != "x" and line["pipe_id"] == "P-007"


# -- effective_end on injection_log (Story 4.1) ------------------------------------------


def test_cancel_after_apply_sets_effective_end_once():
    inj = leak()
    sim, _, ctrl, log, clock = make(control=[inj])
    sim.run(max_ticks=3)
    assert "01LEAK" in log.items and "effective_end" not in log.items["01LEAK"]
    stop = datetime.fromtimestamp(T0 + 3, UTC)
    ctrl.items["01CANC"] = to_item(cancel_of(inj, "01CANC", stop))
    sim.run(max_ticks=15)
    row = log.items["01LEAK"]
    assert row["effective_end"] == to_item(cancel_of(inj, "x", stop))["end_at"]
    assert row["end_at"] == to_item(inj)["end_at"]  # planned end unchanged
    assert len(log.updates) == 1  # once, not on every poll


def test_effective_end_conditional_failure_retries_within_grace_then_gives_up():
    inj = leak()
    stop = datetime.fromtimestamp(T0 + 3, UTC)
    row = {"pk": "01LEAK", "effective_end": "already"}
    from sim.control import ControlPoller

    log = FakeTable([row])
    poller = ControlPoller(FakeTable([to_item(inj), to_item(cancel_of(inj, "01CANC", stop))]), log, F)
    for t in range(T0 + 5, T0 + 30, 5):
        poller.poll(t)
    assert len(log.updates) == 5 and log.items["01LEAK"]["effective_end"] == "already"  # retried each poll
    poller.poll(T0 + 3 + 601)  # past the 10-minute grace: last attempt, then given up
    poller.poll(T0 + 3 + 700)
    assert len(log.updates) == 6


def test_effective_end_generic_error_retries_then_succeeds():
    inj = leak()
    sim, _, ctrl, log, _ = make(control=[inj])
    sim.run(max_ticks=3)
    ctrl.items["01CANC"] = to_item(cancel_of(inj, "01CANC", datetime.fromtimestamp(T0 + 3, UTC)))
    log.fail_updates = 1
    sim.run(max_ticks=15)
    assert len(log.updates) == 2 and "effective_end" in log.items["01LEAK"]


def test_effective_end_older_than_a_day_is_skipped():
    inj = leak(start=T0 - 2 * 86400)
    sim, _, _, log, _ = make(control=[inj, cancel_of(inj, "01CANC", datetime.fromtimestamp(T0 - 2 * 86400 + 60, UTC))])
    sim.run(max_ticks=10)
    assert log.updates == []


def test_cancel_before_apply_writes_no_log_row():
    inj = leak(start=T0 + 60)
    sim, _, _, log, _ = make(control=[inj, cancel_of(inj, "01CANC", datetime.fromtimestamp(T0, UTC))])
    sim.run(max_ticks=10)
    assert log.items == {}
