"""The named-block override seam on the main-agent guidance.

A prompt-evolution benchmark swaps one block's text per run without editing
source, so the contract this pins is: names are stable and ordered, an override
replaces exactly its own block, an unknown name is refused loudly, and the
runtime applies whatever it was handed at every root turn's compose. The
no-override composition must stay byte-identical to what shipped before the
seam existed, which is reconstructed here from the exported block constants
rather than a pasted literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.chat.headless import HeadlessError, run_headless
from alkera_cli.harness import HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.system_prompt import (
    ALKERA_GRAPH_BLOCK,
    ANALYST_BLOCK,
    BACKGROUND_BLOCK,
    BLOB_SYSTEM_BLOCK,
    DELIVERABLE_BLOCK,
    EXPLORE_BLOCK,
    KNOWLEDGE_BLOCK,
    NOTEBOOKS_BLOCK,
    PARALLELISM_BLOCK,
    PLUGINS_SYSTEM_BLOCK,
    REPLY_FORMAT_BLOCK,
    REUSABLE_WORK_BLOCK,
    REVIEW_BLOCK,
    TASKS_SYSTEM_BLOCK,
    TOOL_SURFACE_BLOCK,
    WEB_BLOCK,
    compose_main_agent_guidance,
    identity_block,
    main_agent_block_names,
)
from alkera_core.project.directory import ProjectDirectory

#: The block name → block text mapping, written out independently of the module
#: under test: every assertion about ordering, byte-identity, and per-name
#: replacement is anchored here, so a rename or a reorder in the source shows up
#: as a failure rather than as agreement with itself.
BLOCKS: tuple[tuple[str, str], ...] = (
    ("identity", identity_block()),
    ("reply_format", REPLY_FORMAT_BLOCK),
    ("knowledge", KNOWLEDGE_BLOCK),
    ("parallelism", PARALLELISM_BLOCK),
    ("background", BACKGROUND_BLOCK),
    ("tool_surface", TOOL_SURFACE_BLOCK),
    ("explore", EXPLORE_BLOCK),
    ("tasks", TASKS_SYSTEM_BLOCK),
    ("review", REVIEW_BLOCK),
    ("plugins", PLUGINS_SYSTEM_BLOCK),
    ("blob", BLOB_SYSTEM_BLOCK),
    ("alkera_graph", ALKERA_GRAPH_BLOCK),
    ("reusable_work", REUSABLE_WORK_BLOCK),
    ("deliverable", DELIVERABLE_BLOCK),
    ("analyst", ANALYST_BLOCK),
    ("web", WEB_BLOCK),
    ("notebooks", NOTEBOOKS_BLOCK),
)

_WEB = "web"
_ANALYST = "analyst"
_NOTEBOOKS = "notebooks"
#: The blocks that compose only when their selector is on.
_CONDITIONAL = {_WEB, _ANALYST, _NOTEBOOKS}
#: Every block composes under these selectors.
_ALL_ON: dict[str, bool] = {"web_tools": True, "analyst": True, "notebooks": True}
#: The block every single-name case addresses. Any name serves, since the seam is
#: name-agnostic.
_ONE = "blob"
_ONE_BLOCK = BLOB_SYSTEM_BLOCK
_SENTINEL = "SENTINEL-BLOCK-9F3A2C, the substituted block."


def _pre_seam_join(*, web_tools: bool) -> str:
    """The composition as it read before the seam: strip each block, drop the
    empties, join on a blank line, and append web only when the tools are served.
    The analyst and notebooks blocks are off by default, so they never appear here."""
    texts = [
        text
        for name, text in BLOCKS
        if name not in (_ANALYST, _NOTEBOOKS) and (web_tools or name != _WEB)
    ]
    return "\n\n".join(text.strip() for text in texts if text.strip())


# ---------------------------------------------------------------------------
# The addressable name set
# ---------------------------------------------------------------------------


def test_block_names_are_ordered_and_unique() -> None:
    names = main_agent_block_names()
    assert names == tuple(name for name, _ in BLOCKS)


def test_block_names_are_stable_across_calls() -> None:
    assert main_agent_block_names() == main_agent_block_names()


# ---------------------------------------------------------------------------
# No overrides: byte-identity with the pre-seam output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("web_tools", [False, True], ids=["no_web", "web"])
def test_default_composition_is_byte_identical_to_the_pre_seam_join(web_tools: bool) -> None:
    assert compose_main_agent_guidance(web_tools=web_tools) == _pre_seam_join(web_tools=web_tools)


@pytest.mark.parametrize("web_tools", [False, True], ids=["no_web", "web"])
def test_none_and_empty_overrides_are_the_default(web_tools: bool) -> None:
    default = compose_main_agent_guidance(web_tools=web_tools)
    assert compose_main_agent_guidance(web_tools=web_tools, overrides=None) == default
    assert compose_main_agent_guidance(web_tools=web_tools, overrides={}) == default


def test_default_composition_omits_the_web_block() -> None:
    assert WEB_BLOCK.strip() not in compose_main_agent_guidance()
    assert WEB_BLOCK.strip() in compose_main_agent_guidance(web_tools=True)


@pytest.mark.parametrize("name,text", BLOCKS, ids=[name for name, _ in BLOCKS])
def test_each_block_composes_exactly_once(name: str, text: str) -> None:
    assert compose_main_agent_guidance(**_ALL_ON).count(text.strip()) == 1


# ---------------------------------------------------------------------------
# An override replaces exactly its own block
# ---------------------------------------------------------------------------


def _order_of_surviving(
    composed: str, *, skip: set[str]
) -> list[str]:  # same-author-ok: restored verbatim from the pre-rebase commit
    found = [
        (composed.index(text.strip()), name)
        for name, text in BLOCKS
        if name not in skip and text.strip() in composed
    ]
    return [name for _, name in sorted(found)]


def test_one_override_replaces_exactly_its_own_block() -> None:
    default = compose_main_agent_guidance()
    result = compose_main_agent_guidance(overrides={_ONE: _SENTINEL})

    # Byte-exact: the composition differs from the default only where that block sat.
    assert default.replace(_ONE_BLOCK.strip(), _SENTINEL) == result
    assert result.count(_SENTINEL) == 1
    assert _ONE_BLOCK.strip() not in result

    skip = {_ONE, *_CONDITIONAL}
    for name, text in BLOCKS:
        if name in skip:
            continue
        assert text.strip() in result
    assert _order_of_surviving(result, skip=skip) == [n for n, _ in BLOCKS if n not in skip]


@pytest.mark.parametrize("name,text", BLOCKS, ids=[name for name, _ in BLOCKS])
def test_every_name_addresses_its_own_block_and_no_other(name: str, text: str) -> None:
    default = compose_main_agent_guidance(**_ALL_ON)
    result = compose_main_agent_guidance(**_ALL_ON, overrides={name: _SENTINEL})

    assert default.replace(text.strip(), _SENTINEL) == result


def test_overriding_every_block_yields_only_the_overrides_in_order() -> None:
    names = main_agent_block_names()
    overrides = {name: f"BLOCK-{index}" for index, name in enumerate(names)}
    expected = "\n\n".join(f"BLOCK-{index}" for index in range(len(names)))
    assert compose_main_agent_guidance(**_ALL_ON, overrides=overrides) == expected


def test_override_text_is_stripped_like_a_shipped_block() -> None:
    padded = compose_main_agent_guidance(overrides={_ONE: f"\n\n  {_SENTINEL}  \n\n"})
    assert padded == compose_main_agent_guidance(overrides={_ONE: _SENTINEL})


@pytest.mark.parametrize("blank", ["", "   ", "\n\n"], ids=["empty", "spaces", "newlines"])
def test_a_blank_override_drops_the_block_without_leaving_a_gap(blank: str) -> None:
    result = compose_main_agent_guidance(overrides={_ONE: blank})
    default = compose_main_agent_guidance()
    assert result == default.replace(f"{_ONE_BLOCK.strip()}\n\n", "")


# ---------------------------------------------------------------------------
# The `web` asymmetry
# ---------------------------------------------------------------------------


def test_web_override_is_accepted_but_inert_without_web_tools() -> None:
    # `web` is a member of the ordered mapping, so it is a known name even when the
    # session serves no web tools — the override is silently ignored, not refused.
    result = compose_main_agent_guidance(overrides={_WEB: _SENTINEL})
    assert result == compose_main_agent_guidance()


# ---------------------------------------------------------------------------
# Unknown names
# ---------------------------------------------------------------------------


def test_unknown_override_name_raises_and_names_the_known_blocks() -> None:
    # The retired name must be refused rather than silently targeting `knowledge`.
    with pytest.raises(ValueError) as excinfo:
        compose_main_agent_guidance(overrides={"context": _SENTINEL})
    message = str(excinfo.value)
    assert "context" in message
    for name in main_agent_block_names():
        assert name in message


def test_unknown_names_are_all_reported_sorted() -> None:
    with pytest.raises(ValueError) as excinfo:
        compose_main_agent_guidance(overrides={"zeta": "z", "alpha": "a", _ONE: _SENTINEL})
    message = str(excinfo.value)
    assert message.index("alpha") < message.index("zeta")


@pytest.mark.parametrize(
    "name",
    ["Blob", "blob ", "BLOB_SYSTEM_BLOCK", "identity.block", ""],
    ids=["capitalized", "trailing_space", "constant_name", "dotted", "empty"],
)
def test_near_miss_names_are_not_silently_accepted(name: str) -> None:
    with pytest.raises(ValueError):
        compose_main_agent_guidance(overrides={name: _SENTINEL})


# ---------------------------------------------------------------------------
# HarnessRuntime carries the overrides into every root turn's compose
# ---------------------------------------------------------------------------


def _runtime(
    tmp_path: Path, overrides: dict[str, str] | None = None
) -> tuple[HarnessRuntime, FakeAdapterFactory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    # Every turn settles, since a second prompt waits for the first turn to end.
    factory = FakeAdapterFactory(lambda: FakeAdapter(reply_text="ok"))
    runtime = HarnessRuntime(project, adapter_factory=factory, system_block_overrides=overrides)
    return runtime, factory


async def _systems_for(rt: HarnessRuntime, factory: FakeAdapterFactory, turns: int) -> list[str]:
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    try:
        for index in range(turns):
            await session.send_prompt(f"turn {index}")
        return [prompt.system or "" for prompt in factory.adapters[-1].sent_prompts]
    finally:
        await rt.close_chat(sid)


def test_runtime_defaults_to_no_overrides(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    assert rt.system_block_overrides is None


def test_runtime_exposes_what_it_was_given(tmp_path: Path) -> None:
    overrides = {_ONE: _SENTINEL}
    rt, _ = _runtime(tmp_path, overrides)
    assert rt.system_block_overrides == overrides


async def test_default_runtime_sends_the_shipped_block(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    systems = await _systems_for(rt, factory, turns=1)
    assert _ONE_BLOCK.strip() in systems[0]


async def test_runtime_substitutes_the_block_on_every_root_turn(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path, {_ONE: _SENTINEL})
    systems = await _systems_for(rt, factory, turns=2)
    assert len(systems) == 2
    for system in systems:
        assert _SENTINEL in system
        assert _ONE_BLOCK.strip() not in system


async def test_runtime_reads_the_mapping_at_compose_time(tmp_path: Path) -> None:
    # The mapping is held by reference, so a later edit reaches the next turn. A
    # benchmark driver builds a fresh mapping per run and never sees this; it is
    # pinned so a move to copy-on-construction is a deliberate decision.
    overrides = {_ONE: _SENTINEL}
    rt, factory = _runtime(tmp_path, overrides)
    chat = rt._chats_store.create(title="c", harness_type="agent")
    sid = chat.session_id
    chat.close()
    session = await rt.open_chat(sid)
    try:
        await session.send_prompt("first")
        overrides[_ONE] = "SECOND-GENERATION PROMPT"
        await session.send_prompt("second")
        sent = factory.adapters[-1].sent_prompts
        assert "SECOND-GENERATION PROMPT" in (sent[1].system or "")
    finally:
        await rt.close_chat(sid)


async def test_runtime_rejects_an_unknown_block_name_at_the_first_turn(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path, {"context": _SENTINEL})
    with pytest.raises(ValueError, match="context"):
        await _systems_for(rt, factory, turns=1)


# ---------------------------------------------------------------------------
# run_headless threading + the injected-runtime guard
# ---------------------------------------------------------------------------


async def test_headless_refuses_overrides_with_an_injected_runtime(tmp_path: Path) -> None:
    rt, factory = _runtime(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        await run_headless(
            tmp_path,
            ["hi"],
            runtime=rt,
            system_block_overrides={_ONE: _SENTINEL},
        )
    # A caller catching only `HeadlessError` around a scripted run does NOT catch
    # this one, though the neighboring model/effort guard on the same branch is a
    # `HeadlessError`.
    assert not isinstance(excinfo.value, HeadlessError)
    # The guard fires before anything is spawned or a chat is opened.
    assert factory.adapters == []


async def test_headless_refuses_even_an_empty_override_mapping(tmp_path: Path) -> None:
    # The guard is an identity check against None, so an empty mapping is refused
    # too — unlike `compose_main_agent_guidance`, which treats it as no overrides.
    rt, _ = _runtime(tmp_path)
    with pytest.raises(ValueError):
        await run_headless(tmp_path, ["hi"], runtime=rt, system_block_overrides={})


async def test_headless_model_guard_precedes_the_override_guard(tmp_path: Path) -> None:
    rt, _ = _runtime(tmp_path)
    with pytest.raises(HeadlessError):
        await run_headless(
            tmp_path,
            ["hi"],
            runtime=rt,
            model="some-model",
            system_block_overrides={_ONE: _SENTINEL},
        )


class _StopBuildError(Exception):
    """Aborts `run_headless` at the runtime-build seam so no auth, gateway, or
    subprocess is needed to observe what it forwards."""


@pytest.mark.parametrize(
    "overrides",
    [None, {_ONE: _SENTINEL}],
    ids=["default", "with_overrides"],
)
async def test_headless_forwards_overrides_to_the_runtime_it_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str] | None,
) -> None:
    # With no injected runtime, headless builds its own — the overrides must reach
    # it or a benchmark run would silently score the shipped prompt.
    captured: dict[str, object] = {}

    async def _fake_build(project_dir: Path, **kwargs: object) -> tuple[object, dict[str, object]]:
        captured.update(kwargs)
        raise _StopBuildError

    monkeypatch.setattr("alkera_cli.chat.headless._build_production_runtime", _fake_build)
    with pytest.raises(_StopBuildError):
        await run_headless(tmp_path, ["hi"], system_block_overrides=overrides)
    assert captured["system_block_overrides"] == overrides
