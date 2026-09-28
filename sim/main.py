"""Simulator entry point: ``python -m sim.main`` (AD-1, AD-3, AD-9, AD-15).

Environment:
    STAGE                 ``poc`` or ``poc-<initials>`` (required)
    SIM_CONTROL_TABLE     sim_control table name (required)
    INJECTION_LOG_TABLE   injection_log table name (required)
    FLUSH_S               seconds between SiteWise flushes (default 5)
    POLL_S                seconds between sim_control scans, at most 5 (default 5)
    SIM_SEED              integer seed for the process model (default 0)
    FACILITY_PATH         facility.yaml path (default: the bundled one)

Only this module touches the clock and AWS; both are injectable for tests.
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from facility.model import DEFAULT_PATH, Facility, FacilityError, load
from sim import physics
from sim.control import POLL_MAX_S, ControlPoller
from sim.writer import RawWriter, WriteError

LOG = logging.getLogger("sim")


class ConfigError(ValueError):
    pass


class StartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    stage: str
    control_table: str
    log_table: str
    flush_s: float = 5.0
    poll_s: float = POLL_MAX_S
    seed: int = 0
    facility_path: str = str(DEFAULT_PATH)


def settings_from_env(env: Mapping[str, str]) -> Settings:
    missing = [k for k in ("STAGE", "SIM_CONTROL_TABLE", "INJECTION_LOG_TABLE") if not env.get(k)]
    if missing:
        raise ConfigError(f"missing environment variables: {missing}")
    try:
        flush_s = float(env.get("FLUSH_S", "5"))
        poll_s = float(env.get("POLL_S", str(POLL_MAX_S)))
        seed = int(env.get("SIM_SEED", "0"))
    except ValueError as exc:
        raise ConfigError(f"invalid numeric setting: {exc}") from exc
    if not 1 <= flush_s <= 60:
        raise ConfigError("FLUSH_S must be within [1, 60]")
    if not 0 < poll_s <= POLL_MAX_S:
        raise ConfigError(f"POLL_S must be within (0, {POLL_MAX_S:g}]")
    return Settings(
        stage=env["STAGE"],
        control_table=env["SIM_CONTROL_TABLE"],
        log_table=env["INJECTION_LOG_TABLE"],
        flush_s=flush_s,
        poll_s=poll_s,
        seed=seed,
        facility_path=env.get("FACILITY_PATH") or str(DEFAULT_PATH),
    )


def raw_aliases(facility: Facility) -> list[str]:
    return [facility.alias(p.id, m) for p in facility.pipes for m in p.sensors]


def check_aliases(client: Any, aliases: list[str], *, allow_pending: bool = False) -> list[str]:
    """AD-1: every raw alias must be associated with an asset property; refuse otherwise.

    SiteWise creates an asset property's time series only on its first write, so on a fresh
    stage every alias is "not found". With ``allow_pending`` those aliases are returned as
    pending (checked again after the first write); an existing but unassociated series is
    still refused. Returns the pending aliases.
    """
    bad: list[str] = []
    pending: list[str] = []
    for alias in aliases:
        try:
            ts = client.describe_time_series(alias=alias)
        except Exception as exc:  # every failure becomes a StartupError
            response = getattr(exc, "response", None)
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            if code == "ResourceNotFoundException":
                (pending if allow_pending else bad).append(alias)
                continue
            raise StartupError(f"DescribeTimeSeries failed for {alias} ({code or type(exc).__name__}): {exc}") from exc
        if not ts.get("assetId") or not ts.get("propertyId"):
            bad.append(alias)
    if bad:
        raise StartupError(f"aliases not associated with an asset property: {', '.join(bad)}")
    return pending


# -- JSON logs -------------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    def __init__(self, static: Mapping[str, Any]):
        super().__init__()
        self._static = dict(static)

    def format(self, record: logging.LogRecord) -> str:
        line = {
            **getattr(record, "fields", {}),  # first, so the reserved keys below win
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "msg": record.getMessage(),
            **self._static,
        }
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(static: Mapping[str, Any]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(static))
    LOG.handlers[:] = [handler]
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


# -- runtime -----------------------------------------------------------------------------------


class Simulator:
    def __init__(
        self,
        facility: Facility,
        sitewise: Any,
        control_table: Any,
        log_table: Any,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        seed: int = 0,
        flush_s: float = 5.0,
        poll_s: float = POLL_MAX_S,
        verify_after_first_write: Callable[[], None] | None = None,
    ):
        self.facility = facility
        # Called once after the first successful flush; a StartupError stops the simulator.
        self._verify = verify_after_first_write
        self.failed = False
        self.writer = RawWriter(sitewise, allowed=raw_aliases(facility), sleep=sleep)
        self.poller = ControlPoller(control_table, log_table, facility, poll_s=poll_s)
        self._clock = clock
        self._sleep = sleep
        self._flush_s = flush_s
        self._states = {p.id: physics.initial_state(p, seed) for p in facility.pipes}
        self._aliases = {(p.id, m): facility.alias(p.id, m) for p in facility.pipes for m in p.sensors}
        self._resolved: dict[str, physics.Active] = {}
        self._stop = threading.Event()
        self.last_t: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def tick(self, t: int) -> None:
        now = datetime.fromtimestamp(t, UTC)
        self.poller.poll(t)
        by_pipe: dict[str, list[physics.Active]] = {}
        live = set()
        for inj in self.poller.active(now):
            if inj.pipe_id not in self._states:
                continue
            live.add(inj.injection_id)
            act = self._resolved.get(inj.injection_id)
            if act is None:
                act = physics.activate(self._states[inj.pipe_id], inj)
                self._resolved[inj.injection_id] = act
            self.poller.log_first_apply(inj, act.params, now, self.facility.stage)
            by_pipe.setdefault(inj.pipe_id, []).append(act)
        self._resolved = {k: v for k, v in self._resolved.items() if k in live}

        for pipe_id, state in self._states.items():
            state, samples = physics.step(state, t, by_pipe.get(pipe_id, ()))
            self._states[pipe_id] = state
            for s in samples:
                self.writer.add(self._aliases[(pipe_id, s.measurement)], s.t, s.value, s.quality)
        self.last_t = t

    def flush(self) -> None:
        try:
            n = self.writer.flush()
        except WriteError as exc:
            LOG.error("sitewise flush failed; values dropped", extra={"fields": {"error": str(exc)}})
            return
        LOG.debug("flushed", extra={"fields": {"values": n}})
        if n and self._verify is not None:
            verify, self._verify = self._verify, None
            try:
                verify()
            except StartupError as exc:
                LOG.error("refusing to continue", extra={"fields": {"error": str(exc)}})
                self.failed = True
                self.stop()

    def run(self, max_ticks: int | None = None) -> int:
        """Emit one sample set per wall-clock second until stopped. Returns ticks run."""
        ticks = 0
        last_flush: int | None = None
        LOG.info("simulator started", extra={"fields": {"pipes": len(self._states), "aliases": len(self._aliases)}})
        while not self._stop.is_set() and (max_ticks is None or ticks < max_ticks):
            now = self._clock()
            t = math.floor(now)
            if self.last_t is not None and t <= self.last_t:
                self._sleep(max(0.0, self.last_t + 1 - now))
                continue
            self.tick(t)
            ticks += 1
            if last_flush is None:
                last_flush = t
            if t - last_flush >= self._flush_s:
                self.flush()
                last_flush = t
            if not self._stop.is_set():
                self._sleep(max(0.0, t + 1 - self._clock()))
        self.flush()
        LOG.info("simulator stopped", extra={"fields": {"ticks": ticks}})
        return ticks


def _aws(settings: Settings) -> tuple[Any, Any, Any, Any]:
    import boto3
    from botocore.config import Config

    # SiteWise writes: no SDK retries; RawWriter is the only retry layer, with a bounded backoff (sim/writer.py).
    sitewise = boto3.client("iotsitewise", config=Config(retries={"mode": "standard", "max_attempts": 1}))
    # Startup alias check: off the 1 Hz loop, so SDK retries absorb throttling of the per-alias describes.
    startup = boto3.client("iotsitewise", config=Config(retries={"mode": "standard", "max_attempts": 5}))
    ddb = boto3.resource("dynamodb", config=Config(retries={"mode": "standard", "max_attempts": 5}))
    return sitewise, ddb.Table(settings.control_table), ddb.Table(settings.log_table), startup


def main(
    env: Mapping[str, str] | None = None,
    *,
    sitewise: Any = None,
    control_table: Any = None,
    log_table: Any = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    max_ticks: int | None = None,
    install_signals: bool = True,
) -> int:
    env = os.environ if env is None else env
    configure_logging({"stage": env.get("STAGE", ""), "component": "simulator"})
    try:
        settings = settings_from_env(env)
        facility = load(settings.facility_path, stage=settings.stage)
    except (ConfigError, FacilityError) as exc:
        LOG.error("configuration error", extra={"fields": {"error": str(exc)}})
        return 2
    configure_logging({"stage": settings.stage, "component": "simulator", "config_hash": facility.config_hash()})

    startup_client = sitewise
    if sitewise is None or control_table is None or log_table is None:
        aws = _aws(settings)
        startup_client = sitewise or aws[3]
        sitewise = sitewise or aws[0]
        control_table = control_table or aws[1]
        log_table = log_table or aws[2]

    try:
        pending = check_aliases(startup_client, raw_aliases(facility), allow_pending=True)
    except StartupError as exc:
        LOG.error("refusing to start", extra={"fields": {"error": str(exc)}})
        return 3
    verify = None
    if pending:
        # Fresh stage: these series appear on the first write; then all must be associated.
        LOG.info("aliases pending first write", extra={"fields": {"count": len(pending)}})
        verify = lambda: check_aliases(startup_client, pending)  # noqa: E731

    sim = Simulator(
        facility,
        sitewise,
        control_table,
        log_table,
        clock=clock,
        sleep=sleep,
        seed=settings.seed,
        flush_s=settings.flush_s,
        poll_s=settings.poll_s,
        verify_after_first_write=verify,
    )
    if install_signals and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: sim.stop())
    sim.run(max_ticks=max_ticks)
    return 3 if sim.failed else 0


if __name__ == "__main__":
    sys.exit(main())
