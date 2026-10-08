"""Environment cloning: capture a workspace's Python (and conda) environment
into a portable spec, recreate it on another machine, and tell the next agent
about it.

Library-first: the ``alkera env`` commands, the daemon's ``environment.*``
methods and the agent's ``environment.capture`` / ``environment.recreate``
tools are thin callers of what this package exports. Every process it starts
goes through a :class:`CommandRunner`, which a cloud chat binds to its sandbox.
"""

from alkera_cli.environment.capture import build_spec, capture
from alkera_cli.environment.files import (
    InvalidSpecError,
    load_spec,
    parse_spec,
    read_spec,
    read_workspace_text,
    render_spec,
    write_spec,
)
from alkera_cli.environment.instructions import (
    CAPTURE_TOOL_NAME,
    RECREATE_TOOL_NAME,
    environment_blocks,
    render_environment_instructions,
    summarize_spec,
)
from alkera_cli.environment.probe import ProbeError, ProbeResult, probe_command, run_probe
from alkera_cli.environment.recreate import (
    LOCAL_ENV_DIR,
    Gap,
    PreparedRecreate,
    RecreateReport,
    StepReport,
    env_python,
    execute_recreate,
    host_python,
    local_python,
    plan_recreate,
    prepare_recreate,
    recreate,
    target_state,
)
from alkera_cli.environment.redact import SpecSecretError, find_secrets, redact_url
from alkera_cli.environment.runner import (
    PLACEHOLDER_RE,
    Command,
    CommandResult,
    CommandRunner,
    LocalRunner,
)
from alkera_cli.environment.service import ChatLaunch, ChatRunnerFactory, EnvironmentService
from alkera_cli.environment.spec import (
    INSTRUCTIONS_FILENAME,
    SPEC_FILENAME,
    EnvironmentSpec,
    NotPortable,
    PackageSpec,
)
from alkera_cli.environment.wake import EnvironmentWakes

__all__ = [
    "CAPTURE_TOOL_NAME",
    "INSTRUCTIONS_FILENAME",
    "LOCAL_ENV_DIR",
    "PLACEHOLDER_RE",
    "RECREATE_TOOL_NAME",
    "SPEC_FILENAME",
    "ChatLaunch",
    "ChatRunnerFactory",
    "Command",
    "CommandResult",
    "CommandRunner",
    "EnvironmentService",
    "EnvironmentSpec",
    "EnvironmentWakes",
    "Gap",
    "InvalidSpecError",
    "LocalRunner",
    "NotPortable",
    "PackageSpec",
    "PreparedRecreate",
    "ProbeError",
    "ProbeResult",
    "RecreateReport",
    "SpecSecretError",
    "StepReport",
    "build_spec",
    "capture",
    "env_python",
    "environment_blocks",
    "execute_recreate",
    "find_secrets",
    "host_python",
    "load_spec",
    "local_python",
    "parse_spec",
    "plan_recreate",
    "prepare_recreate",
    "probe_command",
    "read_spec",
    "read_workspace_text",
    "recreate",
    "redact_url",
    "render_environment_instructions",
    "render_spec",
    "run_probe",
    "summarize_spec",
    "target_state",
    "write_spec",
]
