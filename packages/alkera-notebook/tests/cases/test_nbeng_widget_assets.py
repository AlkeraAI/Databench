"""Widget assets through the engine: a kernel's ``widget.asset`` offer
(handcrafted here, through the session's notification dispatch on a live
kernel) is stored and served only to the notebook it was offered for;
refused offers leave a notice and nothing behind; large widget values move
to the store only when the engine was given one."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.engine import NotebookClient, NotebookSession, NotFoundError
from alkera_notebook.events.models import NoticeEvent
from alkera_notebook.rpc import frames as f
from alkera_notebook.widgets.assets import (
    ASSET_REF_PREFIX,
    MemoryBlobStore,
    WidgetAssets,
    workspace_scope,
)
from nbeng_harness import engine_for, notebook, run_cells

BQ = b"define(['@jupyter-widgets/base'], function(b){ return {}; });"
HELPER = b"// helper"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def notify(session: NotebookSession, method: str, params: dict[str, Any]) -> None:
    runtime = session.runtime
    assert runtime.kernel is not None
    runtime.on_kernel_notification(runtime.kernel, method, params)


def bq_offer(**override: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "module": "bqplot",
        "version": "0.13.1",
        "files": [
            {"path": "index.js", "sha256": sha(BQ)},
            {"path": "lib/helper.js", "sha256": sha(HELPER)},
        ],
        "buffers": [f.Segment(BQ), f.Segment(HELPER)],
    }
    params.update(override)
    return params


def notices(client: NotebookClient) -> list[NoticeEvent]:
    return [e for e in list(client.queue._items) if isinstance(e, NoticeEvent)]


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(bq_offer(), id="bytes-as-buffers"),
        pytest.param(
            {
                "module": "bqplot",
                "version": "0.13.1",
                "files": [
                    {"path": "index.js", "sha256": sha(BQ), "data": f.Segment(BQ)},
                    {"path": "lib/helper.js", "sha256": sha(HELPER), "data": HELPER},
                ],
            },
            id="bytes-per-file",
        ),
    ],
)
async def test_an_offer_is_served_to_its_notebook_only(
    tmp_path: Path, params: dict[str, Any]
) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        _other, bob, _ = await notebook(engine, ["y = 1"], path="other.alknb.py")
        await run_cells(ann, a)
        notify(session, "widget.asset", params)
        ref = await ann.widget_asset_resolve("bqplot", "0.13.1")
        assert (ref.module, ref.version, ref.kind) == ("bqplot", "0.13.1", "environment")
        assert ref.sha256 == sha(BQ) and ref.bytes == len(BQ)
        assert await ann.widget_asset(ref.sha256) == BQ
        # A module's other files are fetched by the hash the kernel named.
        assert await ann.widget_asset(sha(HELPER)) == HELPER
        # A version range the kernel did not name resolves to the offered one.
        assert (await ann.widget_asset_resolve("bqplot", "^0.13.0")).sha256 == sha(BQ)
        # Another notebook of the same engine was never offered it.
        with pytest.raises(NotFoundError):
            await bob.widget_asset_resolve("bqplot", "0.13.1")
        for data in (BQ, HELPER):
            with pytest.raises(NotFoundError):
                await bob.widget_asset(sha(data))
        with pytest.raises(NotFoundError):
            await ann.widget_asset_resolve("ipydatagrid")
        assert not notices(ann)


async def test_an_exact_offered_version_wins_and_otherwise_the_latest(tmp_path: Path) -> None:
    old, new = b"// old", b"// new"
    async with engine_for(tmp_path) as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        for version, body in (("1.0.0", old), ("2.0.0", new)):
            notify(
                session,
                "widget.asset",
                {
                    "module": "lib",
                    "version": version,
                    "files": [{"path": "index.js", "sha256": sha(body), "data": f.Segment(body)}],
                },
            )
        assert (await ann.widget_asset_resolve("lib", "1.0.0")).sha256 == sha(old)
        assert (await ann.widget_asset_resolve("lib", "2.0.0")).version == "2.0.0"
        latest = await ann.widget_asset_resolve("lib")
        assert (latest.sha256, latest.version) == (sha(new), "2.0.0")
        assert await ann.widget_asset(sha(old)) == old


@pytest.mark.parametrize(
    ("params", "why"),
    [
        pytest.param(bq_offer(module="@jupyter-widgets/base"), "platform", id="platform-name"),
        pytest.param(bq_offer(module="@alkera/widgets"), "platform", id="platform-manager"),
        pytest.param(
            bq_offer(buffers=[f.Segment(b"tampered"), f.Segment(HELPER)]),
            "hash",
            id="hash-mismatch",
        ),
        pytest.param(bq_offer(buffers=[f.Segment(BQ)]), "do not match", id="buffer-count"),
        pytest.param(bq_offer(module=None), "module and version", id="no-module"),
        pytest.param(bq_offer(version=3), "module and version", id="version-not-text"),
        pytest.param(bq_offer(files="index.js"), "no files", id="files-not-a-list"),
        pytest.param(
            bq_offer(files=["index.js"], buffers=[f.Segment(BQ)]), "object", id="file-not-object"
        ),
        pytest.param(
            {
                "module": "bqplot",
                "version": "1",
                "files": [{"path": "index.js", "sha256": sha(BQ)}],
            },
            "bytes",
            id="no-bytes",
        ),
        pytest.param(
            bq_offer(
                files=[
                    {"path": "../x.js", "sha256": sha(BQ)},
                    {"path": "a.js", "sha256": sha(HELPER)},
                ]
            ),
            "not a path inside the module",
            id="path-walks-out",
        ),
    ],
)
async def test_a_refused_offer_leaves_a_notice_and_nothing_to_fetch(
    tmp_path: Path, params: dict[str, Any], why: str
) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        while len(ann.queue):
            ann.queue._items.popleft()
        notify(session, "widget.asset", params)
        [notice] = notices(ann)
        assert notice.notice.kind == "widget_asset_refused"
        assert why in notice.notice.message
        for module in ("bqplot", "@jupyter-widgets/base", "@alkera/widgets"):
            with pytest.raises(NotFoundError):
                await ann.widget_asset_resolve(module)
        for data in (BQ, HELPER, b"tampered"):
            with pytest.raises(NotFoundError):
                await ann.widget_asset(sha(data))


async def test_platform_bundles_are_served_to_every_notebook(tmp_path: Path) -> None:
    assets = WidgetAssets(MemoryBlobStore())
    bundle = assets.add_platform_bundle("@alkera/widgets", "0.1.0", b"var W={};")
    async with engine_for(tmp_path, widget_assets=assets) as engine:
        _session, ann, _ = await notebook(engine, ["x = 1"])
        ref = await ann.widget_asset_resolve("@alkera/widgets")
        assert (ref.sha256, ref.kind) == (bundle.sha256, "platform")
        assert await ann.widget_asset(bundle.sha256) == b"var W={};"


def big_widget(comm_id: str) -> dict[str, Any]:
    esm = "export default { render() {} };" + " " * (70 * 1024)
    return {
        "comm_id": comm_id,
        "content": {
            "target_name": "jupyter.widget",
            "data": {"state": {"_model_name": "AnyModel", "_view_name": "AnyView", "_esm": esm}},
        },
        "parent_msg_id": None,
    }


@pytest.mark.parametrize("given", [False, True], ids=["no-store-inline", "store-moves-it"])
async def test_large_widget_code_moves_to_the_store_only_when_one_was_given(
    tmp_path: Path, given: bool
) -> None:
    assets = WidgetAssets(MemoryBlobStore()) if given else None
    async with engine_for(tmp_path, widget_assets=assets) as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        notify(session, "comm.open", big_widget("any-1"))
        [open_] = ann.attach_frame(model_id="any-1").opens
        esm = open_.message["data"]["state"]["_esm"]
        if not given:
            assert esm.startswith("export default")
            return
        assert esm.startswith(ASSET_REF_PREFIX)
        moved = esm[len(ASSET_REF_PREFIX) :]
        assert (await ann.widget_asset(moved)).startswith(b"export default")


async def test_notebooks_of_one_workspace_share_one_stored_copy(tmp_path: Path) -> None:
    store = MemoryBlobStore()
    async with engine_for(tmp_path, widget_assets=WidgetAssets(store)) as engine:
        first, ann, (a,) = await notebook(engine, ["x = 1"])
        second, bob, (b,) = await notebook(engine, ["y = 1"], path="other.alknb.py")
        await run_cells(ann, a)
        await run_cells(bob, b)
        notify(first, "widget.asset", bq_offer())
        notify(second, "widget.asset", bq_offer())
        workspace = workspace_scope(engine.config.org_id, engine.config.workspace_id)
        assert sorted(store._blobs) == sorted([(workspace, sha(BQ)), (workspace, sha(HELPER))])
        # Each notebook still fetches only through its own offer.
        assert await ann.widget_asset(sha(BQ)) == BQ
        assert await bob.widget_asset(sha(BQ)) == BQ
