"""An added dbt project mapping one file to one warehouse relation, shared by the two
seams that drive the gate's wiring: the decision engine and the harness permission loop."""

from __future__ import annotations

import json
from pathlib import Path

from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.connections_store import AddedConnectionsStore
from alkera_core.project import ProjectDirectory

MODEL_FILE, ORDERS_RELATION = "proj/models/orders.sql", "wh.sales.orders"


def dbt_project(
    root: Path, *, relation: str = ORDERS_RELATION, manifest: str | None = None
) -> None:
    """An empty ``manifest`` writes no manifest file at all."""
    target = root / "proj" / "target"
    target.mkdir(parents=True)
    node = {"resource_type": "model", "original_file_path": "models/orders.sql", "name": "orders"}
    body = json.dumps({"metadata": {}, "nodes": {"m.orders": node | {"relation_name": relation}}})
    body = body if manifest is None else manifest
    if body:
        (target / "manifest.json").write_text(body)
    attributes = {"project_dir": str(target.parent), "manifest_path": str(target / "manifest.json")}
    conn = Connection(handle="dbt", plugin="dbt", attributes=attributes)
    AddedConnectionsStore(ProjectDirectory(root / ".alkera").connections_path).add(conn)
