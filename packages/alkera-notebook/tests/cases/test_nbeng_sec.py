"""Isolation: engines share nothing, kernels see only allowlisted variables,
and the engine never decodes Arrow produced by a kernel."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_notebook.events.models import AnyEvent
from alkera_notebook.kernels.launch_local import ENV_ALLOWLIST
from nbeng_harness import engine_for, notebook, run_cells, text_of

PACKAGE = Path(__file__).resolve().parents[2] / "alkera_notebook"


async def test_sec_two_engines_share_nothing(tmp_path: Path) -> None:
    async with engine_for(tmp_path, name="a") as one, engine_for(tmp_path, name="b") as two:
        s1, c1, (a1,) = await notebook(one, ["x = 'one'\nx"])
        s2, c2, (a2,) = await notebook(two, ["x = 'two'\nx"])
        seen_two: list[AnyEvent] = []
        s2.runtime.hub.listen(seen_two.append)
        assert (await run_cells(c1, a1)).status == "ok"
        # Same path, same sequential ids, different everything else.
        assert s1.path == s2.path
        assert s2.runtime.kernel is None
        assert seen_two == []
        assert one.sessions != two.sessions and one.guard is not two.guard
        assert (await c2.read()).cells[0].status == "not_run"
        assert (await run_cells(c2, a2)).status == "ok"
        k1, k2 = s1.runtime.kernel, s2.runtime.kernel
        assert k1 is not None and k2 is not None and k1.pid != k2.pid
        assert await text_of(c1, a1) == "'one'"
        assert await text_of(c2, a2) == "'two'"
        assert one.live_kernel_count() == 1 and two.live_kernel_count() == 1
        await c1.kernel("shutdown")
        assert k2.alive and two.live_kernel_count() == 1
        files = {p.name for p in (tmp_path / "a").iterdir()}
        assert "nb.alknb.py" in files
        assert "'one'" in (tmp_path / "a" / "nb.alknb.py").read_text()
        assert "'one'" not in (tmp_path / "b" / "nb.alknb.py").read_text()


async def test_sec_kernel_environment_holds_only_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    async with engine_for(tmp_path, kernel="fake") as engine:
        engine.config.base_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "LANG": "C.UTF-8",
            "AWS_SECRET_ACCESS_KEY": "leak-me",
            "PYTHONPATH": "/evil",
        }
        session, client, (a,) = await notebook(engine, ["import os\nsorted(os.environ)"])
        assert (await run_cells(client, a)).status == "ok"
        kernel = session.runtime.kernel
        assert kernel is not None
        names = set(kernel.hello["env"])
        # macOS's CoreFoundation sets __CF_USER_TEXT_ENCODING inside every
        # process at startup; it is not inherited from the engine.
        names = {n for n in names if not n.startswith("__CF")}
        assert names <= ENV_ALLOWLIST, names - ENV_ALLOWLIST
        assert "AWS_SECRET_ACCESS_KEY" not in await text_of(client, a)
        assert {"ALKERA_RPC_ENDPOINT", "ALKERA_KERNEL_ID", "PYTHONHASHSEED"} <= names


# The Arrow ratchet ----------------------------------------------------------

# Arrow (and Arrow-backed frame) readers: decoding kernel-produced bytes with
# any of these on the engine side is forbidden.
FORBIDDEN_CALLS = frozenset(
    {
        "open_stream",
        "open_file",
        "RecordBatchStreamReader",
        "RecordBatchFileReader",
        "read_ipc",
        "read_ipc_stream",
        "scan_ipc",
        "read_table",
        "read_message",
        "read_record_batch",
        "read_schema",
        "deserialize_pandas",
        "read_feather",
    }
)
ARROW_MODULES = ("pyarrow", "polars", "pa", "pl", "ipc", "feather")

# Engine-side paths the ratchet does not cover: the SQL providers run
# engine-side against warehouses (their Arrow is the provider's own, never the
# kernel's); the shared frames module only encodes.
EXEMPT = ("sql/", "rpc/frames.py")


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def arrow_readers(source: str) -> Iterator[tuple[int, str]]:
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0]
            in (
                "pyarrow",
                "polars",
            )
        ):
            for alias in node.names:
                if alias.name in FORBIDDEN_CALLS:
                    yield node.lineno, f"{node.module}.{alias.name}"
                imported.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        parts = name.split(".")
        if parts[-1] in FORBIDDEN_CALLS and (len(parts) > 1 and parts[0] in ARROW_MODULES):
            yield node.lineno, name
        elif len(parts) == 1 and parts[0] in FORBIDDEN_CALLS and parts[0] in imported:
            yield node.lineno, name


def scan_package() -> list[str]:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith(EXEMPT[0]) or rel == EXEMPT[1] or rel.startswith("_marimo/"):
            continue
        for line, name in arrow_readers(path.read_text(encoding="utf-8")):
            found.append(f"{rel}:{line} {name}")
    return found


def test_sec_engine_never_decodes_kernel_arrow() -> None:
    assert PACKAGE.is_dir()
    assert scan_package() == []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import pyarrow.ipc\npyarrow.ipc.open_stream(b)\n", id="pyarrow-ipc-open-stream"
        ),
        pytest.param("import pyarrow as pa\npa.ipc.open_file(f)\n", id="pa-ipc-open-file"),
        pytest.param("import pyarrow as pa\npa.RecordBatchStreamReader(b)\n", id="stream-reader"),
        pytest.param("import pyarrow as pa\npa.ipc.RecordBatchFileReader(f)\n", id="file-reader"),
        pytest.param("import pyarrow.ipc as ipc\nipc.read_message(b)\n", id="ipc-read-message"),
        pytest.param("import polars as pl\npl.read_ipc(b)\n", id="polars-read-ipc"),
        pytest.param("import polars\npolars.read_ipc_stream(b)\n", id="polars-read-ipc-stream"),
        pytest.param("from pyarrow.ipc import open_stream\nopen_stream(b)\n", id="from-import"),
        pytest.param(
            "def f(b):\n    import pyarrow.feather as feather\n    return feather.read_table(b)\n",
            id="nested-feather",
        ),
    ],
)
def test_sec_arrow_ratchet_catches_a_planted_reader(source: str) -> None:
    assert list(arrow_readers(source)), source


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import pyarrow as pa\npa.ipc.new_stream(sink, schema)\n", id="writer"),
        pytest.param("f.open_file(path)\n", id="unrelated-open-file"),
        pytest.param("import json\njson.loads(s)\n", id="json"),
    ],
)
def test_sec_arrow_ratchet_admits_writers_and_unrelated_calls(source: str) -> None:
    assert list(arrow_readers(source)) == []
