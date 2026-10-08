"""Arrow written for kernels, the statement classifier and policy, and the
standalone engine's local connection file."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from alkera_notebook.sql.broker import SqlBroker
from alkera_notebook.sql.encode import ArrowSink, classic_schema, classic_type
from alkera_notebook.sql.errors import UnknownConnectionError
from alkera_notebook.sql.limits import has_own_limit
from alkera_notebook.sql.policy import DefaultStatementPolicy, NoConfirmer, keyword_classifier
from alkera_notebook.sql.provider import ArrowResult, SqlProviderRegistry, SqlRequest, SqlWorkspace
from alkera_notebook.sql.providers.local_config import LocalConfigProvider, load_connections
from nbsqw_support import AGENT, ALICE, SYSTEM, Kernel, make_db


class UuidType(pa.ExtensionType):
    def __init__(self) -> None:
        super().__init__(pa.binary(16), "example.uuid")

    def __arrow_ext_serialize__(self) -> bytes:
        return b""

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type: pa.DataType, serialized: bytes) -> UuidType:
        return cls()


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param(pa.string_view(), pa.string(), id="string-view"),
        pytest.param(pa.binary_view(), pa.binary(), id="binary-view"),
        pytest.param(pa.run_end_encoded(pa.int32(), pa.string()), pa.string(), id="run-end"),
        pytest.param(pa.list_view(pa.string_view()), pa.list_(pa.string()), id="list-view"),
        pytest.param(
            pa.struct([("a", pa.string_view())]), pa.struct([("a", pa.string())]), id="struct"
        ),
        pytest.param(
            pa.map_(pa.string_view(), pa.int8()), pa.map_(pa.string(), pa.int8()), id="map"
        ),
        pytest.param(UuidType(), pa.binary(16), id="extension"),
        pytest.param(pa.int64(), pa.int64(), id="plain"),
    ],
)
def test_classic_types(given: pa.DataType, expected: pa.DataType) -> None:
    assert classic_type(given) == expected


def test_a_batch_with_new_types_reaches_the_kernel_classic() -> None:
    ree = pa.RunEndEncodedArray.from_arrays(pa.array([2, 3], pa.int32()), pa.array(["a", "b"]))
    views = pa.array(["x", "y", "z"], pa.string_view())
    ext = pa.ExtensionArray.from_storage(UuidType(), pa.array([b"0" * 16] * 3, pa.binary(16)))
    batch = pa.record_batch([ree, views, ext], names=["r", "v", "u"])
    sink = ArrowSink(batch.schema, data_dir=None)
    sink.write(batch)
    encoded = sink.finish()
    assert encoded.inline is not None
    table = pa.ipc.open_stream(encoded.inline).read_all()
    assert table.schema == pa.schema([("r", pa.string()), ("v", pa.string()), ("u", pa.binary(16))])
    assert table.to_pydict() == {"r": ["a", "a", "b"], "v": ["x", "y", "z"], "u": [b"0" * 16] * 3}
    assert b"ARROW:extension" not in encoded.inline
    assert b"example.uuid" not in encoded.inline


def test_classic_schema_drops_extension_metadata() -> None:
    schema = pa.schema([pa.field("u", UuidType())])
    assert classic_schema(schema).field("u").metadata is None


@pytest.mark.parametrize(
    ("sql", "own"),
    [
        pytest.param("select * from t limit 10", True, id="limit"),
        pytest.param("SELECT * FROM t LIMIT 10 OFFSET 5", True, id="upper-offset"),
        pytest.param("select * from t fetch first 5 rows only", True, id="fetch-first"),
        pytest.param("select top 5 * from t", True, id="top"),
        pytest.param("select * from (select * from t limit 5) s", False, id="subquery"),
        pytest.param("select 'limit 5' from t", False, id="in-string"),
        pytest.param("select * from t -- limit 5", False, id="in-comment"),
        pytest.param("select 1 limit 1; select * from t", False, id="earlier-statement"),
        pytest.param("select limit_col from t", False, id="identifier"),
        pytest.param("", False, id="empty"),
    ],
)
def test_has_own_limit(sql: str, own: bool) -> None:
    assert has_own_limit(sql) is own


# ------------------------------------------------------------------ classifier and policy


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        pytest.param("select 1", "read", id="select"),
        pytest.param("  -- note\nWITH t AS (select 1) select * from t", "read", id="cte"),
        pytest.param("(select 1) union (select 2)", "read", id="parenthesised"),
        pytest.param("show tables", "read", id="show"),
        pytest.param("select 'delete from x' as s", "read", id="keyword-in-string"),
        pytest.param("select 1 /* drop table */", "read", id="keyword-in-comment"),
        pytest.param("insert into t values (1)", "write", id="insert"),
        pytest.param(
            "with x as (delete from t returning *) select * from x", "write", id="modifying-cte"
        ),
        pytest.param("select * into outfile '/tmp/x' from t", "write", id="into-outfile"),
        pytest.param("select 1; delete from t", "write", id="second-statement"),
        pytest.param("copy t to 'x.csv'", "write", id="copy"),
        pytest.param("create table t (a int)", "ddl", id="create"),
        pytest.param("select 1; drop table t", "ddl", id="drop-after-read"),
        pytest.param("grant select on t to bob", "ddl", id="grant"),
        pytest.param("vacuum", "write", id="vacuum"),
        pytest.param("frobnicate the table", "unknown", id="unknown"),
        pytest.param("  ;  ", "unknown", id="empty"),
    ],
)
def test_keyword_classifier(sql: str, kind: str) -> None:
    assert keyword_classifier(sql) == kind


@pytest.mark.parametrize(
    ("actor", "sql", "kind"),
    [
        pytest.param(ALICE, "select 1", "read", id="person-read"),
        pytest.param(ALICE, "insert into t values (1)", "write", id="person-insert"),
        pytest.param(ALICE, "frobnicate", "unknown", id="person-unknown"),
        pytest.param(AGENT, "drop table t", "ddl", id="agent-ddl"),
        pytest.param(AGENT, "delete from t", "write", id="agent-delete"),
        pytest.param(SYSTEM, "delete from t", "write", id="system-write"),
    ],
)
def test_the_default_policy_runs_every_statement_and_names_its_kind(
    actor: Any, sql: str, kind: str
) -> None:
    """The run is the gate (a person ran it, approved it, or the agent's mode
    runs without asking), so no statement is held back for what it does."""
    decision = DefaultStatementPolicy().check(sql, actor)
    assert (decision.decision, decision.kind) == ("allow", kind)


async def test_no_confirmer_confirms_nothing() -> None:
    assert await NoConfirmer().confirm("r", ALICE, "delete from t", "write") is False


# ------------------------------------------------------------------ local connection file


def write_config(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_load_connections_skips_entries_without_a_target(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path / "c.toml",
        '[connections.a]\nurl = "duckdb:///x.db"\n[connections.b]\nnote = "nothing"\n'
        '[connections.c]\nadbc_driver = "adbc_driver_sqlite"\nuri = "file.db"\nread_only = false\n',
    )
    found = load_connections(cfg)
    assert sorted(found) == ["a", "c"]
    assert found["c"].read_only is False
    assert load_connections(tmp_path / "missing.toml") == {}


def test_default_path_honours_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert LocalConfigProvider().path == tmp_path / "alkera" / "connections.toml"


async def test_local_duckdb_connection(tmp_path: Path) -> None:
    db = make_db(tmp_path / "shop.duckdb")
    cfg = write_config(
        tmp_path / "cfg" / "connections.toml", f'[connections.shop]\nurl = "duckdb:///{db}"\n'
    )
    provider = LocalConfigProvider(path=cfg)
    ws = SqlWorkspace(id="w", root=str(tmp_path))
    assert provider.can_resolve("shop", ws)
    assert not provider.can_resolve("other", ws)
    kernel = Kernel(data_dir=None)
    kernel.start("r1")
    response = await SqlBroker(SqlProviderRegistry([provider])).execute(
        kernel, {"sql": "select count(*) n from orders", "connection": "shop"}, {"run_id": "r1"}
    )
    assert pa.ipc.open_stream(response["table"].data).read_all().to_pydict() == {"n": [100]}


async def test_local_duckdb_connection_is_hardened(tmp_path: Path) -> None:
    db = make_db(tmp_path / "shop.duckdb")
    cfg = write_config(
        tmp_path / "connections.toml", f'[connections.shop]\nurl = "duckdb:///{db}"\n'
    )
    provider = LocalConfigProvider(path=cfg)
    with pytest.raises(Exception, match=r"(?i)external|disabled|permission"):
        result = await provider.execute(
            SqlRequest("select * from read_text('/etc/hosts')", "shop"),
            ALICE,
            SqlWorkspace("w", "/"),
        )
        result.reader.read_all()


async def test_local_sqlalchemy_connection(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("create table t (a integer, b text)")
    conn.executemany("insert into t values (?, ?)", [(1, "x"), (2, "y")])
    conn.commit()
    conn.close()
    cfg = write_config(
        tmp_path / "connections.toml", f'[connections.lite]\nurl = "sqlite:///{path}"\n'
    )
    provider = LocalConfigProvider(path=cfg)
    result = await provider.execute(
        SqlRequest("select a, b from t where a >= :lo order by a", "lite", params={"lo": 1}),
        ALICE,
        SqlWorkspace("w", "/"),
    )
    assert result.reader.read_all().to_pydict() == {"a": [1, 2], "b": ["x", "y"]}
    await provider.cancel(result.query_id)  # nothing in flight: a no-op


async def test_local_unknown_connection(tmp_path: Path) -> None:
    provider = LocalConfigProvider(path=tmp_path / "none.toml")
    with pytest.raises(UnknownConnectionError):
        await provider.execute(SqlRequest("select 1", "x"), ALICE, SqlWorkspace("w", "/"))


async def test_arrow_result_release_runs_once() -> None:
    calls: list[int] = []
    table = pa.table({"a": [1]})
    result = ArrowResult(
        reader=pa.RecordBatchReader.from_batches(table.schema, table.to_batches()),
        query_id="q",
        release=lambda: calls.append(1),
    )
    result.close()
    result.close()
    await asyncio.sleep(0)
    assert calls == [1]
