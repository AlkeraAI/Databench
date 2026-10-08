"""Widget assets end to end: a real kernel running in an environment that
holds a widget library offers the library's JavaScript when a comm opens,
and the engine stores and serves exactly those bytes to that notebook only."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from alkera_notebook.engine import NotFoundError
from alkera_notebook.envs.static import StaticEnvRegistry
from nbeng_harness import engine_for, notebook, run_cells

INDEX = b"define(['@jupyter-widgets/base'], function (base) { return {GridModel: 1}; });\n"
LOGO = b"\x89PNG fake"

OPEN = (
    "import sys\nhost = sys.modules['_alkera_runtime'].host\n"
    "state = {'_model_name': 'GridModel', '_model_module': 'fakegrid',"
    " '_model_module_version': '^1.0.0', '_view_module': 'fakegrid',"
    " '_view_module_version': '^1.0.0'}\n"
    "comm = host.open_comm('jupyter.widget', {'state': state, 'buffer_paths': []}, {},"
    " lambda m: None).comm_id\n"
)


def _env_with_library(root: Path) -> str:
    env = root / "env"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env)], check=True)
    lib = env / "share" / "jupyter" / "nbextensions" / "fakegrid"
    (lib / "static").mkdir(parents=True)
    (lib / "index.js").write_bytes(INDEX)
    (lib / "static" / "logo.png").write_bytes(LOGO)
    return str(env / "bin" / "python")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("index.js", INDEX, id="entry"),
        pytest.param("static/logo.png", LOGO, id="static_file"),
    ],
)
async def test_a_real_kernels_offer_is_served_to_its_notebook(
    tmp_path: Path, path: str, body: bytes
) -> None:
    envs = StaticEnvRegistry(_env_with_library(tmp_path))
    async with engine_for(tmp_path, envs=envs) as engine:
        session, ann, (a,) = await notebook(engine, [OPEN])
        _other, bob, (b,) = await notebook(engine, ["y = 1"], path="other.alknb.py")
        assert (await run_cells(ann, a, timeout_s=60)).status == "ok"
        assert (await run_cells(bob, b, timeout_s=60)).status == "ok"

        # The entry resolves by module; every other file by the hash the
        # kernel offered it under.
        if path == "index.js":
            sha = (await ann.widget_asset_resolve("fakegrid")).sha256
        else:
            runtime = session.runtime
            offered = engine.widget_assets.offered(runtime.asset_scope)
            [sha] = [e.sha256 for e in offered if e.name == f"fakegrid/{path}"]
        assert sha == hashlib.sha256(body).hexdigest()
        assert await ann.widget_asset(sha) == body
        # Another notebook's kernel never offered it.
        with pytest.raises(NotFoundError):
            await bob.widget_asset_resolve("fakegrid")
        with pytest.raises(NotFoundError):
            await bob.widget_asset(sha)


async def test_a_kernel_without_the_library_offers_nothing(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _session, ann, (a,) = await notebook(engine, [OPEN])
        assert (await run_cells(ann, a, timeout_s=60)).status == "ok"
        with pytest.raises(NotFoundError):
            await ann.widget_asset_resolve("fakegrid")
