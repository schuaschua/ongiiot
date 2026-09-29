"""ops/reset.py: invoke the reset, then delete every sim_control item (Story 4.2, AD-15)."""

from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ClientError

from ops import reset
from tests.sim_fakes import FakeTable

ENV = {"STAGE": "poc-dj", "SIM_CONTROL_TABLE": "sim-control-poc-dj"}
INJ = [
    {"pk": "01AAA", "type": "leak", "pipe_id": "P-007"},
    {"pk": "01BBB", "type": "dq_gap", "pipe_id": "P-012", "measurement": "flow_out"},
]


class FakeLambda:
    def __init__(self, summary=None, function_error=None, raise_code=None, table=None):
        self.summary = summary if summary is not None else {"action": "reset", "run_id": "r", "closed": [], "conflicts": []}
        self.function_error, self.raise_code, self.table = function_error, raise_code, table
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.table is not None:
            assert self.table.writes == 0  # incidents are closed before any injection is deleted
        if self.raise_code:
            raise ClientError({"Error": {"Code": self.raise_code, "Message": "boom"}}, "Invoke")
        resp = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.summary).encode())}
        if self.function_error:
            resp["FunctionError"] = self.function_error
            resp["Payload"] = io.BytesIO(b'{"errorMessage":"boom"}')
        return resp


def test_reset_closes_incidents_then_deletes_every_injection(capsys):
    table = FakeTable(INJ, page_size=1)  # paginated scan
    closed = [{"pipe": "P-007", "incident_id": "01I7"}, {"pipe": "P-012", "incident_id": "01I12"}]
    lam = FakeLambda({"action": "reset", "run_id": "2026-09-29T01:00:00.000Z", "closed": closed, "conflicts": ["P-012"]}, table=table)
    code = reset.main(["--yes"], env=ENV, table=table, lambda_client=lam)
    assert code == 0 and table.items == {}
    ((call),) = lam.calls
    assert call["FunctionName"] == "detection-poc-dj" and call["InvocationType"] == "RequestResponse"
    assert json.loads(call["Payload"]) == {"action": "reset"}
    captured = capsys.readouterr()
    out = captured.out
    assert "P-012" in captured.err and "warning" in captured.err
    assert "01I7" in out and "01I12" in out and "closed 2 incidents, deleted 2 injections" in out
    assert "about 5 minutes" in out


def test_nothing_to_reset(capsys):
    code = reset.main(["--yes", "--stage", "poc-ab", "--table", "t"], env={}, table=FakeTable(), lambda_client=FakeLambda())
    assert code == 0
    assert "closed 0 incidents, deleted 0 injections" in capsys.readouterr().out


@pytest.mark.parametrize("fail", [{"function_error": "Unhandled"}, {"raise_code": "ResourceNotFoundException"}])
def test_invoke_failure_deletes_nothing(fail, capsys):
    table = FakeTable(INJ)
    code = reset.main(["--yes"], env=ENV, table=table, lambda_client=FakeLambda(**fail))
    assert code == 1 and set(table.items) == {"01AAA", "01BBB"} and table.writes == 0
    assert "nothing deleted" in capsys.readouterr().err


def test_confirmation_is_required_without_yes():
    table, lam = FakeTable(INJ), FakeLambda()
    assert reset.main([], env=ENV, table=table, lambda_client=lam, ask=lambda _: "n") == 1
    assert lam.calls == [] and len(table.items) == 2

    def no_tty(_):
        raise EOFError

    assert reset.main([], env=ENV, table=table, lambda_client=lam, ask=no_tty) == 1
    assert lam.calls == [] and len(table.items) == 2
    assert reset.main([], env=ENV, table=table, lambda_client=lam, ask=lambda _: "y") == 0
    assert len(lam.calls) == 1 and table.items == {}
