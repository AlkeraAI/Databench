"""The shell-effect model: what a shell command reads, writes and reaches.

One reading of a command for every consumer. The permission classifier takes
the invocations and decides their effect; the workspace fence takes the
locations and judges where they land; both take ``opaque`` as the list of what
nobody can vouch for. See :mod:`.model` for the walk and :mod:`.programs` for
what each program does with its arguments.
"""

from __future__ import annotations

from alkera_cli.plugins.plugin_base.permissions.shell.model import (
    MAX_COMMAND_BYTES,
    MAX_WRAP_DEPTH,
    Invocation,
    NetworkReach,
    Redirect,
    Role,
    ShellEffects,
    ShellLocation,
    analyze_shell,
    backslash_escapes_here,
)
from alkera_cli.plugins.plugin_base.permissions.shell.programs import (
    CODE_INTERPRETERS,
    FLAG_WRITERS,
    INPLACE_EDITORS,
    KNOWN,
    NETWORK_PROGRAMS,
    OPAQUE_FLAGS,
    OPAQUE_PROGRAMS,
    OUTPUT_OPTIONS,
    PARTIAL,
    PATHLESS,
    PATTERN_FIRST,
    PROCESS_ENVIRONS,
    PROCESS_OBSERVERS,
    PROCESS_SIGNALERS,
    PROCESS_TABLE,
    PROGRAM_OPTIONS,
    READERS,
    SHELLS,
    TARGET_DIRECTORY_COMMANDS,
    VALUE_FLAGS,
    WRAPPERS,
    WRITERS,
    Opaque,
    OpaqueKind,
)

__all__ = [
    "CODE_INTERPRETERS",
    "FLAG_WRITERS",
    "INPLACE_EDITORS",
    "KNOWN",
    "MAX_COMMAND_BYTES",
    "MAX_WRAP_DEPTH",
    "NETWORK_PROGRAMS",
    "OPAQUE_FLAGS",
    "OPAQUE_PROGRAMS",
    "OUTPUT_OPTIONS",
    "PARTIAL",
    "PATHLESS",
    "PATTERN_FIRST",
    "PROCESS_ENVIRONS",
    "PROCESS_OBSERVERS",
    "PROCESS_SIGNALERS",
    "PROCESS_TABLE",
    "PROGRAM_OPTIONS",
    "READERS",
    "SHELLS",
    "TARGET_DIRECTORY_COMMANDS",
    "VALUE_FLAGS",
    "WRAPPERS",
    "WRITERS",
    "Invocation",
    "NetworkReach",
    "Opaque",
    "OpaqueKind",
    "Redirect",
    "Role",
    "ShellEffects",
    "ShellLocation",
    "analyze_shell",
    "backslash_escapes_here",
]
