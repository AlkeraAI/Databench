"""A sandbox worker pays its cold costs before it serves, never inside a request.

The first SQL cell a fresh worker judged used to cost seconds: rendering it
compiles the cell, and marimo's visitor then imports DuckDB and sqlglot (plus
the format package itself). On a loaded host that outlived the 2 s validate
budget, the worker was killed, and the next try met another cold worker. Here
the real worker's own start-up runs (``worker.main``), and then a tab's
requests (load, an update inserting a SQL cell, a render) are answered through
``worker.handle``: nothing may be imported while they run, because every
import there is time a request's budget pays for.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from alkera_notebook import format as fmt
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox import notebook as nb
from backend.services.crdt.sandbox.pool import PoolConfig, SandboxPool
from loro import ExportMode

pytestmark = [pytest.mark.spread]

KEY = "org:notebook:warm"
SEED, TAB = 4000, 5000
CELL = "c0d1e2f3g4"

#: Runs the real worker's start-up, then (in place of the frame loop) a tab's
#: requests through the worker's own handler, and writes down every module
#: they imported.
_PROBE = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from backend.services.crdt.sandbox import worker
    from backend.services.crdt.sandbox.protocol import Frame

    here = Path(sys.argv[1])
    blob = lambda name: (here / name).read_bytes()

    def probe(stdin, stdout, cache, *rest):
        before = set(sys.modules)
        at = {"key": %(key)r, "epoch": 1, "log_seq": 0, "rules": "notebook"}
        loaded = worker.handle(
            Frame({"op": "load", **at}, (blob("snapshot"), blob("vv"))), cache
        )
        judged = worker.handle(
            Frame({"op": "validate", **at, "peers": [%(tab)d]}, (blob("update"),)), cache
        )
        worker.handle(
            Frame({"op": "advance", **at, "log_seq": 1}, (judged.blobs[0],)), cache
        )
        rendered = worker.handle(Frame({"op": "content", **at, "log_seq": 1}), cache)
        (here / "report.json").write_text(json.dumps({
            "imported": sorted(set(sys.modules) - before),
            "loaded": loaded.header.get("ok"),
            "outcome": judged.header.get("outcome"),
            "rendered": rendered.blobs[0].decode() if rendered.blobs else "",
        }))

    worker.serve = probe
    sys.exit(worker.main(["--memory-mb", "0"]))
    """
) % {"key": KEY, "tab": TAB}


def _python_only_notebook() -> str:
    return str(
        fmt.write(
            fmt.NotebookIR(
                cells=(
                    fmt.CellIR(
                        id="a1b2c3d4e5",
                        kind="python",
                        name="_",
                        source="x = 1",
                        code="x = 1",
                    ),
                )
            )
        )
    )


def _insert_sql_cell(snapshot: bytes) -> bytes:
    """What "Insert SQL below" and typing a query send: one new cell."""
    tab = core.new_doc()
    tab.peer_id = TAB
    tab.import_(snapshot)
    before = tab.oplog_vv
    created = nb.insert_map(tab.get_map("cells"), CELL)
    created.insert("kind", "sql")
    created.insert("name", "_")
    created.insert("deleted", False)
    nb.insert_text(created, "source").insert(
        0, "SELECT category, sum(amount) AS total FROM df GROUP BY 1 ORDER BY 1"
    )
    nb.insert_map(created, "config")
    meta = nb.insert_map(created, "meta")
    meta.insert("output_var", "_df")
    meta.insert("show_output", True)
    nb.insert_map(created, "extra")
    tab.get_movable_list("order").push(CELL)
    tab.commit()
    return bytes(tab.export(ExportMode.Updates(before)))


def test_a_fresh_workers_first_sql_cell_imports_nothing_inside_the_request(
    tmp_path: Path,
) -> None:
    cache = core.DocCache()
    seeded = core.seed(
        cache, key=KEY, epoch=1, rules=nb.NOTEBOOK, text=_python_only_notebook(), peer=SEED
    )
    (tmp_path / "snapshot").write_bytes(seeded.snapshot)
    (tmp_path / "vv").write_bytes(seeded.vv)
    (tmp_path / "update").write_bytes(_insert_sql_cell(seeded.snapshot))
    script = tmp_path / "probe.py"
    script.write_text(_PROBE)

    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True, timeout=300)

    report = json.loads((tmp_path / "report.json").read_text())
    assert (report["loaded"], report["outcome"]) == (True, "ok")
    assert "_df = alkera.sql(" in report["rendered"]
    assert report["imported"] == [], "a request paid for an import the warm-up should have"


async def test_a_started_pool_has_a_warm_worker_on_every_slot_before_any_request() -> None:
    pool = SandboxPool(PoolConfig(workers=2))
    try:
        await pool.start()
        pids = pool.pids()
        assert all(pid is not None for pid in pids) and len(set(pids)) == 2
    finally:
        await pool.close()


def test_every_registered_type_warms() -> None:
    """The warm-up runs every registered type through the lane's own path;
    one that fails says so instead of keeping the worker from starting."""
    report = core.warm_all()
    assert set(report) == set(core.RULES)
    assert not [name for name, outcome in report.items() if outcome.startswith("failed")]
