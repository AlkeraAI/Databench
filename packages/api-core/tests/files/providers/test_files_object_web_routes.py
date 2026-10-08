"""Where an object opens in the web app.

The address is the one a Files row's object facet, a mounted pointer and an
object read all carry, so a kind that falls through to the object page opens a
page that has nothing to show it. The object page renders a promoted result and
explains the retired kinds; every other kind has a page of its own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from alkera_core.files.providers.registry import OBJECT_WEB_ROUTES, object_web_path
from alkera_core.models.workspace_object import OBJECT_TYPES
from alkera_core.schemas.objects.api import WorkspaceObjectRead

OBJECT_ID = "7c0e1d52-3f0a-4b8e-9d55-0b6f1c2a9e11"


@pytest.mark.parametrize(
    ("object_type", "path"),
    [
        pytest.param("chat", f"/chat/{OBJECT_ID}", id="chat"),
        pytest.param("workspace", f"/workspaces/{OBJECT_ID}", id="workspace"),
        pytest.param("chat_template", f"/templates/{OBJECT_ID}", id="template"),
        pytest.param("result", f"/objects/{OBJECT_ID}", id="result"),
        pytest.param("query", f"/objects/{OBJECT_ID}", id="retired-query"),
        pytest.param("report", f"/objects/{OBJECT_ID}", id="retired-report"),
    ],
)
def test_each_kind_opens_on_its_own_page(object_type: str, path: str) -> None:
    assert object_web_path(object_type, OBJECT_ID) == path


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
def test_every_kind_the_model_admits_names_its_page(object_type: str) -> None:
    """A kind added to the model must choose where it opens; the fallback is
    for a node whose type nothing recognises, never for a real kind."""
    assert object_type in OBJECT_WEB_ROUTES


def test_only_the_kinds_the_object_page_answers_for_open_there() -> None:
    """The object page renders a result and says a query or a report was
    retired. A board and an app have no page yet and nothing creates them, so
    they keep the object page until one exists. Anything else sent there lands
    on "this kind of object was retired", which is a lie about a live object."""
    routed_to_object_page = {
        kind for kind, route in OBJECT_WEB_ROUTES.items() if route == "objects"
    }
    assert routed_to_object_page == {"result", "query", "report", "board", "app"}


@pytest.mark.parametrize(
    ("object_type", "path"),
    [
        pytest.param("workspace", f"/workspaces/{OBJECT_ID}", id="workspace"),
        pytest.param("chat", f"/chat/{OBJECT_ID}", id="chat"),
        pytest.param("result", f"/objects/{OBJECT_ID}", id="result"),
    ],
)
def test_an_object_read_carries_the_page_it_opens_on(object_type: str, path: str) -> None:
    """Read off the row the way the routes read it (``from_attributes``): the
    row has no address of its own, so the shape names it."""
    now = datetime(2026, 10, 6, tzinfo=UTC)
    row = SimpleNamespace(
        id=uuid.UUID(OBJECT_ID),
        logical_id="logical",
        namespace="workspace",
        type=object_type,
        title="Weekly numbers",
        version=1,
        status="ready",
        spec={},
        owner_user_id=uuid.uuid4(),
        visibility_scope="private",
        created_at=now,
        updated_at=now,
        content_updated_at=0.0,
    )
    read = WorkspaceObjectRead.model_validate(row)
    assert read.model_dump(mode="json")["web_url"] == path
