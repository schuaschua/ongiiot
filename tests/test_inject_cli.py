"""ops/inject.py: validated sim_control writes only (AD-15)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from facility.control import from_item
from facility.model import load
from ops import inject
from tests.sim_fakes import FakeTable

NOW = datetime(2026, 9, 28, 10, 0, 0, 123456, tzinfo=UTC)
ENV = {"STAGE": "poc-dj", "SIM_CONTROL_TABLE": "sim_control-poc-dj"}


def cli(argv, table=None, ids=("01AAA", "01BBB", "01CCC")):
    table = table if table is not None else FakeTable()
    it = iter(ids)
    code = inject.main(argv, env=ENV, table=table, now=lambda: NOW, new_id=lambda: next(it))
    return code, table


def test_leak_writes_one_immutable_item(capsys):
    code, table = cli(["leak", "--pipe", "P-007", "--minutes", "20"])
    assert code == 0
    item = table.items["01AAA"]
    assert item == {
        "pk": "01AAA",
        "type": "leak",
        "pipe_id": "P-007",
        "params": {},
        "start_at": "2026-09-28T10:00:00.123Z",
        "end_at": "2026-09-28T10:20:00.123Z",
    }
    assert json.loads(capsys.readouterr().out)["pk"] == "01AAA"
    from_item(item, load(stage="poc-dj"))  # valid v1


def test_leak_rate_and_drift_params():
    _, table = cli(["leak", "--pipe", "P-007", "--minutes", "5", "--rate-kg-s", "2.5"])
    assert float(table.items["01AAA"]["params"]["rate_kg_s"]) == 2.5
    _, table = cli(["dq_drift", "--pipe", "P-003", "--measurement", "temperature", "--minutes", "5", "--per-min", "0.5"])
    item = table.items["01AAA"]
    assert item["measurement"] == "temperature" and float(item["params"]["per_min"]) == 0.5


@pytest.mark.parametrize("type_", ["dq_flatline", "dq_stuck", "dq_gap"])
def test_dq_types(type_):
    code, table = cli([type_, "--pipe", "P-003", "--measurement", "flow_out", "--minutes", "1"])
    assert code == 0 and table.items["01AAA"]["type"] == type_


@pytest.mark.parametrize(
    "argv",
    [
        ["dq_stuck", "--pipe", "P-003", "--minutes", "5"],  # dq type without measurement
        ["leak", "--pipe", "P-999", "--minutes", "5"],  # unknown pipe
        ["dq_gap", "--pipe", "P-003", "--measurement", "viscosity", "--minutes", "5"],
        ["leak", "--pipe", "P-007", "--minutes", "0"],
        ["leak", "--pipe", "P-007", "--minutes", "5", "--rate-kg-s", "-1"],
        ["leak", "--pipe", "P-007", "--measurement", "flow_in", "--minutes", "5"],
    ],
)
def test_invalid_inject_refused_without_write(argv, capsys):
    code, table = cli(argv)
    assert code != 0 and table.items == {} and table.writes == 0
    assert "error" in capsys.readouterr().err


def test_unknown_type_refused_without_write():
    table = FakeTable()
    with pytest.raises(SystemExit) as exc:
        cli(["burst", "--pipe", "P-007", "--minutes", "5"], table=table)
    assert exc.value.code != 0 and table.writes == 0


def test_cancel_writes_superseding_item():
    code, table = cli(["leak", "--pipe", "P-007", "--minutes", "20"])
    code, table = cli(["cancel", "01AAA"], table=table, ids=("01CAN",))
    assert code == 0
    assert table.items["01AAA"]["end_at"] == "2026-09-28T10:20:00.123Z"  # original untouched
    c = table.items["01CAN"]
    assert c["supersedes"] == "01AAA" and c["start_at"] == c["end_at"] == "2026-09-28T10:00:00.123Z"
    assert c["type"] == "leak" and c["pipe_id"] == "P-007"


def test_cancel_unknown_or_cancel_item_refused():
    code, table = cli(["cancel", "01NOPE"])
    assert code != 0 and table.writes == 0
    _, table = cli(["leak", "--pipe", "P-007", "--minutes", "20"])
    cli(["cancel", "01AAA"], table=table, ids=("01CAN",))
    code, _ = cli(["cancel", "01CAN"], table=table, ids=("01X",))
    assert code != 0 and table.writes == 2


def test_missing_table_env_refused():
    code = inject.main(["leak", "--pipe", "P-007", "--minutes", "5"], env={"STAGE": "poc"})
    assert code != 0


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        inject.main(["--help"], env=ENV)
    assert exc.value.code == 0
    assert "leak" in capsys.readouterr().out


class AwsFailTable(FakeTable):
    def __init__(self, op, code):
        super().__init__()
        self.op, self.code = op, code

    def _raise(self, name):
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": self.code, "Message": "boom"}}, name)

    def get_item(self, Key, **kwargs):
        if self.op == "get":
            self._raise("GetItem")
        return super().get_item(Key, **kwargs)

    def put_item(self, Item, ConditionExpression=None):
        if self.op == "put":
            self._raise("PutItem")
        return super().put_item(Item, ConditionExpression)


@pytest.mark.parametrize(
    ("op", "code", "argv"),
    [
        ("put", "ResourceNotFoundException", ["leak", "--pipe", "P-007", "--minutes", "5"]),
        ("put", "AccessDeniedException", ["leak", "--pipe", "P-007", "--minutes", "5"]),
        ("put", "ConditionalCheckFailedException", ["leak", "--pipe", "P-007", "--minutes", "5"]),
        ("get", "AccessDeniedException", ["cancel", "01AAA"]),
    ],
)
def test_aws_errors_exit_2_without_traceback(op, code, argv, capsys):
    rc, table = cli(argv, table=AwsFailTable(op, code))
    assert rc == 2 and table.writes == 0
    assert code in capsys.readouterr().err


@pytest.mark.parametrize("minutes", ["inf", "nan", "1e30", str(7 * 24 * 60 + 1)])
def test_minutes_must_be_finite_and_at_most_seven_days(minutes):
    code, table = cli(["leak", "--pipe", "P-007", "--minutes", minutes])
    assert code == 2 and table.writes == 0
    code, _ = cli(["leak", "--pipe", "P-007", "--minutes", str(7 * 24 * 60)])
    assert code == 0


def test_facility_path_is_honoured(capsys):
    from facility.model import DEFAULT_PATH

    argv = ["leak", "--pipe", "P-007", "--minutes", "5"]
    table = FakeTable()
    code = inject.main(argv, env=ENV | {"FACILITY_PATH": "no/such/facility.yaml"}, table=table, now=lambda: NOW, new_id=lambda: "01A")
    assert code == 2 and table.writes == 0 and "no/such/facility.yaml" in capsys.readouterr().err
    code = inject.main(argv, env=ENV | {"FACILITY_PATH": str(DEFAULT_PATH)}, table=table, now=lambda: NOW, new_id=lambda: "01A")
    assert code == 0 and table.writes == 1


# -- list (Story 4.1) --------------------------------------------------------------------

def _listing_table():
    from datetime import timedelta

    from facility.control import cancel_of, new_injection, to_item

    f = load(stage="poc-dj")
    now = NOW.replace(microsecond=123000)
    m = lambda n: timedelta(minutes=n)  # noqa: E731
    leak = new_injection("01LEAK", "leak", "P-007", now - m(5), now + m(15), facility=f)
    flat = new_injection("01FLAT", "dq_flatline", "P-003", now - m(20), now + m(10), measurement="flow_in", facility=f)
    cancel = cancel_of(flat, "01CANC", now - m(8))
    stuck = new_injection("01STUCK", "dq_stuck", "P-015", now - m(40), now - m(10), measurement="temperature", facility=f)
    old = new_injection("01OLD", "leak", "P-007", now - m(300), now - m(200), facility=f)
    return FakeTable([to_item(i) for i in (leak, flat, cancel, stuck, old)], page_size=2)


def test_list_json_folds_cancels_and_classifies(capsys):
    table = _listing_table()
    before = dict(table.items)
    code, _ = cli(["list", "--json"], table=table)
    assert code == 0 and table.items == before and table.writes == 0  # read-only
    rows = {r["id"]: r for r in map(json.loads, capsys.readouterr().out.splitlines())}
    assert set(rows) == {"01LEAK", "01FLAT", "01STUCK"}  # cancel folded, old one outside --recent-min
    assert rows["01LEAK"]["state"] == "active"
    assert rows["01FLAT"] == {
        "id": "01FLAT",
        "type": "dq_flatline",
        "pipe": "P-003",
        "measurement": "flow_in",
        "start": "2026-09-28T09:40:00.123Z",
        "planned_end": "2026-09-28T10:10:00.123Z",
        "effective_end": "2026-09-28T09:52:00.123Z",
        "state": "recent",
    }
    assert rows["01STUCK"]["state"] == "recent"
    assert rows["01STUCK"]["effective_end"] == rows["01STUCK"]["planned_end"]


def test_list_recent_window_and_table(capsys):
    code, _ = cli(["list", "--recent-min", "400"], table=_listing_table())
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and out[0].split() == ["id", "type", "pipe", "measurement", "start", "effective_end", "planned_end", "state"]
    assert {line.split()[0] for line in out[1:]} == {"01LEAK", "01FLAT", "01STUCK", "01OLD"}


def test_list_empty(capsys):
    code, _ = cli(["list"])
    assert code == 0 and capsys.readouterr().out == "no injections\n"
