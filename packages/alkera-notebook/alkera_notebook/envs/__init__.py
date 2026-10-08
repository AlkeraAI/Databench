"""Notebook environments: detection, materialization and installs."""

from alkera_notebook.envs.detect import (
    Detector,
    EnvNotFoundError,
    detect_envs,
    env_fingerprint,
    interpreter_in,
)
from alkera_notebook.envs.models import (
    CommandResult,
    CommandRunner,
    EnvDescriptor,
    EnvRegistry,
)
from alkera_notebook.envs.registry import (
    MODULE_DISTRIBUTIONS,
    DistributionRequiredError,
    EnvBuildCancelledError,
    EnvBuildError,
    EnvError,
    EnvNotMaterializableError,
    InvalidPythonError,
    InvalidRequirementError,
    LocalEnvRegistry,
    ScriptInstallViaDocumentError,
    distribution_for_module,
    validate_requirement,
)
from alkera_notebook.envs.runner import LocalCommandRunner
from alkera_notebook.envs.script import (
    add_script_dependencies,
    parse_script_block,
    remove_script_dependencies,
    script_dependencies,
)
from alkera_notebook.tree_io import TreeModes

__all__ = [
    "MODULE_DISTRIBUTIONS",
    "CommandResult",
    "CommandRunner",
    "Detector",
    "DistributionRequiredError",
    "EnvBuildCancelledError",
    "EnvBuildError",
    "EnvDescriptor",
    "EnvError",
    "EnvNotFoundError",
    "EnvNotMaterializableError",
    "EnvRegistry",
    "InvalidPythonError",
    "InvalidRequirementError",
    "LocalCommandRunner",
    "LocalEnvRegistry",
    "ScriptInstallViaDocumentError",
    "TreeModes",
    "add_script_dependencies",
    "detect_envs",
    "distribution_for_module",
    "env_fingerprint",
    "interpreter_in",
    "parse_script_block",
    "remove_script_dependencies",
    "script_dependencies",
    "validate_requirement",
]
