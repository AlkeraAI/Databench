"""Write the permission presentation's generated files.

* ``alkera_core/permission_presentation/_alkera_tools.py`` -- the alkera tool
  names, from the tool manifest, so the server resolves a tool the way the web
  does;
* ``packages/chat-model/src/generated/permissionPresentation.ts`` -- the
  presenter registry, redaction rules and permission modes, as data;
* ``packages/chat-model/src/generated/permissionPresentationVectors.json`` --
  the conformance vectors the TS twin must reproduce.

Run via ``make gen-tool-manifest`` (after the tool manifest export).
"""

from __future__ import annotations

import importlib
import sys


def main() -> int:
    package = "alkera_core.permission_presentation"
    export = importlib.import_module(f"{package}.export")
    # The tool names are themselves generated here, and the presenter reads
    # them at import: write them first, then reload what read the old list so
    # the vectors are computed against the new one.
    export.write(export.ALKERA_TOOLS_PATH, export.render_alkera_tools())
    for module in ("_alkera_tools", "present", "export"):
        importlib.reload(importlib.import_module(f"{package}.{module}"))
    export = importlib.import_module(f"{package}.export")
    for path, content in export.outputs().items():
        export.write(path, content)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
