"""The receipt the server stores and the receipt the object page renders.

The receipt is the trust surface: SQL, connection, role, engine,
principal, timestamp, rows, duration, parameters, rendered inline. It crosses
the boundary inside ``WorkspaceObjectRead.spec``, which is ``dict[str, Any]``
in the schema — so the generated SDK carries no ``Receipt`` type, the browser
reads the keys by hand, and nothing checks that the names agree.

This reads the key list out of the browser's own declaration —
``apps/web/src/pages/workspace/objects/receipt.ts`` exports ``RECEIPT_FIELDS``,
and ``ObjectPage.tsx`` renders exactly that table — and holds it against the
model the server writes. A key the page asks for that the server never writes
renders as an em dash forever, which is the failure this exists to catch: the
panel asked for ``connection`` and ``principal`` while the receipt has always
held ``connection_name`` and ``principal_chain``.
"""

from __future__ import annotations

import re
from pathlib import Path

from alkera_core.schemas.objects import Receipt

RECEIPT_MODULE = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "web"
    / "src"
    / "pages"
    / "workspace"
    / "objects"
    / "receipt.ts"
)

#: ``export const RECEIPT_FIELDS … = [ … ];`` — the exported constant, not the
#: JSX that consumes it, so the pin survives a re-layout of the panel.
_DECLARATION = re.compile(r"export const RECEIPT_FIELDS[^=]*=\s*\[(?P<body>.*?)\]\s*;", re.DOTALL)


def _rendered_receipt_keys() -> list[str]:
    """The ``RECEIPT_FIELDS`` table the page renders, in its own order."""
    source = RECEIPT_MODULE.read_text()
    block = _DECLARATION.search(source)
    assert block is not None, f"{RECEIPT_MODULE.name} no longer exports RECEIPT_FIELDS"
    keys = re.findall(r'key:\s*"([^"]+)"', block.group("body"))
    assert keys, "RECEIPT_FIELDS declares no keys"
    return keys


def test_every_receipt_field_the_object_page_renders_is_one_the_server_writes() -> None:
    written = set(Receipt.model_fields)
    asked = _rendered_receipt_keys()
    missing = [key for key in asked if key not in written]
    assert not missing, (
        f"the object page renders receipt keys the server never writes: {missing}. "
        f"The server's Receipt declares {sorted(written)}."
    )


def test_the_receipt_covers_every_field_the_brief_makes_the_trust_surface() -> None:
    """D4: nothing on that list may be missing from the stored shape — and the
    connection and the person are on it, spelled as the server spells them."""
    asked = set(_rendered_receipt_keys())
    for required in (
        "sql",
        "connection_name",
        "role",
        "engine",
        "principal_chain",
        "executed_at",
        "row_count",
        "duration_ms",
        "params",
    ):
        assert required in asked, f"the object page stopped rendering {required}"
