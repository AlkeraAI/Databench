"""The kernel's data directory reaches it in its own spelling.

The engine writes a large SQL result as a file in the kernel's data
directory (``<data_root>/kernels/<kernel id>``) and tells the kernel the
file's name; the kernel opens it under ITS spelling of that directory, which
the launcher passes as ``ALKERA_DATA_DIR``. Locally the two spellings are the
same; in the platform's kernel sandbox the directory is a bind seen at
another path. A launcher that respells it, as the sandbox's does, is stood in
here by one that hands the kernel a link to the directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.kernels.launch_local import (
    KERNELS_SUBDIR,
    LaunchSpec,
    LocalSubprocessLauncher,
    kernel_data_dir,
)
from nbeng_fakes import duck_provider
from nbeng_harness import BOB, engine_for, notebook, run_cells, text_of

pytest.importorskip("pyarrow")

SHOW_DIR = "import alkera._host as h\nprint(h.current().data_dir)"


class RespellingLauncher:
    """The local launcher, handing the kernel its data directory through a
    link elsewhere (the way a sandbox sees the bind at its own path)."""

    def __init__(self, alias_root: Path) -> None:
        self.inner = LocalSubprocessLauncher()
        self.alias_root = alias_root
        self.seen: list[Path] = []

    def launch(self, spec: LaunchSpec) -> Any:
        assert spec.data_dir is not None
        alias = self.alias_root / spec.kernel_id
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(spec.data_dir, target_is_directory=True)
        self.seen.append(spec.data_dir)
        return self.inner.launch(
            LaunchSpec(**{**spec.__dict__, "data_dir": alias})  # type: ignore[arg-type]
        )

    def kill_all(self) -> None:
        self.inner.kill_all()


async def test_a_local_kernel_is_told_the_engine_s_own_data_dir(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, _ann, (cell,) = await notebook(engine, [SHOW_DIR])
        bob = session.attach(BOB)
        record = await run_cells(bob, cell)
        assert record.status == "ok", (await bob.output(cell, "error")).error
        kernel_id = (await bob.kernel("status")).kernel_id
        assert kernel_id is not None
        expected = kernel_data_dir(engine.config.data_root, kernel_id)
        assert expected == Path(engine.config.data_root) / KERNELS_SUBDIR / kernel_id
        assert (await text_of(bob, cell)).strip().splitlines()[0] == str(expected)


async def test_a_large_result_is_read_at_the_kernel_s_spelling_of_the_data_dir(
    tmp_path: Path,
) -> None:
    """The engine writes the file at its own path; the kernel, told only the
    launcher's spelling, opens it there, and the hello's spelling is not what
    it used."""
    launcher = RespellingLauncher(tmp_path / "seen-by-kernel")
    provider = duck_provider(tmp_path / "db", list(range(5000)))
    code = (
        SHOW_DIR
        + "\nfrom alkera._sql import sql\n"
        + "df = sql('select v from t', connection='Warehouse')\n"
        + "len(df)"
    )
    async with engine_for(tmp_path, sql=provider, launcher=launcher) as engine:
        engine.sql_broker().inline_limit = 1024
        session, _ann, (cell,) = await notebook(engine, [code])
        bob = session.attach(BOB)
        record = await run_cells(bob, cell)
        assert record.status == "ok", (await bob.output(cell, "error")).error
        lines = (await text_of(bob, cell)).strip().splitlines()
        (engine_side,) = launcher.seen
        kernel_side = launcher.alias_root / engine_side.name
        assert lines[0] == str(kernel_side) != str(engine_side)
        assert lines[-1] == "5000"
        # The file went through the directory and was taken away after.
        assert [n for n in os.listdir(engine_side) if n != "kernel.log"] == []
