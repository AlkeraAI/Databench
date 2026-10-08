"""The Loro CRDT lane."""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.crdt.announce import CRDT_UPDATE_EVENT_TYPE as CRDT_UPDATE_EVENT_TYPE
    from backend.services.crdt.docs import CrdtDocs as CrdtDocs
    from backend.services.crdt.errors import CrdtError as CrdtError
    from backend.services.crdt.file_kinds import file_name as file_name
    from backend.services.crdt.file_kinds import live_type_of as live_type_of
    from backend.services.crdt.file_kinds import name_text as name_text
    from backend.services.crdt.gateway import relay_frames as relay_frames
    from backend.services.crdt.notebook_peers import NotebookOpError as NotebookOpError
    from backend.services.crdt.notebook_type import author_display as author_display
    from backend.services.crdt.registry import Access as Access
    from backend.services.crdt.registry import DocRef as DocRef
    from backend.services.crdt.switch import LIVE_EDITING_OFF as LIVE_EDITING_OFF
    from backend.services.crdt.switch import LiveSwitch as LiveSwitch
    from backend.services.crdt.switch import OrgWriteBack as OrgWriteBack
    from backend.services.crdt.switch import live_editing_override as live_editing_override
    from backend.services.crdt.switch import resolve_live_editing as resolve_live_editing
    from backend.services.crdt.switch import write_back_org as write_back_org
    from backend.services.crdt.text_peers import PeerText as PeerText

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "CRDT_UPDATE_EVENT_TYPE": "backend.services.crdt.announce",
    "Access": "backend.services.crdt.registry",
    "CrdtDocs": "backend.services.crdt.docs",
    "CrdtError": "backend.services.crdt.errors",
    "DocRef": "backend.services.crdt.registry",
    "NotebookOpError": "backend.services.crdt.notebook_peers",
    "PeerText": "backend.services.crdt.text_peers",
    "author_display": "backend.services.crdt.notebook_type",
    "file_name": "backend.services.crdt.file_kinds",
    "live_type_of": "backend.services.crdt.file_kinds",
    "name_text": "backend.services.crdt.file_kinds",
    "relay_frames": "backend.services.crdt.gateway",
    "LIVE_EDITING_OFF": "backend.services.crdt.switch",
    "LiveSwitch": "backend.services.crdt.switch",
    "OrgWriteBack": "backend.services.crdt.switch",
    "live_editing_override": "backend.services.crdt.switch",
    "resolve_live_editing": "backend.services.crdt.switch",
    "write_back_org": "backend.services.crdt.switch",
}

__all__ = [
    "CRDT_UPDATE_EVENT_TYPE",
    "LIVE_EDITING_OFF",
    "Access",
    "CrdtDocs",
    "CrdtError",
    "DocRef",
    "LiveSwitch",
    "NotebookOpError",
    "OrgWriteBack",
    "PeerText",
    "author_display",
    "file_name",
    "live_editing_override",
    "live_type_of",
    "name_text",
    "relay_frames",
    "resolve_live_editing",
    "write_back_org",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.crdt.announce":
        from backend.services.crdt import announce

        return announce
    if owner == "backend.services.crdt.docs":
        from backend.services.crdt import docs

        return docs
    if owner == "backend.services.crdt.errors":
        from backend.services.crdt import errors

        return errors
    if owner == "backend.services.crdt.file_kinds":
        from backend.services.crdt import file_kinds

        return file_kinds
    if owner == "backend.services.crdt.gateway":
        from backend.services.crdt import gateway

        return gateway
    if owner == "backend.services.crdt.notebook_peers":
        from backend.services.crdt import notebook_peers

        return notebook_peers
    if owner == "backend.services.crdt.notebook_type":
        from backend.services.crdt import notebook_type

        return notebook_type
    if owner == "backend.services.crdt.registry":
        from backend.services.crdt import registry

        return registry
    if owner == "backend.services.crdt.text_peers":
        from backend.services.crdt import text_peers

        return text_peers
    if owner == "backend.services.crdt.switch":
        from backend.services.crdt import switch

        return switch
    raise AssertionError(f"no import for {owner}")
