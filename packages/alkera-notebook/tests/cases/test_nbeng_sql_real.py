"""SQL from the real kernel's ``alkera`` client through the engine's broker."""

from __future__ import annotations

from pathlib import Path

import pytest
from nbeng_fakes import duck_provider
from nbeng_harness import BOB, engine_for, notebook, run_cells, text_of

pytest.importorskip("pyarrow")


async def test_sql_real_kernel_query_runs_on_the_workspace_connection(tmp_path: Path) -> None:
    provider = duck_provider(tmp_path / "db", [4, 5, 6])
    code = (
        "from alkera._sql import sql\n"
        "df = sql('select v from t order by v', connection='Warehouse')\n"
        "total = int(sum(df['v']))\n"
        "total"
    )
    async with engine_for(tmp_path, sql=provider) as engine:
        session, _ann, (a,) = await notebook(engine, [code])
        bob = session.attach(BOB)
        record = await run_cells(bob, a)
        assert record.status == "ok", (await bob.output(a, "error")).error
        text = await text_of(bob, a)
        assert text.splitlines()[-1] == "15" and "6" in text  # the frame is shown, then the value
        assert provider.executed == [("Warehouse", BOB.id, record.run_id)]
