"""Export the daemon's JSON-RPC protocol as JSON Schema documents.

Reads the Pydantic models registered via the `@method`, `@client_request`,
and `@notification` decorators in `apps/cli/alkera_cli/daemon/protocol.py`
and emits a schema file the TypeScript codegen consumes. One composition per
run, each in its own process:

* the product protocol (the default): every method the daemon serves with the
  composition `CLI_INSTALL` names installed (the open CLI's when unset), the
  open ones plus each installed contribution, written where `--out` says;
* the open protocol (`--open`): what the open daemon registers with no
  extension installed, written to `packages/shared-openapi/open/`.

The open run selects by composition, not by filtering: it installs nothing and
then loads the daemon's methods, so its output is exactly what an open-only
daemon serves.

Output shape:

    {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "title": "AlkeraDaemonProtocol",
      "$defs": {
        "PingRequest": {...},
        "PingResponse": {...},
        ...
      },
      "x-alkera-methods": {
        "ping": { "request": "PingRequest", "response": "PingResponse" },
        ...
      },
      "x-alkera-clientRequests": {
        "auth.requestLogin": { "request": "AuthRequestLoginRequest",
                                "response": "AuthRequestLoginResponse" }
      },
      "x-alkera-notifications": {
        "harness.event": "HarnessEventNotification",
        ...
      }
    }

`json-schema-to-typescript` reads this and emits one named type per
`$defs` entry; we then wrap the resulting `messages.ts` with hand-written
method/notification union types that drive the typed client wrapper.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alkera_cli.daemon.protocol import (
    CLIENT_REQUESTS,
    METHODS,
    NOTIFICATIONS,
)
from open_subset import (
    OPEN_DIR,
    Composition,
    install_composition,
    parse_export,
)
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
#: The open protocol is this tree's own artifact. The product's protocol belongs
#: to whatever consumes it (the editor extension, outside this tree), so the
#: product run names its output with ``--out`` and there is no default for it.
OPEN_OUTPUT = OPEN_DIR / "daemon-schema.json"


def register_methods(composition: Composition) -> None:
    """Register what the composition's daemon serves: the installed extensions
    first, then the open handlers and every installed contribution.

    The extensions are installed before the open daemon is imported, the order
    its own entry point keeps: a point read during import would freeze empty
    and refuse the install."""
    install_composition(composition)
    from alkera_cli.daemon.methods import load_methods

    load_methods()


def build_schema() -> dict[str, Any]:
    """The protocol of every entry registered in this process."""
    defs: dict[str, dict[str, Any]] = {}
    methods_index: dict[str, dict[str, str]] = {}
    client_requests_index: dict[str, dict[str, str]] = {}
    notifications_index: dict[str, str] = {}

    def register(model_cls: type[BaseModel]) -> str:
        schema = model_cls.model_json_schema(
            ref_template="#/$defs/{model}",
            mode="serialization",
        )
        # Pydantic emits nested model schemas under "$defs"; hoist them up.
        nested = schema.pop("$defs", {})
        for name, body in nested.items():
            defs.setdefault(name, body)
        defs.setdefault(model_cls.__name__, schema)
        return model_cls.__name__

    for method_name, spec in sorted(METHODS.items()):
        methods_index[method_name] = {
            "request": register(spec.request_type),
            "response": register(spec.response_type),
        }

    for client_method, (req_cls, resp_cls) in sorted(CLIENT_REQUESTS.items()):
        client_requests_index[client_method] = {
            "request": register(req_cls),
            "response": register(resp_cls),
        }

    for notif_name, model_cls in sorted(NOTIFICATIONS.items()):
        notifications_index[notif_name] = register(model_cls)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AlkeraDaemonProtocol",
        "$defs": defs,
        "x-alkera-methods": methods_index,
        "x-alkera-clientRequests": client_requests_index,
        "x-alkera-notifications": notifications_index,
    }


def export(composition: Composition) -> dict[str, Any]:
    """Register the composition's daemon in this process and return its
    protocol."""
    register_methods(composition)
    return build_schema()


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    composition, out = parse_export(argv, "Export the daemon JSON-RPC schema.")
    if out is None and composition == "product":
        print("the product protocol has no default output; pass --out <path>", file=sys.stderr)
        return 2
    out = out or OPEN_OUTPUT
    out.parent.mkdir(parents=True, exist_ok=True)

    schema = export(composition)
    out.write_text(render(schema), encoding="utf-8")
    print(f"✓ wrote {out}")
    print(
        f"  methods={len(schema['x-alkera-methods'])} "
        f"clientRequests={len(schema['x-alkera-clientRequests'])} "
        f"notifications={len(schema['x-alkera-notifications'])} "
        f"defs={len(schema['$defs'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
