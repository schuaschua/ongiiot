"""Detection adapters with fakes: reader completeness and strategy parity (AD-18); derived writer guard and batching (AD-13)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from detect.adapters import derived_writer, reader
from detect.adapters.derived_writer import AliasError, DerivedWriter, WriteError
from detect.adapters.reader import BatchGetReader, ExecuteQueryReader, ReaderError, make_reader
from detect.core.models import PipeMeta, Sample
from tests.detect_fakes import FakeSiteWiseRead, FakeSiteWiseWrite
from tests.sim_fakes import FakeClientError

START = datetime(2026, 9, 28, 12, 18, tzinfo=UTC)
END = datetime(2026, 9, 28, 12, 33, tzinfo=UTC)
S0, S1 = START.timestamp(), END.timestamp()
ROOT = "/poc-dj/lng1"


def pipes(n: int = 3, measurements=("flow_in", "flow_out", "methane_ppm")) -> dict[str, PipeMeta]:
    out = {}
    for i in range(1, n + 1):
        pid = f"P-{i:03d}"
        area = "train1" if i % 2 else "train2"
        out[pid] = PipeMeta(pid, area, "fuel_gas", "cs", 5, "g", {m: f"{ROOT}/{area}/{pid}/{m}" for m in measurements}, {})
    return out


def full_store(ps, step=1.0, lo=S0 - 30, hi=S1 + 30):
    store = {}
    for p in ps.values():
        for m, a in p.aliases.items():
            n = int((hi - lo) / step)
            store[a] = [(lo + k * step, float(k % 17) + 0.5, "GOOD") for k in range(n)]
    return store


READERS = [ExecuteQueryReader, BatchGetReader]


@pytest.mark.parametrize("cls", READERS)
def test_every_alias_present_even_without_data(cls):
    ps = pipes()
    store = full_store(ps)
    missing = ps["P-002"].aliases["flow_out"]
    del store[missing]
    fw = cls(FakeSiteWiseRead(store)).read(ps, START, END)
    assert set(fw.window.series) == {a for p in ps.values() for a in p.aliases.values()}
    assert fw.window.series[missing] == []
    assert fw.pipes is ps and (fw.window.start, fw.window.end) == (START, END)


@pytest.mark.parametrize("cls", READERS)
def test_window_is_half_open_ascending_and_utc(cls):
    ps = pipes(1)
    fw = cls(FakeSiteWiseRead(full_store(ps))).read(ps, START, END)
    for samples in fw.window.series.values():
        assert len(samples) == 900
        assert samples[0].ts == START and samples[-1].ts == datetime(2026, 9, 28, 12, 32, 59, tzinfo=UTC)
        assert all(a.ts < b.ts for a, b in zip(samples, samples[1:]))
        assert all(s.ts.tzinfo is UTC for s in samples)


def test_strategies_return_the_same_window():
    ps = pipes(5)
    store = full_store(ps, step=0.5)
    # Irregular data: a gap, a BAD sample with no value, sub-second stamps, one silent alias.
    a = ps["P-003"].aliases["flow_in"]
    store[a] = [v for v in store[a] if not S0 + 100 <= v[0] < S0 + 200] + [(S0 + 150.25, None, "BAD")]
    store[ps["P-004"].aliases["methane_ppm"]] = []
    # Values that must be ignored: another stage under a similar root, and a derived alias.
    store["/poc-dj/lng1/train1/P-001/derived/flow_in_dq"] = [(S0 + 1, 1.0, "GOOD")]
    store["/poc-dx/lng1/train1/P-001/flow_in"] = [(S0 + 1, 9.0, "GOOD")]
    eq = ExecuteQueryReader(FakeSiteWiseRead(store, page=997)).read(ps, START, END)
    bg = BatchGetReader(FakeSiteWiseRead(store, page=1234)).read(ps, START, END)
    assert eq == bg
    assert Sample(datetime.fromtimestamp(S0 + 150.25, tz=UTC), None, "BAD") in eq.window.series[a]


def test_batch_get_uses_16_entries_per_request_and_pages():
    ps = pipes(6)  # 18 aliases -> 2 requests
    fake = FakeSiteWiseRead(full_store(ps), page=5000)
    BatchGetReader(fake).read(ps, START, END)
    first_calls = [c for c in fake.batch_calls if c["nextToken"] is None]
    assert sorted(len(c["entries"]) for c in first_calls) == [2, 16]
    assert any(c["nextToken"] for c in fake.batch_calls)
    for c in fake.batch_calls:
        for e in c["entries"]:
            assert e["timeOrdering"] == "ASCENDING"
            assert e["startDate"] <= START and e["endDate"] >= END


def test_execute_query_statement_and_prefix_cached():
    ps = pipes(2)
    fake = FakeSiteWiseRead(full_store(ps), page=400)
    r = ExecuteQueryReader(fake)
    r.read(ps, START, END)
    r.read(ps, START, END)
    q = fake.queries[0]["queryStatement"]
    assert "FROM raw_time_series" in q and f"LIKE '{ROOT}/train%'" in q
    assert "event_timestamp >= TIMESTAMP '2026-09-28 12:18:00'" in q and "event_timestamp < TIMESTAMP '2026-09-28 12:33:00'" in q
    assert len(r._prefixes) == 1
    assert any(c["nextToken"] for c in fake.queries)


@pytest.mark.parametrize("cls", READERS)
def test_adapter_failure_raises(cls):
    with pytest.raises(FakeClientError):
        cls(FakeSiteWiseRead({}, fail="AccessDeniedException")).read(pipes(1), START, END)


def test_batch_get_error_entries_raise():
    class Broken(FakeSiteWiseRead):
        def batch_get_asset_property_value_history(self, entries, maxResults=None, nextToken=None):
            return {"errorEntries": [{"entryId": "e0", "errorCode": "ResourceNotFoundException"}], "successEntries": []}

    with pytest.raises(ReaderError, match="errors"):
        BatchGetReader(Broken({})).read(pipes(1), START, END)


@pytest.mark.parametrize("cls", READERS)
def test_naive_or_empty_window_refused(cls):
    with pytest.raises(ValueError):
        cls(FakeSiteWiseRead({})).read(pipes(1), START.replace(tzinfo=None), END)
    with pytest.raises(ValueError):
        cls(FakeSiteWiseRead({})).read(pipes(1), END, START)


def test_make_reader():
    assert make_reader("execute_query", None).strategy == "execute_query"
    assert make_reader("batch_get", None).strategy == "batch_get"
    assert make_reader(None, None).strategy == reader.DEFAULT_STRATEGY
    with pytest.raises(ValueError, match="READER_STRATEGY"):
        make_reader("athena", None)


# -- derived writer ------------------------------------------------------------------------------

DQ = f"{ROOT}/train1/P-001/derived/flow_in_dq"
FAIL = f"{ROOT}/train1/P-001/derived/flow_in_dq_fail"


@pytest.mark.parametrize(
    "alias",
    [
        f"{ROOT}/train1/P-001/flow_in",  # raw
        f"{ROOT}/train1/P-001/attr/flow_in_max",
        f"{ROOT}/train1/P-001/derived/",
        f"{ROOT}/train1/P-001/derived/x/y",
        "derived/flow_in_dq",
        "",
    ],
)
def test_writer_refuses_non_derived_before_any_call(alias):
    fake = FakeSiteWiseWrite()
    with pytest.raises(AliasError):
        DerivedWriter(fake).write({DQ: 1.0, alias: 1.0}, END)
    assert fake.calls == []


def test_writer_refuses_alias_outside_allowed_set():
    fake = FakeSiteWiseWrite()
    with pytest.raises(AliasError, match="not a derived alias"):
        DerivedWriter(fake, allowed=[DQ]).write({DQ: 1.0, FAIL: ""}, END)
    assert fake.calls == []
    with pytest.raises(AliasError):
        DerivedWriter(fake, allowed=[f"{ROOT}/train1/P-001/flow_in"])


def test_writer_batches_types_and_timestamp():
    values = {}
    for i in range(1, 13):
        values[f"{ROOT}/train1/P-{i:03d}/derived/flow_in_dq"] = 1.0
        values[f"{ROOT}/train1/P-{i:03d}/derived/flow_in_dq_fail"] = ""
    fake = FakeSiteWiseWrite()
    assert DerivedWriter(fake).write(values, END) == 24
    assert [len(c) for c in fake.calls] == [10, 10, 4]
    entries = [e for c in fake.calls for e in c]
    assert len({e["entryId"] for c in fake.calls for e in c}) == 24
    ts = {"timeInSeconds": int(S1), "offsetInNanos": 0}
    for e in entries:
        (v,) = e["propertyValues"]
        assert v["timestamp"] == ts and v["quality"] == "GOOD"
        assert v["value"] == ({"stringValue": ""} if e["propertyAlias"].endswith("_fail") else {"doubleValue": 1.0})


def test_writer_retries_throttling_then_raises_on_errors():
    sleeps = []
    fake = FakeSiteWiseWrite(throttle_first=2)
    DerivedWriter(fake, sleep=sleeps.append).write({DQ: 0.5}, END)
    assert len(fake.calls) == 3 and len(sleeps) == 2
    with pytest.raises(WriteError):
        DerivedWriter(FakeSiteWiseWrite(entry_error="InvalidRequestException"), sleep=sleeps.append).write({DQ: 0.5}, END)
    with pytest.raises(WriteError):
        DerivedWriter(FakeSiteWiseWrite(throttle_first=99), sleep=sleeps.append).write({DQ: 0.5}, END)


@pytest.mark.parametrize("bad", [float("nan"), True, None])
def test_writer_refuses_bad_values_before_any_call(bad):
    fake = FakeSiteWiseWrite()
    with pytest.raises(ValueError):
        DerivedWriter(fake).write({DQ: 1.0, FAIL: bad}, END)
    assert fake.calls == []


def test_writer_refuses_naive_timestamp():
    with pytest.raises(ValueError):
        DerivedWriter(FakeSiteWiseWrite()).write({DQ: 1.0}, END.replace(tzinfo=None))


def test_writer_max_entries_is_ten():
    assert derived_writer.MAX_ENTRIES == 10
    assert reader.BATCH_GET_ENTRIES == 16
