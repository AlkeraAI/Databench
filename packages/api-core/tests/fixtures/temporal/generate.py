"""Regenerate the Temporal history-shape fixture corpus at the CURRENT writer version.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/temporal/generate.py

Writes every shape that crosses workflow history into
``packages/api-core/tests/fixtures/temporal/v<X_Y_Z>/``. Commit the diff alongside the schema
change that prompted the regeneration.

NEVER edit old fixture files by hand. Migrations go in the model's
``MIGRATIONS`` dict; fixtures stay frozen as historical evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from alkera_core.schemas.temporal import (
    DrainInput,
    DrainOutcome,
    DrainReport,
    SweepInput,
    ToolCallActivityInput,
)
from alkera_core.versioning import VersionedModel

_NOW = datetime(2026, 9, 5, 12, 30, tzinfo=UTC)


def _shapes() -> dict[str, dict[str, VersionedModel]]:
    return {
        "drain_input": {
            "defaults": DrainInput(),
            "pinned_clock": DrainInput(now=_NOW, limit=25, max_passes=3),
            "no_locked_retries": DrainInput(locked_retries=0, locked_retry_seconds=0.0),
        },
        "sweep_input": {
            "defaults": SweepInput(),
            "pinned_clock": SweepInput(now=_NOW),
        },
        "drain_outcome": {
            "empty": DrainOutcome(),
            "full_page": DrainOutcome(applied=100),
            "mixed_page": DrainOutcome(applied=97, failed=3),
            "all_failed": DrainOutcome(failed=100),
            "skipped_locked": DrainOutcome(skipped_locked=True),
            "skipped_unconfigured": DrainOutcome(skipped_unconfigured=True),
        },
        "drain_report": {
            "empty": DrainReport(),
            "two_passes": DrainReport(passes=2, processed=137, emails=4, locked_retries=1),
        },
        "tool_call_activity_input": {
            "minimal": ToolCallActivityInput(
                tool_name="read",
                session_id="ses_01",
                call_id="call_01",
                org_id="1f4e2c9a-2d5b-4a7c-9e3f-0b6a8b6e3f2c",
                input_ref="blob:sha256:0123abcd",
                idempotency_key="ses_01:call_01",
            ),
        },
    }


def main() -> None:
    root = Path(__file__).resolve().parent
    for category, shapes in _shapes().items():
        for name, model in shapes.items():
            version_dir = root / f"v{model.SCHEMA_VERSION.replace('.', '_')}" / category
            version_dir.mkdir(parents=True, exist_ok=True)
            path = version_dir / f"{name}.json"
            path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n")
            print(f"wrote {path.relative_to(root.parents[2])}")


if __name__ == "__main__":
    main()
