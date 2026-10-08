"""The ``?org=`` hint on links into the web app.

One rule for every link: the hint names the org only with multi-org on. With it
off a person holds one org, and a link keeps the shape it had before multi-org
existed. The billing links built on it are pinned in ``test_email_render.py``.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.org_links import org_link_query

ORG = UUID("6f1c0d3e-0000-4000-8000-000000000001")


@pytest.fixture
def multi_org_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "multi_org_enabled", True)


@pytest.fixture
def multi_org_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "multi_org_enabled", False)


@pytest.mark.usefixtures("multi_org_on")
def test_with_multi_org_on_the_hint_names_the_org() -> None:
    assert org_link_query(ORG) == f"?org={ORG}"


@pytest.mark.usefixtures("multi_org_off")
def test_with_multi_org_off_there_is_no_hint() -> None:
    assert org_link_query(ORG) == ""


@pytest.mark.parametrize(
    "fixture", [pytest.param("multi_org_on", id="on"), pytest.param("multi_org_off", id="off")]
)
def test_no_org_means_no_hint(fixture: str, request: pytest.FixtureRequest) -> None:
    request.getfixturevalue(fixture)
    assert org_link_query(None) == ""
