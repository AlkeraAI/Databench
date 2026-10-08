"""Regression test for ops/scripts/gen-openapi.sh.

The script pipes `app.openapi()` to stdout and redirects to openapi.json.
Historically log output was prepended to stdout, corrupting the JSON.
This test runs the real script and asserts the output parses cleanly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "gen-openapi.sh"
OUT = REPO_ROOT / "packages" / "shared-openapi" / "open" / "openapi.json"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="gen-openapi.sh is POSIX dev/CI tooling; artifact drift is enforced "
    "by the Linux drift job, not a Windows product path",
)
def test_gen_openapi_produces_clean_json():
    assert SCRIPT.exists(), f"script missing: {SCRIPT}"

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"script failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert OUT.exists(), "openapi.json was not written"

    raw = OUT.read_text(encoding="utf-8")
    # The file must parse as JSON end-to-end — no log lines or banner text.
    schema = json.loads(raw)

    assert raw.lstrip().startswith("{"), "openapi.json starts with non-JSON content"
    assert schema.get("openapi", "").startswith("3."), "missing or wrong openapi version"
    assert "paths" in schema, "openapi.json missing 'paths'"
    assert "/health/live" in schema["paths"], "health routes missing from schema"
