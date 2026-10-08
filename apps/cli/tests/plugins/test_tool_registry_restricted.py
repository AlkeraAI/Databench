"""``ToolRegistry.restricted``: one build of the project's tools, one view per
session with what that session's org withholds taken out.

The view is what lets a runtime that serves chats of several orgs build its
registry once without that build encoding any org's policy. These pin what a
view is: absent from every listing and search, refused by name with the reason,
sharing everything else with the registry it was made from — and what it is
not: a change to the registry underneath.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_cli.plugins.plugin_base.web_tools import (
    WEB_TOOL_NAMES,
    register_web_tools,
    withheld_web_tools,
)
from alkera_core.project.directory import ProjectDirectory

pytestmark = [pytest.mark.spread]


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_meta_tools(registry)
    register_web_tools(registry)
    return registry


def _names(registry: ToolRegistry) -> set[str]:
    return {spec.name for spec in registry.all_specs()}


@pytest.mark.parametrize(
    ("search", "fetch", "withheld"),
    [
        pytest.param(False, False, {"web.search", "web.fetch"}, id="org-off"),
        pytest.param(False, True, {"web.search", "web.fetch"}, id="fetch-cannot-grant-past-search"),
        pytest.param(True, False, {"web.fetch"}, id="deployment-withholds-fetch"),
        pytest.param(True, True, set(), id="both-on"),
    ],
)
def test_the_org_flags_name_exactly_the_tools_a_chat_does_not_get(
    search: bool, fetch: bool, withheld: set[str]
) -> None:
    assert withheld_web_tools(search=search, fetch=fetch) == frozenset(withheld)


def test_withholding_nothing_is_the_registry_itself(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert registry.restricted(frozenset()) is registry
    assert registry.withheld == frozenset()


def test_a_withheld_tool_is_gone_from_every_listing_of_the_view(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    view = registry.restricted(WEB_TOOL_NAMES)
    assert WEB_TOOL_NAMES.isdisjoint(_names(view))
    assert WEB_TOOL_NAMES.isdisjoint({s.name for s in view.hot_prefix()})
    assert view.tool_for("web.search") is None
    assert view.tool_for("web.fetch") is None
    assert view.withheld == WEB_TOOL_NAMES
    # Everything else the registry carries, the view carries.
    assert _names(view) == _names(registry) - WEB_TOOL_NAMES
    assert view.tool_for("search_tools") is registry.tool_for("search_tools")


def test_the_registry_underneath_is_unchanged_by_a_view(tmp_path: Path) -> None:
    """A view is the session's; the build stays whole for the next session,
    which may be an org that gets what this one does not."""
    registry = _registry(tmp_path)
    before = _names(registry)
    registry.restricted(WEB_TOOL_NAMES)
    assert _names(registry) == before
    assert registry.tool_for("web.search") is not None
    assert registry.withheld == frozenset()


async def test_a_search_on_the_view_never_answers_with_a_withheld_tool(tmp_path: Path) -> None:
    """The view ranks over the registry's own index rather than embedding the
    corpus again — and a name it withholds is filtered out of the ranking, so
    ``search_tools`` cannot show a chat a tool it may not call."""
    registry = _registry(tmp_path)
    assert {s.name for s in await registry.search("fetch a web page", k=5)} & WEB_TOOL_NAMES
    view = registry.restricted(frozenset({"web.fetch"}))
    found = {s.name for s in await view.search("fetch a web page", k=5)}
    assert "web.fetch" not in found
    assert "web.search" in found, "what the view keeps is still found"
    # A view of a view still ranks over the one shared index.
    narrower = view.restricted(WEB_TOOL_NAMES)
    assert WEB_TOOL_NAMES.isdisjoint({s.name for s in await narrower.search("web", k=5)})
    assert narrower.withheld == WEB_TOOL_NAMES


async def test_a_dispatch_by_name_is_refused_as_policy_not_as_a_typo(tmp_path: Path) -> None:
    """The model may know the name from another chat or an older turn. The
    answer says the tool is not enabled here, which stops it retrying with
    spelling variants — and differs from the answer for a name that does not
    exist at all."""
    view = _registry(tmp_path).restricted(WEB_TOOL_NAMES)
    refused = await view.dispatch("web.search", {"query": "x"})
    assert refused["error"] == "tool 'web.search' is not enabled for this chat"
    assert refused["tool"] == "web.search"
    unknown = await view.dispatch("web.nothing", {})
    assert unknown["error"] == "unknown tool 'web.nothing'"


async def test_the_registry_underneath_still_dispatches_what_a_view_withholds(
    tmp_path: Path,
) -> None:
    """The refusal is the view's, not the tool's: the same name on the registry
    the view was made from is looked up and run. (It fails on the arguments,
    which is the tool answering, not the catalog refusing.)"""
    registry = _registry(tmp_path)
    registry.restricted(WEB_TOOL_NAMES)
    answered = await registry.dispatch("web.search", {"query": ""})
    assert "not enabled" not in answered.get("error", "")
    assert "unknown tool" not in answered.get("error", "")
