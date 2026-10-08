"""The project steps a distribution registers: every one runs, in registration
order, and a failing opened-project step never stops the ones after it."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.extension_points import (
    OpenedProjectStep,
    SharedProjectStep,
    WorkspaceSeeder,
    prepare_opened_project,
    prepare_shared_project,
    workspace_seeder,
)
from alkera_cli.host.paths import project_directory
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.project.directory import ProjectDirectory


def _point(*steps: OpenedProjectStep) -> ExtensionPoint[OpenedProjectStep]:
    point: ExtensionPoint[OpenedProjectStep] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    for step in steps:
        point.register(step)
    return point


def test_every_opened_project_step_runs_even_after_one_fails(tmp_path: Path) -> None:
    ran: list[str] = []

    def first(_project: ProjectDirectory) -> None:
        ran.append("first")
        raise RuntimeError("its store is locked")

    def second(project: ProjectDirectory) -> None:
        ran.append(project.path.name)

    prepare_opened_project(project_directory(tmp_path), _point(first, second))
    assert ran == ["first", ".alkera"]


def test_a_shared_project_step_failure_is_not_swallowed(tmp_path: Path) -> None:
    """Marking a shared store is a tenancy guarantee, so a failure surfaces."""

    def refuse(_project: ProjectDirectory) -> None:
        raise RuntimeError("cannot mark")

    point: ExtensionPoint[SharedProjectStep] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    point.register(refuse)
    with pytest.raises(RuntimeError, match="cannot mark"):
        prepare_shared_project(project_directory(tmp_path), point)


def test_with_nothing_registered_both_do_nothing(tmp_path: Path) -> None:
    project = project_directory(tmp_path)
    prepare_opened_project(project, _point())
    prepare_shared_project(project, ExtensionPoint(f"probe-{uuid.uuid4()}"))


class _Seeder:
    """Stands in for a seeder; the lookup never calls it."""


def _seeders(*registered: Any) -> ExtensionPoint[WorkspaceSeeder]:
    point: ExtensionPoint[WorkspaceSeeder] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    for seeder in registered:
        point.register(seeder)
    return point


def test_a_build_with_no_seeder_has_none() -> None:
    assert workspace_seeder(_seeders()) is None


def test_the_one_registered_seeder_is_the_one_used() -> None:
    seeder = _Seeder()
    assert workspace_seeder(_seeders(seeder)) is seeder


def test_two_seeders_are_a_composition_error() -> None:
    with pytest.raises(ExtensionError, match="more than one extension seeds"):
        workspace_seeder(_seeders(_Seeder(), _Seeder()))
