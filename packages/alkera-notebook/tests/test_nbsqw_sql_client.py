"""``alkera.sql`` in a kernel against the engine's real broker: the request
crosses the RPC's value encoding both ways, as it does on the socket."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from nbsqw_support import ALKERA_CODECS_ALL, Kernel, Who, make_db

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "alkera-py" / "tests"))
import alkera._sql as alkera_sql
from alkera_notebook.rpc import frames
from alkera_notebook.sql.broker import SqlBroker
from alkera_notebook.sql.provider import SqlProviderRegistry
from alkera_notebook.sql.providers.fake import FakeConnectionProvider
from nbsqw_host import FakeHost, installed


class EngineThread:
    """The engine's event loop on a thread; the kernel's calls block on it,
    as the kernel's threaded client does."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def engine() -> Iterator[EngineThread]:
    e = EngineThread()
    yield e
    e.stop()


def wire_host(
    engine: EngineThread, broker: SqlBroker, kernel: Kernel, host: FakeHost, run_id: str = "r1"
) -> None:
    def respond(method: str, params: dict[str, Any]) -> Any:
        assert method == "sql.execute"
        header, segments = frames.encode_params(params)
        decoded = frames.decode_params(header, segments)
        result = engine.run(broker.execute(kernel, decoded, {"run_id": run_id, "cell_id": "c1"}))
        out_segments: list[bytes] = []
        encoded = frames.encode_value(result, out_segments)
        return frames.decode_value(encoded, out_segments, allow_files=True)

    host.responder = respond


@pytest.fixture
def setup(tmp_path: Path, engine: EngineThread) -> tuple[FakeHost, Kernel, FakeConnectionProvider]:
    db = make_db(tmp_path / "shop.duckdb", rows=50_000)
    data = tmp_path / "data"
    data.mkdir()
    kernel = Kernel(data_dir=data, codecs=ALKERA_CODECS_ALL)
    kernel.start("r1", Who("person", "alice"))
    provider = FakeConnectionProvider({"shop": db})
    broker = SqlBroker(SqlProviderRegistry([provider]), inline_limit=64 * 1024)
    host = FakeHost(data_dir=str(data))
    wire_host(engine, broker, kernel, host)
    return host, kernel, provider


def test_a_small_result_arrives_inline_as_pandas(setup: Any) -> None:
    host, _kernel, _provider = setup
    with installed(host):
        df = alkera_sql.sql("select id, amount from orders order by id limit 3", connection="shop")
    assert isinstance(df, pd.DataFrame)
    assert df.to_dict(orient="list") == {"id": [0, 1, 2], "amount": [0.0, 1.5, 3.0]}
    assert host.displayed == [df]


def test_a_large_result_arrives_as_a_file_that_is_deleted_once_read(setup: Any) -> None:
    host, kernel, _provider = setup
    with installed(host):
        df = alkera_sql.sql("select * from orders", connection="shop", output=False)
    assert len(df) == 50_000
    assert host.displayed == []
    assert os.listdir(kernel.data_dir) == []


def test_a_cut_result_shows_the_notice(setup: Any) -> None:
    host, kernel, _provider = setup
    kernel.sql_row_limit = 10
    with installed(host):
        df = alkera_sql.sql("select * from orders", connection="shop")
    assert len(df) == 10
    notice, frame = host.displayed
    assert isinstance(notice, alkera_sql.Notice)
    assert notice._repr_mimebundle_()["text/markdown"].startswith("Showing the first 10 rows")
    assert frame is df


def test_rows_json_when_the_kernel_has_no_arrow(setup: Any, engine: EngineThread) -> None:
    host, kernel, _provider = setup
    kernel.codecs = frozenset({"json", "rows.json"})
    with installed(host):
        df = alkera_sql.sql(
            "select 1 as i, 2.5::decimal(5,2) as d, date '2026-10-05' as day, "
            "time '12:30:00' as at, 'x'::blob as raw",
            connection="shop",
        )
    row = df.iloc[0].to_dict()
    assert row["i"] == 1
    assert str(row["d"]) == "2.50"
    assert str(row["day"]) == "2026-10-05"
    assert str(row["at"]) == "12:30:00"
    assert row["raw"] == b"x"


def test_parameters_cross_with_their_types(setup: Any) -> None:
    host, _kernel, _provider = setup
    import decimal

    with installed(host):
        df = alkera_sql.sql(
            "select count(*) as n from orders where amount > $lo",
            connection="shop",
            params={"lo": decimal.Decimal("74000.5")},
        )
    assert int(df["n"][0]) == sum(1 for i in range(50_000) if i * 1.5 > 74000.5)


def test_errors_from_the_engine_reach_the_cell(setup: Any) -> None:
    host, _kernel, _provider = setup
    with installed(host), pytest.raises(frames.RpcError) as err:
        alkera_sql.sql("select 1", connection="nope")
    assert err.value.name == "sql.unknown_connection"


def test_a_missing_preferred_library_raises_before_any_query(setup: Any) -> None:
    host, _kernel, provider = setup
    host.dataframe = "polars"
    if alkera_sql.available("polars"):
        pytest.skip("polars is installed here; the conformance runs cover it")
    with installed(host), pytest.raises(ModuleNotFoundError) as err:
        alkera_sql.sql("select 1", connection="shop")
    assert err.value.name == "polars"
    assert provider.executed == []


def test_connection_outside_a_notebook_says_so() -> None:
    with pytest.raises(RuntimeError, match="inside an Alkera notebook"):
        alkera_sql.sql("select 1", connection="shop")


def test_connection_and_engine_together_are_refused() -> None:
    with pytest.raises(ValueError, match="not both"):
        alkera_sql.sql("select 1", connection="a", engine=object())


def test_local_duckdb_over_frames_in_scope() -> None:
    host = FakeHost()
    orders = pd.DataFrame({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]})
    unrelated = pd.DataFrame({"x": [1]})
    with installed(host):
        df = alkera_sql.sql("select sum(amount) as total from orders where id > 1")
    assert df.to_dict(orient="list") == {"total": [50.0]}
    # The connection was interruptible while it ran, and let go after.
    assert len(host.interruptible) == 1
    assert host.unregistered == host.interruptible
    # Only the frame the query names is handed to DuckDB.
    found = alkera_sql._frames_in_scope("select * from orders", sys._getframe())
    assert found == {"orders": orders}
    assert len(unrelated) == 1


def test_local_duckdb_in_a_script_with_parameters() -> None:
    df = alkera_sql.sql("select $x + 1 as y", params={"x": 41})
    assert df.to_dict(orient="list") == {"y": [42]}


def test_engine_through_a_dbapi_connection(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "x.sqlite")
    conn.execute("create table t (a int, b text)")
    conn.executemany("insert into t values (?, ?)", [(1, "x"), (2, "y")])
    df = alkera_sql.sql("select a, b from t where a > ?", engine=conn, params=(0,))
    assert df.to_dict(orient="list") == {"a": [1, 2], "b": ["x", "y"]}


def test_engine_with_an_arrow_cursor() -> None:
    import duckdb

    conn = duckdb.connect()
    df = alkera_sql.sql("select 7 as seven", engine=conn)
    assert df.to_dict(orient="list") == {"seven": [7]}


@pytest.mark.parametrize("query", [pytest.param("", id="empty"), pytest.param("   ", id="blank")])
def test_an_empty_query_is_refused(query: str) -> None:
    with pytest.raises(ValueError, match="needs a query"):
        alkera_sql.sql(query)
