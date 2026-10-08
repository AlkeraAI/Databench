"""Output snapshot cases: written and read through real files beside a notebook."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_notebook.engine.config import OutputLimits
from alkera_notebook.engine.models import ActingFor, ErrorInfo, RunActor
from alkera_notebook.outputs import (
    CellOutputs,
    KernelMeta,
    RunMeta,
    SnapshotCell,
    SnapshotContent,
    SnapshotScheduler,
    SnapshotWriter,
    blob_dir,
    code_hash,
    env_fingerprint,
    lineage_hashes,
    outdated,
    read_snapshot,
    reattach,
    snapshot_path,
)

LIMITS = OutputLimits()
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
#: An agent acting for a person: the richest requester a snapshot keeps.
BY = RunActor(
    kind="agent",
    id="agent:chat-1",
    display_name="Agent",
    acting_for=ActingFor(id="user:ana", display_name="Ana"),
)


def _ran(
    code: str,
    *,
    bundles: list[dict[str, object]] | None = None,
    stdout: str = "",
    error: ErrorInfo | None = None,
    lineage: str | None = "L",
    env: str | None = "E",
) -> CellOutputs:
    out = CellOutputs()
    out.begin_run(
        RunMeta(run_id="run_1", trigger="run", by=BY, started_at=T0),
        code_hash=code_hash(code),
        lineage_hash=lineage,
        env_fingerprint=env,
    )
    for b in bundles or []:
        out.apply_output(dict(b), "append", LIMITS)
    if stdout:
        out.apply_stream("stdout", stdout, LIMITS)
    out.apply_finished(error, "ok" if error is None else "error")
    assert out.run is not None
    out.run.finished_at = T0
    return out


def _nb(tmp_path: Path) -> Path:
    nb = tmp_path / "weekly.alknb.py"
    nb.write_text("# notebook\n", encoding="utf-8")
    return nb


def _write(nb: Path, cells: list[SnapshotCell], *, outputs_in_git: bool = False) -> Path:
    content = SnapshotContent(
        cells=cells,
        kernel=KernelMeta("krn_1", "default", "E"),
        outputs_in_git=outputs_in_git,
    )
    return SnapshotWriter(nb).write(content)


def test_out_snapshot_write_and_reload(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    a = _ran("x = 1\nx", bundles=[{"text/plain": "1", "text/html": "<b>1</b>"}], stdout="hi\n")
    b = _ran("y = x / 0", error=ErrorInfo(ename="ZeroDivisionError", evalue="division by zero"))
    path = _write(
        nb,
        [
            SnapshotCell("aaaaaaaaaa", "x = 1\nx", a),
            SnapshotCell("bbbbbbbbbb", "y = x / 0", b),
            SnapshotCell("cccccccccc", "z = 2", None),
        ],
    )
    assert path == snapshot_path(nb)
    doc = json.loads(path.read_text())
    assert doc["version"] == "1"
    assert set(doc["metadata"]) == {"marimo_version", "script_metadata_hash"}
    # Every cell carries its code hash, including one that never produced output.
    assert [c["code_hash"] for c in doc["cells"]] == [
        code_hash("x = 1\nx"),
        code_hash("y = x / 0"),
        code_hash("z = 2"),
    ]
    assert doc["alkera"]["kernel"] == {
        "kernel_id": "krn_1",
        "env_id": "default",
        "env_fingerprint": "E",
    }
    assert doc["cells"][0]["alkera"]["run"]["run_id"] == "run_1"
    assert doc["cells"][1]["outputs"][0]["type"] == "error"

    read = read_snapshot(nb)
    assert read.alkera and read.notices == []
    assert read.kernel == KernelMeta("krn_1", "default", "E")
    got = reattach(
        read, [("aaaaaaaaaa", "x = 1\nx"), ("bbbbbbbbbb", "y = x / 0"), ("cccccccccc", "z = 2")]
    )
    assert got["aaaaaaaaaa"].bundles == [{"text/plain": "1", "text/html": "<b>1</b>"}]
    assert [(i.name, i.text) for i in got["aaaaaaaaaa"].console] == [("stdout", "hi\n")]
    assert got["aaaaaaaaaa"].origin == "saved"
    assert got["aaaaaaaaaa"].run is not None and got["aaaaaaaaaa"].run.finished_at == T0
    # Who ran it survives the file whole: the agent and the person it acted for.
    assert got["aaaaaaaaaa"].run.by == BY
    assert got["aaaaaaaaaa"].run.by.label() == "Agent for Ana"
    assert got["bbbbbbbbbb"].error == ErrorInfo(
        ename="ZeroDivisionError", evalue="division by zero"
    )
    assert got["cccccccccc"].bundles == [] and got["cccccccccc"].error is None


@pytest.mark.parametrize(
    "by",
    [
        pytest.param("user:5b0c7c1e", id="bare-id-from-an-earlier-writer"),
        pytest.param({"kind": "person"}, id="incomplete-object"),
        pytest.param(None, id="missing"),
    ],
)
def test_out_snapshot_run_without_a_readable_requester_keeps_its_outputs(
    tmp_path: Path, by: object
) -> None:
    """An attribution that cannot be read is dropped, never shown as an id;
    the outputs it came with still reattach."""
    nb = _nb(tmp_path)
    a = _ran("x = 1\nx", bundles=[{"text/plain": "1"}])
    path = _write(nb, [SnapshotCell("aaaaaaaaaa", "x = 1\nx", a)])
    doc = json.loads(path.read_text())
    doc["cells"][0]["alkera"]["run"]["by"] = by
    path.write_text(json.dumps(doc))
    got = reattach(read_snapshot(nb), [("aaaaaaaaaa", "x = 1\nx")])
    assert got["aaaaaaaaaa"].bundles == [{"text/plain": "1"}]
    assert got["aaaaaaaaaa"].run is None and got["aaaaaaaaaa"].attribution() is None


def test_out_reattach_only_matching_code(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    _write(
        nb, [SnapshotCell("aaaaaaaaaa", "x = 1", _ran("x = 1", bundles=[{"text/plain": "one"}]))]
    )
    read = read_snapshot(nb)
    assert reattach(read, [("aaaaaaaaaa", "x = 2")]) == {}
    # Same code under a new id still finds its output by hash.
    moved = reattach(read, [("zzzzzzzzzz", "x = 1")])
    assert moved["zzzzzzzzzz"].bundles == [{"text/plain": "one"}]


STOCK_MARIMO = {
    "version": "1",
    "metadata": {"marimo_version": "0.25.1", "script_metadata_hash": None},
    "cells": [
        {
            "id": "Hbol",
            "code_hash": hashlib.md5(b"import marimo as mo").hexdigest(),
            "outputs": [],
            "console": [],
        },
        {
            "id": "MJUe",
            "code_hash": hashlib.md5(b"x = 41 + 1\nx").hexdigest(),
            "outputs": [{"type": "data", "data": {"text/html": "<pre>42</pre>"}}],
            "console": [
                {
                    "type": "stream",
                    "name": "stdout",
                    "text": "computing\n",
                    "mimetype": "text/plain",
                }
            ],
        },
    ],
}


def test_out_stock_marimo_snapshot_reattached_as_origin_unknown(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    path = snapshot_path(nb)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(STOCK_MARIMO), encoding="utf-8")
    read = read_snapshot(nb)
    assert not read.alkera
    got = reattach(read, [("a1b2c3d4e5", "import marimo as mo"), ("f6g7h8j9k0", "x = 41 + 1\nx")])
    out = got["f6g7h8j9k0"]
    assert out.origin == "unknown"
    assert out.bundles[0]["text/html"] == "<pre>42</pre>"
    # Every bundle carries text/plain, even one stock marimo wrote without it.
    assert isinstance(out.bundles[0]["text/plain"], str)
    assert out.lineage_hash is None and out.run is None
    # No provenance: never outdated by lineage, only flagged origin unknown.
    assert not outdated(out, "anything", "other-env")


def _big_png() -> tuple[str, bytes]:
    raw = os.urandom(300 * 1024)
    return base64.b64encode(raw).decode(), raw


def test_out_blobs_over_256kib_stored_by_hash(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    png_b64, raw = _big_png()
    big_html = "<p>" + "x" * (300 * 1024) + "</p>"
    small = {"text/plain": "<Figure>", "image/png": png_b64}
    cell = _ran("fig", bundles=[small, {"text/plain": "html", "text/html": big_html}])
    _write(nb, [SnapshotCell("aaaaaaaaaa", "fig", cell)])
    digest = hashlib.sha256(raw).hexdigest()
    blob = blob_dir(nb) / f"{digest}.png"
    assert blob.read_bytes() == raw
    doc = json.loads(snapshot_path(nb).read_text())
    data = doc["cells"][0]["outputs"][0]["data"]
    assert data["text/plain"] == "<Figure>"
    assert data["image/png"] == {
        "application/vnd.alkera.ref+json": {
            "sha256": digest,
            "mime": "image/png",
            "bytes": len(raw),
        }
    }
    # The JSON stays small; the value lives in the blob.
    assert snapshot_path(nb).stat().st_size < 10_000
    got = reattach(read_snapshot(nb), [("aaaaaaaaaa", "fig")])["aaaaaaaaaa"]
    assert got.bundles[0]["image/png"] == png_b64
    assert got.bundles[1]["text/html"] == big_html


def test_out_value_at_threshold_stays_inline(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    value = "y" * (256 * 1024)
    _write(
        nb,
        [
            SnapshotCell(
                "aaaaaaaaaa", "v", _ran("v", bundles=[{"text/plain": "v", "text/html": value}])
            )
        ],
    )
    assert not blob_dir(nb).exists()
    doc = json.loads(snapshot_path(nb).read_text())
    assert doc["cells"][0]["outputs"][0]["data"]["text/html"] == value


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param("path", id="out.forged_ref_path"),
        pytest.param("hash", id="out.forged_ref_hash_mismatch"),
        pytest.param("not_hex", id="out.forged_ref_not_a_hash"),
    ],
)
def test_out_forged_ref_is_ignored(tmp_path: Path, forge: str) -> None:
    nb = _nb(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read", encoding="utf-8")
    bdir = blob_dir(nb)
    bdir.mkdir(parents=True)
    claimed = hashlib.sha256(b"what the file claims").hexdigest()
    if forge == "hash":
        # A file at the claimed name whose content does not hash to it.
        (bdir / f"{claimed}.txt").write_bytes(b"tampered")
    ref = {"sha256": claimed, "mime": "text/plain", "bytes": 5, "path": str(secret)}
    if forge == "path":
        ref["path"] = "../../../secret.txt"
    if forge == "not_hex":
        ref["sha256"] = "../../secret"
    doc = {
        "version": "1",
        "metadata": {"marimo_version": "0.25.1", "script_metadata_hash": None},
        "alkera": {"schema_version": "1.0.0"},
        "cells": [
            {
                "id": "aaaaaaaaaa",
                "code_hash": code_hash("v"),
                "outputs": [
                    {
                        "type": "data",
                        "data": {
                            "text/plain": "fallback",
                            "text/html": {"application/vnd.alkera.ref+json": ref},
                        },
                    }
                ],
                "console": [],
            }
        ],
    }
    snapshot_path(nb).write_text(json.dumps(doc), encoding="utf-8")
    read = read_snapshot(nb)
    out = reattach(read, [("aaaaaaaaaa", "v")])["aaaaaaaaaa"]
    assert out.bundles == [{"text/plain": "fallback"}]
    assert any("do not read" in json.dumps(b) for b in out.bundles) is False
    assert read.notices == ["missing_blob: cell aaaaaaaaaa text/html"]


def test_out_gc_of_unreferenced_blobs(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    png_b64, raw = _big_png()
    _write(
        nb,
        [
            SnapshotCell(
                "aaaaaaaaaa",
                "fig",
                _ran("fig", bundles=[{"text/plain": "f", "image/png": png_b64}]),
            )
        ],
    )
    old = blob_dir(nb) / f"{hashlib.sha256(raw).hexdigest()}.png"
    assert old.exists()
    png2, raw2 = _big_png()
    _write(
        nb,
        [
            SnapshotCell(
                "aaaaaaaaaa", "fig", _ran("fig", bundles=[{"text/plain": "f", "image/png": png2}])
            )
        ],
    )
    assert not old.exists()
    assert sorted(p.name for p in blob_dir(nb).iterdir()) == [
        f"{hashlib.sha256(raw2).hexdigest()}.png"
    ]
    _write(nb, [SnapshotCell("aaaaaaaaaa", "fig", _ran("fig", bundles=[{"text/plain": "small"}]))])
    assert not blob_dir(nb).exists()


def _lineage(cells: list[tuple[str, str]]) -> dict[str, str]:
    # load -> clean -> plot; summary is unrelated
    edges = [("load", "clean"), ("clean", "plot")]
    return lineage_hashes(cells, edges)


BASE = [
    ("load", "df = read()"),
    ("clean", "c = df.dropna()"),
    ("plot", "c.plot()"),
    ("summary", "print(1)"),
]


@pytest.mark.parametrize(
    ("change", "env_after", "expect"),
    [
        pytest.param(("load", "df = read(2)"), "E", True, id="out.outdated_after_upstream_edit"),
        pytest.param(None, "E2", True, id="out.outdated_after_env_lock_change"),
        pytest.param(
            ("summary", "print(2)"), "E", False, id="out.not_outdated_after_unrelated_edit"
        ),
        pytest.param(None, "E", False, id="out.not_outdated_when_nothing_changed"),
    ],
)
def test_out_outdated(
    tmp_path: Path, change: tuple[str, str] | None, env_after: str, expect: bool
) -> None:
    nb = _nb(tmp_path)
    before = _lineage(BASE)
    plot = _ran("c.plot()", bundles=[{"text/plain": "chart"}], lineage=before["plot"], env="E")
    _write(nb, [SnapshotCell("plot", "c.plot()", plot)])
    saved = reattach(read_snapshot(nb), [("plot", "c.plot()")])["plot"]
    after_cells = [(cid, change[1] if change and cid == change[0] else code) for cid, code in BASE]
    assert outdated(saved, _lineage(after_cells)["plot"], env_after) is expect


def test_out_outputs_of_deleted_cells_dropped(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    png_b64, _ = _big_png()
    a = _ran("a", bundles=[{"text/plain": "a"}])
    b = _ran("b", bundles=[{"text/plain": "b", "image/png": png_b64}])
    _write(nb, [SnapshotCell("aaaaaaaaaa", "a", a), SnapshotCell("bbbbbbbbbb", "b", b)])
    # The next write lists only live cells: b was deleted.
    _write(nb, [SnapshotCell("aaaaaaaaaa", "a", a)])
    doc = json.loads(snapshot_path(nb).read_text())
    assert [c["id"] for c in doc["cells"]] == ["aaaaaaaaaa"]
    assert not blob_dir(nb).exists()
    assert reattach(read_snapshot(nb), [("bbbbbbbbbb", "b")]) == {}


def test_out_gitignore_toggling(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    other = tmp_path / "other.alknb.py"
    cell = [SnapshotCell("aaaaaaaaaa", "a", _ran("a", bundles=[{"text/plain": "a"}]))]
    gi = snapshot_path(nb).parent / ".gitignore"
    _write(nb, cell)
    assert gi.read_text().splitlines() == ["/weekly.alknb.py.json", "/weekly.alknb.py.d/"]
    # A person's own line and another notebook's lines survive every toggle.
    gi.write_text("# mine\n*.tmp\n" + gi.read_text(), encoding="utf-8")
    SnapshotWriter(other).write(SnapshotContent(cells=cell))
    _write(nb, cell, outputs_in_git=True)
    assert gi.read_text().splitlines() == [
        "# mine",
        "*.tmp",
        "/other.alknb.py.json",
        "/other.alknb.py.d/",
    ]
    _write(nb, cell, outputs_in_git=False)
    assert gi.read_text().splitlines()[-2:] == ["/weekly.alknb.py.json", "/weekly.alknb.py.d/"]
    assert gi.read_text().count("/weekly.alknb.py.json") == 1
    # With nothing left to ignore the file goes away.
    gi.unlink()
    _write(nb, cell, outputs_in_git=True)
    assert not gi.exists()


@pytest.mark.parametrize(
    ("text", "notice"),
    [
        pytest.param("{not json", "corrupt_snapshot: not valid JSON", id="out.corrupt_json"),
        pytest.param(
            '["a list"]', "corrupt_snapshot: not a session snapshot", id="out.corrupt_shape"
        ),
        pytest.param(
            '{"version": "1", "cells": [42, '
            '{"id": "ok", "code_hash": null, "outputs": [], "console": []}]}',
            "corrupt_snapshot: a cell could not be read",
            id="out.corrupt_cell",
        ),
        pytest.param(b"\xff\xfe\x00", "corrupt_snapshot: not valid JSON", id="out.corrupt_bytes"),
    ],
)
def test_out_corrupt_snapshot_ignored_with_notice(
    tmp_path: Path, text: str | bytes, notice: str
) -> None:
    nb = _nb(tmp_path)
    path = snapshot_path(nb)
    path.parent.mkdir(parents=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    read = read_snapshot(nb)
    assert read.notices == [notice]
    assert reattach(read, [("aaaaaaaaaa", "x = 1")]) == {}


def test_out_missing_snapshot_reads_empty(tmp_path: Path) -> None:
    read = read_snapshot(_nb(tmp_path))
    assert read.cells == [] and read.notices == []


class StepClock:
    """A clock whose ``sleep`` returns only when the test advances time."""

    def __init__(self) -> None:
        self.t = 0.0
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return T0

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self.t + seconds, fut))
        await fut

    async def advance(self, seconds: float) -> None:
        self.t += seconds
        for due, fut in list(self._waiters):
            if due <= self.t and not fut.done():
                fut.set_result(None)
                self._waiters.remove((due, fut))
        for _ in range(20):
            await asyncio.sleep(0)


async def _settle(sched: SnapshotScheduler, writes: int) -> None:
    for _ in range(200):
        if sched.writes >= writes:
            return
        await asyncio.sleep(0.005)


async def test_out_debounce_coalesces_and_flush_writes_now(tmp_path: Path) -> None:
    nb = _nb(tmp_path)
    clock = StepClock()
    state = {"text": "v0"}
    saved: list[int] = []

    def produce() -> SnapshotContent:
        out = _ran("a", bundles=[{"text/plain": state["text"]}])
        return SnapshotContent(cells=[SnapshotCell("aaaaaaaaaa", "a", out)])

    sched = SnapshotScheduler(
        SnapshotWriter(nb), produce, clock, debounce_s=1.0, on_written=lambda p, n: saved.append(n)
    )
    for i in range(50):
        state["text"] = f"v{i}"
        sched.schedule()
        await clock.advance(0.01)
    assert sched.writes == 0 and not snapshot_path(nb).exists()
    await clock.advance(1.0)
    await _settle(sched, 1)
    assert sched.writes == 1 and saved == [1]
    # Content is produced at write time: the last state, not the first.
    assert (
        reattach(read_snapshot(nb), [("aaaaaaaaaa", "a")])["aaaaaaaaaa"].bundles[0]["text/plain"]
        == "v49"
    )

    state["text"] = "suspending"
    sched.schedule()
    await sched.flush()
    assert sched.writes == 2
    assert (
        reattach(read_snapshot(nb), [("aaaaaaaaaa", "a")])["aaaaaaaaaa"].bundles[0]["text/plain"]
        == "suspending"
    )
    # The flushed debounce does not write again later.
    await clock.advance(5.0)
    await _settle(sched, 3)
    assert sched.writes == 2

    sched.schedule()
    await sched.close()
    assert sched.writes == 3
    sched.schedule()
    await clock.advance(5.0)
    assert sched.writes == 3


def test_env_fingerprint_fields_are_unambiguous() -> None:
    assert env_fingerprint("uv", b"ab", "3.13", "x") != env_fingerprint("uv", b"a", "b3.13", "x")
    assert env_fingerprint("uv", b"ab", "3.13", "x") == env_fingerprint("uv", b"ab", "3.13", "x")


def _streamed(*steps: tuple[str, str]) -> CellOutputs:
    """Outputs folded from kernel notifications in the order given: ``("out",
    text)`` a printed line, ``("err", text)`` stderr, ``("value", text)`` a
    rich output."""
    out = CellOutputs()
    for kind, text in steps:
        if kind == "value":
            out.apply_output({"text/plain": text}, "append", LIMITS)
        else:
            out.apply_stream("stdout" if kind == "out" else "stderr", text, LIMITS)
    return out


def _order(out: CellOutputs) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []
    for item in out.items("c"):
        if item.type == "stream":
            shown.append((item.name, item.text))
        elif item.type == "display":
            shown.append(("value", str(item.data["text/plain"])))
        else:
            shown.append(("error", item.error.ename))
    return shown


@pytest.mark.parametrize(
    ("steps", "shown"),
    [
        pytest.param(
            [("out", "a\n"), ("value", "V")],
            [("stdout", "a\n"), ("value", "V")],
            id="printed_then_value",
        ),
        pytest.param(
            [("value", "V"), ("out", "a\n")],
            [("value", "V"), ("stdout", "a\n")],
            id="value_then_printed",
        ),
        pytest.param(
            [("out", "a\n"), ("value", "1"), ("out", "b\n"), ("value", "2"), ("err", "c\n")],
            [
                ("stdout", "a\n"),
                ("value", "1"),
                ("stdout", "b\n"),
                ("value", "2"),
                ("stderr", "c\n"),
            ],
            id="interleaved",
        ),
        pytest.param(
            [("out", "a"), ("out", "b\n"), ("value", "V")],
            [("stdout", "ab\n"), ("value", "V")],
            id="adjacent_writes_merge",
        ),
        pytest.param(
            [("out", "a\n"), ("value", "V"), ("out", "b\n")],
            [("stdout", "a\n"), ("value", "V"), ("stdout", "b\n")],
            id="a_value_splits_one_stream",
        ),
    ],
)
def test_out_items_keep_the_order_the_kernel_sent(
    steps: list[tuple[str, str]], shown: list[tuple[str, str]]
) -> None:
    assert _order(_streamed(*steps)) == shown


def test_out_snapshot_keeps_the_order_the_kernel_sent(tmp_path: Path) -> None:
    nb = tmp_path / "nb.alknb.py"
    nb.write_text("x\n")
    out = _streamed(("out", "a\n"), ("value", "1"), ("out", "b\n"), ("value", "2"))
    SnapshotWriter(nb).write(SnapshotContent(cells=[SnapshotCell("c", "x", out)]))
    (saved,) = read_snapshot(nb).cells
    assert _order(saved.outputs) == _order(out)


def test_out_a_snapshot_with_no_recorded_order_shows_the_console_after_the_values(
    tmp_path: Path,
) -> None:
    """A stock marimo snapshot, or one an earlier writer made, records no
    order: its console follows its rich outputs, as before."""
    nb = tmp_path / "nb.alknb.py"
    nb.write_text("x\n")
    out = _streamed(("out", "a\n"), ("value", "1"))
    SnapshotWriter(nb).write(SnapshotContent(cells=[SnapshotCell("c", "x", out)]))
    path = snapshot_path(nb)
    doc = json.loads(path.read_text())
    del doc["cells"][0]["alkera"]["console_after"]
    path.write_text(json.dumps(doc))
    (saved,) = read_snapshot(nb).cells
    assert _order(saved.outputs) == [("value", "1"), ("stdout", "a\n")]
