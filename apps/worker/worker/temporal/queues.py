"""What each task queue serves — discovered from the ``worker.workflows`` and
``worker.activities`` packages, plus the job modules a private extension
registers, not listed by hand.

Every module in those two packages is imported (sorted, private ``_helpers``
skipped) and every ``@workflow.defn`` class / ``@activity.defn`` function it
holds is registered under the queue its type name maps to in the shared
contract. Adding a job family is therefore file-additive: drop in
``workflows/<family>.py`` + ``activities/<family>.py`` and the worker serves
them. A family that ships only with the product lives outside those packages
and registers its modules into :data:`JOB_MODULES` at composition
(``python -m worker`` installs them); the open worker serves none of it.
Families are registered in family-name order, wherever their modules live.

The registries are built the first time one is read, after composition, and
reading one closes :data:`JOB_MODULES`. A type name the contract does not know
fails then, because a registered job with no queue is a wiring bug a worker
must not boot past.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType, ModuleType
from typing import Any

from alkera_core.extensions import ExtensionPoint
from alkera_core.temporal import COMPANION_ACTIVITIES, QUEUE_FOR, TaskQueue, WorkflowType
from temporalio import activity, workflow

import worker.activities
import worker.workflows

__all__ = [
    "ACTIVITIES_BY_QUEUE",
    "ALL_ACTIVITIES",
    "ALL_WORKFLOWS",
    "JOB_MODULES",
    "QUEUE_FOR",
    "WORKFLOWS_BY_QUEUE",
    "JobModules",
    "TaskQueue",
    "WorkflowType",
    "activity_type_name",
    "queue_for_activity",
    "workflow_type_name",
]


def workflow_type_name(cls: type) -> str:
    """The registered type name of a ``@workflow.defn`` class — the one place
    that reaches into the SDK's definition object."""
    defn = workflow._Definition.from_class(cls)
    if defn is None:
        raise TypeError(f"{cls.__qualname__} is not a @workflow.defn class")
    return defn.name or cls.__name__


def activity_type_name(fn: Callable[..., Any]) -> str:
    """The registered type name of an ``@activity.defn`` function."""
    defn = activity._Definition.from_callable(fn)
    if defn is None:
        raise TypeError(f"{getattr(fn, '__qualname__', fn)!r} is not an @activity.defn function")
    return defn.name or fn.__name__


def queue_for_activity(activity_type: str) -> TaskQueue:
    """The queue that serves an activity type: every activity shares the queue
    of the workflow it is named after (the email dispatch included — its
    workflow lives on the email queue), or, for the few that are a second step
    rather than a namesake, of the workflow ``COMPANION_ACTIVITIES`` names."""
    companion = COMPANION_ACTIVITIES.get(activity_type)
    if companion is not None:
        return QUEUE_FOR[companion]
    try:
        return QUEUE_FOR[activity_type]
    except KeyError:
        raise LookupError(
            f"activity {activity_type!r} is not a known workflow type and has no queue; "
            "add it to alkera_core.temporal.contract, or register its family's queues "
            "in JOB_QUEUES, before registering it"
        ) from None


@dataclass(frozen=True, slots=True)
class JobModules:
    """One job family a private extension adds: the module holding its
    ``@workflow.defn`` classes and the one holding its ``@activity.defn``
    functions, registered under ``family`` (the name its open modules would
    have had, which places it in registration order)."""

    family: str
    workflows: ModuleType
    activities: ModuleType


JOB_MODULES: ExtensionPoint[JobModules] = ExtensionPoint("worker_job_modules")
"""The job families the product adds to the open worker."""


def _public_modules(package: ModuleType) -> list[tuple[str, ModuleType]]:
    modules: list[tuple[str, ModuleType]] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        modules.append((info.name, importlib.import_module(f"{package.__name__}.{info.name}")))
    return modules


def _job_modules(package: ModuleType, pick: Callable[[JobModules], ModuleType]) -> list[ModuleType]:
    """The open package's modules and the registered ones, in family order."""
    found = _public_modules(package)
    found.extend((jobs.family, pick(jobs)) for jobs in JOB_MODULES.items())
    return [module for _, module in sorted(found, key=lambda pair: pair[0])]


def _discover_workflows() -> tuple[type, ...]:
    found: list[type] = []
    for module in _job_modules(worker.workflows, lambda jobs: jobs.workflows):
        for name in sorted(vars(module)):
            obj = vars(module)[name]
            if isinstance(obj, type) and workflow._Definition.from_class(obj) is not None:
                if obj.__module__ == module.__name__ and obj not in found:
                    found.append(obj)
    return tuple(found)


def _discover_activities() -> tuple[Callable[..., Any], ...]:
    found: list[Callable[..., Any]] = []
    for module in _job_modules(worker.activities, lambda jobs: jobs.activities):
        for name in sorted(vars(module)):
            obj = vars(module)[name]
            if not callable(obj) or activity._Definition.from_callable(obj) is None:
                continue
            if getattr(obj, "__module__", None) == module.__name__ and obj not in found:
                found.append(obj)
    return tuple(found)


def _by_queue_workflows(all_workflows: tuple[type, ...]) -> Mapping[TaskQueue, tuple[type, ...]]:
    buckets: dict[TaskQueue, list[type]] = {q: [] for q in TaskQueue}
    for cls in all_workflows:
        name = workflow_type_name(cls)
        try:
            queue = QUEUE_FOR[name]
        except KeyError:
            raise LookupError(
                f"workflow {name!r} ({cls.__qualname__}) is not a known workflow type; "
                "add it to alkera_core.temporal.contract, or register its family's queues "
                "in JOB_QUEUES, before registering it"
            ) from None
        buckets[queue].append(cls)
    return MappingProxyType({q: tuple(v) for q, v in buckets.items()})


def _by_queue_activities(
    all_activities: tuple[Callable[..., Any], ...],
) -> Mapping[TaskQueue, tuple[Callable[..., Any], ...]]:
    buckets: dict[TaskQueue, list[Callable[..., Any]]] = {q: [] for q in TaskQueue}
    for fn in all_activities:
        buckets[queue_for_activity(activity_type_name(fn))].append(fn)
    return MappingProxyType({q: tuple(v) for q, v in buckets.items()})


@dataclass(frozen=True, slots=True)
class _Registry:
    workflows: tuple[type, ...]
    activities: tuple[Callable[..., Any], ...]
    workflows_by_queue: Mapping[TaskQueue, tuple[type, ...]]
    activities_by_queue: Mapping[TaskQueue, tuple[Callable[..., Any], ...]]


@cache
def _registry() -> _Registry:
    workflows = _discover_workflows()
    activities = _discover_activities()
    return _Registry(
        workflows=workflows,
        activities=activities,
        workflows_by_queue=_by_queue_workflows(workflows),
        activities_by_queue=_by_queue_activities(activities),
    )


# Declared, not assigned: the module ``__getattr__`` below builds them on first
# read, so a composition root can register job modules before discovery runs.
ALL_WORKFLOWS: tuple[type, ...]
"""Every ``@workflow.defn`` class the worker serves."""

ALL_ACTIVITIES: tuple[Callable[..., Any], ...]
"""Every ``@activity.defn`` function the worker serves."""

WORKFLOWS_BY_QUEUE: Mapping[TaskQueue, tuple[type, ...]]
ACTIVITIES_BY_QUEUE: Mapping[TaskQueue, tuple[Callable[..., Any], ...]]

_LAZY: Mapping[str, Callable[[_Registry], object]] = {
    "ALL_WORKFLOWS": lambda r: r.workflows,
    "ALL_ACTIVITIES": lambda r: r.activities,
    "WORKFLOWS_BY_QUEUE": lambda r: r.workflows_by_queue,
    "ACTIVITIES_BY_QUEUE": lambda r: r.activities_by_queue,
}


def __getattr__(name: str) -> object:
    build = _LAZY.get(name)
    if build is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return build(_registry())


def served_workflow_types() -> frozenset[str]:
    """The workflow type names some worker in this codebase can serve today."""
    return frozenset(workflow_type_name(cls) for cls in _registry().workflows)
