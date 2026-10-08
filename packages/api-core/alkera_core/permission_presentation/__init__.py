"""One surface-neutral description of a pending permission ask.

Every surface that asks a person for permission -- the web chat card and the
Slack thread card today -- renders the :class:`PermissionPresentation` that
:func:`present` builds from the ``permission.request`` event. The words and
registers come from the presenter registry (``registry.py``); a new kind of
ask gains its detail on every surface by one ``register`` call there. The web
reads the registry through its generated TypeScript twin in
``@alkera/chat-model``, held to this package's output by the committed
conformance vectors.
"""

from alkera_core.permission_presentation.model import (
    PRESENTATION_VERSION,
    AskCall,
    PermissionAsk,
    PermissionPresentation,
)
from alkera_core.permission_presentation.modes import (
    APPROVAL_REFUSALS,
    MODE_RANK,
    PERMISSION_MODES,
    WRITING_MODES,
    PermissionModeSpec,
    approval_refusal,
    mode_spec,
    mode_words,
)
from alkera_core.permission_presentation.outcome import (
    ModeChangePresentation,
    mode_change,
    resolution_line,
)
from alkera_core.permission_presentation.present import (
    ask_from_event,
    canonical_tool_name,
    present,
    redact,
)

__all__ = [
    "APPROVAL_REFUSALS",
    "MODE_RANK",
    "PERMISSION_MODES",
    "PRESENTATION_VERSION",
    "WRITING_MODES",
    "AskCall",
    "ModeChangePresentation",
    "PermissionAsk",
    "PermissionModeSpec",
    "PermissionPresentation",
    "approval_refusal",
    "ask_from_event",
    "canonical_tool_name",
    "mode_change",
    "mode_spec",
    "mode_words",
    "present",
    "redact",
    "resolution_line",
]
