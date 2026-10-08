"""A box's data plane is a registration: with none, the box serves chats with
no schema or source cards and a connection pass never reports a change."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.box_data import BoxData, box_data
from alkera_cli.host.paths import project_directory
from alkera_core.extensions import ExtensionError, ExtensionPoint


class _Plane:
    def schema_cards(self, **_kwargs: Any) -> Any:
        raise AssertionError("not built here")

    def refresh_sources(self, project: Any) -> Sequence[str]:
        return ("warehouse",)


def _point(*planes: _Plane) -> ExtensionPoint[BoxData]:
    point: ExtensionPoint[BoxData] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    for plane in planes:
        point.register(plane)
    return point


def test_with_no_plane_a_box_knows_no_data(tmp_path: Path) -> None:
    plane = box_data(_point())
    cards = plane.schema_cards(
        api_url="http://api", token="t", runtime=None, chats=list, refresh_interval=1.0
    )
    assert asyncio.run(cards.sync_once()) is False
    asyncio.run(cards.stop())
    assert list(plane.refresh_sources(project_directory(tmp_path))) == []


def test_the_registered_plane_is_the_one_used(tmp_path: Path) -> None:
    plane = _Plane()
    assert box_data(_point(plane)) is plane


def test_two_planes_are_a_composition_error() -> None:
    with pytest.raises(ExtensionError, match="more than one"):
        box_data(_point(_Plane(), _Plane()))
