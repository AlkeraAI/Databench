"""The status a person reads on a pill, decided on the server.

The public contract: :class:`StatusFact` rides every read that feeds a pill,
each subject registers its vocabulary and has one pure builder over a typed
evidence record, and nothing outside this package constructs a fact.
"""

from alkera_core.status.chat import (
    CHAT_STATUS,
    ChatBounds,
    ChatEvidence,
    chat_status,
    placement_status,
    refusal_read,
)
from alkera_core.status.contract import (
    ACTION_KINDS,
    TONES,
    ActionKind,
    Generic,
    ReasonDef,
    StateDef,
    StatusAction,
    StatusFact,
    StatusVocabularyError,
    Tone,
    Vocabulary,
    action,
    register,
    registered,
)
from alkera_core.status.files_live import (
    FILES_LIVE_STATUS,
    FilesLiveEvidence,
    files_live_status,
)
from alkera_core.status.machine import (
    FLEET_STATUS,
    MACHINE_STATUS,
    MachineEvidence,
    fleet_status,
    machine_status,
)
from alkera_core.status.workspace import (
    WORKSPACE_STATUS,
    WorkspaceBounds,
    WorkspaceEvidence,
    workspace_status,
)

__all__ = [
    "ACTION_KINDS",
    "CHAT_STATUS",
    "FILES_LIVE_STATUS",
    "FLEET_STATUS",
    "MACHINE_STATUS",
    "TONES",
    "WORKSPACE_STATUS",
    "ActionKind",
    "ChatBounds",
    "ChatEvidence",
    "FilesLiveEvidence",
    "Generic",
    "MachineEvidence",
    "ReasonDef",
    "StateDef",
    "StatusAction",
    "StatusFact",
    "StatusVocabularyError",
    "Tone",
    "Vocabulary",
    "WorkspaceBounds",
    "WorkspaceEvidence",
    "action",
    "chat_status",
    "files_live_status",
    "fleet_status",
    "machine_status",
    "placement_status",
    "refusal_read",
    "register",
    "registered",
    "workspace_status",
]
