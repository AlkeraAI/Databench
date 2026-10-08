"""Merge node bundle directories (each written by scripts/build-node-bundle.sh)
into one, under one manifest.json, refusing parts built as different versions.

    python3 scripts/merge-node-bundles.py OUT PART [PART ...]
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def merge(out: Path, parts: list[Path]) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    version: str | None = None
    targets: dict[str, object] = {}
    for part in parts:
        manifest = json.loads((part / "manifest.json").read_text(encoding="utf-8"))
        if version not in (None, manifest["version"]):
            raise SystemExit(f"{part} holds version {manifest['version']}, not {version}")
        version = manifest["version"]
        for target, entry in manifest["targets"].items():
            name = Path(entry["file"]).name
            shutil.copy2(part / name, out / name)
            shutil.copy2(part / f"{name}.sha256", out / f"{name}.sha256")
            targets[target] = entry
    merged: dict[str, object] = {"version": version, "targets": targets}
    (out / "manifest.json").write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return merged


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    merge(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]])
