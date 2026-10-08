"""The workflow sandbox every Worker runs its workflows in.

The SDK's default restrictions pass ``pydantic`` through (its classes import
lazily), but not ``pydantic_core``, which pydantic imports the first time a
model is built. A workflow that constructs one of the versioned shapes that
cross history (a drain's input, its report) therefore triggers an import
*during* a workflow activation; the sandbox re-imports the module into its
own namespace and warns that a module was imported after the initial workflow
load — and under a warnings-as-errors policy that warning fails the workflow.
Passing ``pydantic_core`` through, like ``pydantic`` already is, makes the
shapes' validation run against the one real module and keeps the activation
free of dynamic imports.
"""

from __future__ import annotations

from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

PASSTHROUGH_MODULES: frozenset[str] = frozenset({"pydantic_core"})
"""Modules passed through on top of the SDK defaults. Data only: a test pins it."""

SANDBOX_RESTRICTIONS: SandboxRestrictions = SandboxRestrictions.default.with_passthrough_modules(
    *sorted(PASSTHROUGH_MODULES)
)


def workflow_runner() -> SandboxedWorkflowRunner:
    """The sandboxed runner with the codebase's restrictions — one per Worker."""
    return SandboxedWorkflowRunner(restrictions=SANDBOX_RESTRICTIONS)
