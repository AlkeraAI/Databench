"""Notebooks on the backend: the document peer route, runs and kernels, the
box transport and the realtime notebook channel.

The live document is a Loro ``notebook`` document on the CRDT lane, reached
through ``CrdtDocs.notebooks``; the engine and its kernel run on the box that
holds the notebook's folder. This package decides who may do what, records
runs, kernels and edits, carries requests to the box and fans the kernel's
events out to the people watching.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.notebooks.app import STATE_ATTR as STATE_ATTR
    from backend.services.notebooks.app import build_service as build_service
    from backend.services.notebooks.app import close_service as close_service
    from backend.services.notebooks.app import service_for as service_for
    from backend.services.notebooks.callers import Caller as Caller
    from backend.services.notebooks.callers import Target as Target
    from backend.services.notebooks.callers import agent_of_holder as agent_of_holder
    from backend.services.notebooks.callers import box_follows as box_follows
    from backend.services.notebooks.callers import is_notebook as is_notebook
    from backend.services.notebooks.callers import lease_admits_writes as lease_admits_writes
    from backend.services.notebooks.callers import runner_of as runner_of
    from backend.services.notebooks.connections import CATALOG_FACTS as CATALOG_FACTS
    from backend.services.notebooks.connections import (
        connection_choices as connection_choices,
    )
    from backend.services.notebooks.connections import (
        holder_on as holder_on,
    )
    from backend.services.notebooks.connections import (
        workspace_and_owner as workspace_and_owner,
    )
    from backend.services.notebooks.errors import AgentChatRefusedError as AgentChatRefusedError
    from backend.services.notebooks.errors import KernelAnswerError as KernelAnswerError
    from backend.services.notebooks.errors import KernelSilentError as KernelSilentError
    from backend.services.notebooks.errors import NoMachineError as NoMachineError
    from backend.services.notebooks.errors import (
        NotebookUnavailableError as NotebookUnavailableError,
    )
    from backend.services.notebooks.errors import OpRefusedError as OpRefusedError
    from backend.services.notebooks.feed import MAX_BATCH_EVENTS as MAX_BATCH_EVENTS
    from backend.services.notebooks.feed import MAX_EVENT_SEQ as MAX_EVENT_SEQ
    from backend.services.notebooks.feed import KernelRefusedError as KernelRefusedError
    from backend.services.notebooks.feed import accept_events as accept_events
    from backend.services.notebooks.feed import seq_out_of_range as seq_out_of_range
    from backend.services.notebooks.names import system_name as system_name
    from backend.services.notebooks.refs import NOTEBOOK_DOC_TYPE as NOTEBOOK_DOC_TYPE
    from backend.services.notebooks.runs import follow_runs as follow_runs
    from backend.services.notebooks.service import NotebookService as NotebookService
    from backend.services.notebooks.service import RecordedDecider as RecordedDecider
    from backend.services.notebooks.socket import NotebookSocket as NotebookSocket
    from backend.services.notebooks.socket import nb_close as nb_close
    from backend.services.notebooks.socket import nb_holds as nb_holds
    from backend.services.notebooks.socket import nb_inbound as nb_inbound
    from backend.services.notebooks.socket import nb_outbound as nb_outbound
    from backend.services.notebooks.socket import nb_reconsider as nb_reconsider
    from backend.services.notebooks.socket import nb_subscribe as nb_subscribe
    from backend.services.notebooks.socket import nb_unsubscribe as nb_unsubscribe
    from backend.services.notebooks.socket import notebook_socket_for as notebook_socket_for
    from backend.services.notebooks.stored import blob_node as blob_node
    from backend.services.notebooks.stored import read_stored as read_stored
    from backend.services.notebooks.stored import snapshot_node as snapshot_node
    from backend.services.notebooks.waking import is_waking as is_waking
    from backend.services.notebooks.waking import notebook_chats as notebook_chats
    from backend.services.notebooks.waking import waits_on_wake as waits_on_wake

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing the package (as the realtime runtime and the routes do)
#: never imports its submodules, which import the realtime runtime back.
_OWNERS: dict[str, str] = {
    "STATE_ATTR": "backend.services.notebooks.app",
    "build_service": "backend.services.notebooks.app",
    "close_service": "backend.services.notebooks.app",
    "service_for": "backend.services.notebooks.app",
    "MAX_BATCH_EVENTS": "backend.services.notebooks.feed",
    "MAX_EVENT_SEQ": "backend.services.notebooks.feed",
    "KernelRefusedError": "backend.services.notebooks.feed",
    "accept_events": "backend.services.notebooks.feed",
    "seq_out_of_range": "backend.services.notebooks.feed",
    "system_name": "backend.services.notebooks.names",
    "NOTEBOOK_DOC_TYPE": "backend.services.notebooks.refs",
    "follow_runs": "backend.services.notebooks.runs",
    "AgentChatRefusedError": "backend.services.notebooks.errors",
    "Caller": "backend.services.notebooks.callers",
    "CATALOG_FACTS": "backend.services.notebooks.connections",
    "connection_choices": "backend.services.notebooks.connections",
    "holder_on": "backend.services.notebooks.connections",
    "workspace_and_owner": "backend.services.notebooks.connections",
    "KernelAnswerError": "backend.services.notebooks.errors",
    "KernelSilentError": "backend.services.notebooks.errors",
    "NoMachineError": "backend.services.notebooks.errors",
    "NotebookService": "backend.services.notebooks.service",
    "RecordedDecider": "backend.services.notebooks.service",
    "NotebookUnavailableError": "backend.services.notebooks.errors",
    "OpRefusedError": "backend.services.notebooks.errors",
    "Target": "backend.services.notebooks.callers",
    "agent_of_holder": "backend.services.notebooks.callers",
    "box_follows": "backend.services.notebooks.callers",
    "is_notebook": "backend.services.notebooks.callers",
    "lease_admits_writes": "backend.services.notebooks.callers",
    "runner_of": "backend.services.notebooks.callers",
    "NotebookSocket": "backend.services.notebooks.socket",
    "nb_close": "backend.services.notebooks.socket",
    "nb_holds": "backend.services.notebooks.socket",
    "nb_inbound": "backend.services.notebooks.socket",
    "nb_outbound": "backend.services.notebooks.socket",
    "nb_reconsider": "backend.services.notebooks.socket",
    "nb_subscribe": "backend.services.notebooks.socket",
    "nb_unsubscribe": "backend.services.notebooks.socket",
    "notebook_socket_for": "backend.services.notebooks.socket",
    "blob_node": "backend.services.notebooks.stored",
    "read_stored": "backend.services.notebooks.stored",
    "saved_image": "backend.services.notebooks.stored",
    "snapshot_node": "backend.services.notebooks.stored",
    "is_waking": "backend.services.notebooks.waking",
    "notebook_chats": "backend.services.notebooks.waking",
    "waits_on_wake": "backend.services.notebooks.waking",
}

__all__ = [
    "CATALOG_FACTS",
    "MAX_BATCH_EVENTS",
    "MAX_EVENT_SEQ",
    "NOTEBOOK_DOC_TYPE",
    "STATE_ATTR",
    "AgentChatRefusedError",
    "Caller",
    "KernelAnswerError",
    "KernelRefusedError",
    "KernelSilentError",
    "NoMachineError",
    "NotebookService",
    "NotebookSocket",
    "NotebookUnavailableError",
    "OpRefusedError",
    "RecordedDecider",
    "Target",
    "accept_events",
    "agent_of_holder",
    "blob_node",
    "box_follows",
    "build_service",
    "close_service",
    "connection_choices",
    "follow_runs",
    "holder_on",
    "is_notebook",
    "is_waking",
    "lease_admits_writes",
    "nb_close",
    "nb_holds",
    "nb_inbound",
    "nb_outbound",
    "nb_reconsider",
    "nb_subscribe",
    "nb_unsubscribe",
    "notebook_chats",
    "notebook_socket_for",
    "read_stored",
    "runner_of",
    "saved_image",
    "seq_out_of_range",
    "service_for",
    "snapshot_node",
    "system_name",
    "waits_on_wake",
    "workspace_and_owner",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.notebooks.app":
        from backend.services.notebooks import app

        return app
    if owner == "backend.services.notebooks.callers":
        from backend.services.notebooks import callers

        return callers
    if owner == "backend.services.notebooks.connections":
        from backend.services.notebooks import connections

        return connections
    if owner == "backend.services.notebooks.errors":
        from backend.services.notebooks import errors

        return errors
    if owner == "backend.services.notebooks.feed":
        from backend.services.notebooks import feed

        return feed
    if owner == "backend.services.notebooks.names":
        from backend.services.notebooks import names

        return names
    if owner == "backend.services.notebooks.refs":
        from backend.services.notebooks import refs

        return refs
    if owner == "backend.services.notebooks.runs":
        from backend.services.notebooks import runs

        return runs
    if owner == "backend.services.notebooks.service":
        from backend.services.notebooks import service

        return service
    if owner == "backend.services.notebooks.socket":
        from backend.services.notebooks import socket

        return socket
    if owner == "backend.services.notebooks.stored":
        from backend.services.notebooks import stored

        return stored
    if owner == "backend.services.notebooks.waking":
        from backend.services.notebooks import waking

        return waking
    raise AssertionError(f"no import for {owner}")
