"""Which earlier reasoning a model is told it may replay as written.

After a switch the server allowed (the new model reads the reasoning already in
the chat), opencode must replay that reasoning unchanged. It learns which
reasoning a model reads only from the config the harness writes:
``options.alkera.readsReasoningFormats`` (what the model reads) and
``options.alkera.reasoningFormatsByModel`` (which format each other model it
could be switched from writes). A box spawns the agent on the chat's pin alone,
so the formats of the models a chat moved off travel with the pin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from _adapter_factory import FakeAdapterFactory
from alkera_cli.cloud import CloudRestClient, CloudSocket
from alkera_cli.cloud.mirror import pinned_model
from alkera_cli.cloud.service import CloudMirrorService, MirrorSettings
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness import HarnessRuntime, gateway_session
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapters.opencode_alkera import (
    build_alkera_opencode_config,
    build_manifest_model,
)
from alkera_cli.harness.gateway_session import default_gateway_config_builder
from alkera_cli.harness.turn_model import (
    REASONING_HISTORY_KEY,
    reasoning_history,
    with_reasoning_history,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import SessionUpdated

pytestmark = pytest.mark.asyncio

CHAT_ID = "chat-moved-to-a-reader"
OWNER = "00000000-0000-4000-8000-000000000003"

OPUS = GatewayModel(
    id="claude-opus-5",
    display_name="Opus 5",
    wire="anthropic",
    efforts=("low", "high"),
    reasoning_format="anthropic:claude-opus-5",
)
FABLE = GatewayModel(
    id="claude-fable-5",
    display_name="Fable 5",
    wire="anthropic",
    efforts=("low", "high"),
    reasoning_format="anthropic:claude-fable-5",
    reads_reasoning_formats=("anthropic:claude-opus-5", "anthropic:claude-sonnet-5"),
)
GPT = GatewayModel(
    id="gpt-5.5",
    display_name="GPT-5.5",
    wire="openai",
    efforts=("low",),
    reasoning_format="openai:gpt-5.5",
)


def _entries(config: dict[str, Any], provider: str) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = config["provider"][provider]["models"]
    return entries


# --- the config ----------------------------------------------------------------


def test_a_model_that_reads_others_is_told_what_it_reads_on_every_entry() -> None:
    config = build_alkera_opencode_config(
        gateway_url="http://gw", token="t", models=[OPUS, FABLE, GPT]
    )
    expected = {
        "alkera": {
            "readsReasoningFormats": ["anthropic:claude-opus-5", "anthropic:claude-sonnet-5"],
            # Only models whose format Fable reads; GPT's format is not one.
            "reasoningFormatsByModel": {"claude-opus-5": "anthropic:claude-opus-5"},
        }
    }
    fable = {k: v for k, v in _entries(config, "alkera-anthropic").items() if "fable" in k}
    assert set(fable) == {"claude-fable-5", "claude-fable-5::low", "claude-fable-5::high"}
    assert all(entry["options"] == expected for entry in fable.values())


@pytest.mark.parametrize("model_id", ["claude-opus-5", "claude-opus-5::high", "gpt-5.5::low"])
def test_a_model_that_reads_only_itself_gets_no_note(model_id: str) -> None:
    config = build_alkera_opencode_config(
        gateway_url="http://gw", token="t", models=[OPUS, FABLE, GPT]
    )
    entries = {**_entries(config, "alkera-anthropic"), **_entries(config, "alkera-openai")}
    assert "options" not in entries[model_id]


def test_formats_of_models_outside_the_catalog_come_from_the_history() -> None:
    """A box spawns on the chat's model alone; the model it moved off is named
    only by the history the pin carries. A format the model does not read is
    left out even when the history names it."""
    config = build_alkera_opencode_config(
        gateway_url="http://gw",
        token="t",
        models=[FABLE],
        reasoning_formats_by_model={
            "claude-sonnet-5": "anthropic:claude-sonnet-5",
            "gpt-5.5": "openai:gpt-5.5",
        },
    )
    note = _entries(config, "alkera-anthropic")["claude-fable-5"]["options"]["alkera"]
    assert note["reasoningFormatsByModel"] == {"claude-sonnet-5": "anthropic:claude-sonnet-5"}


def test_the_catalog_outranks_a_stale_history() -> None:
    config = build_alkera_opencode_config(
        gateway_url="http://gw",
        token="t",
        models=[OPUS, FABLE],
        reasoning_formats_by_model={"claude-opus-5": "anthropic:something-else"},
    )
    note = _entries(config, "alkera-anthropic")["claude-fable-5"]["options"]["alkera"]
    assert note["reasoningFormatsByModel"] == {"claude-opus-5": "anthropic:claude-opus-5"}


# --- the pin -------------------------------------------------------------------


def test_the_pin_carries_what_the_model_writes_and_reads() -> None:
    pinned = build_manifest_model(FABLE, "low")
    assert pinned["reasoning_format"] == "anthropic:claude-fable-5"
    assert pinned["reads_reasoning_formats"] == [
        "anthropic:claude-opus-5",
        "anthropic:claude-sonnet-5",
    ]


def test_a_model_with_no_reasoning_adds_no_keys_to_the_pin() -> None:
    plain = GatewayModel(id="m", display_name="M", wire="openai")
    assert "reasoning_format" not in build_manifest_model(plain, None)
    assert "reads_reasoning_formats" not in build_manifest_model(plain, None)


def test_a_server_pin_reaches_the_manifest_with_its_formats() -> None:
    pinned = pinned_model(
        {
            "id": "claude-fable-5",
            "wire": "anthropic",
            "efforts": ["low"],
            "effort": "low",
            "reasoning_format": "anthropic:claude-fable-5",
            "reads_reasoning_formats": ["anthropic:claude-opus-5"],
        }
    )
    assert pinned is not None
    assert pinned["reasoning_format"] == "anthropic:claude-fable-5"
    assert pinned["reads_reasoning_formats"] == ["anthropic:claude-opus-5"]


def test_an_older_server_pin_still_pins() -> None:
    pinned = pinned_model({"id": "claude-fable-5", "wire": "anthropic"})
    assert pinned is not None
    assert "reasoning_format" not in pinned


# --- the history ---------------------------------------------------------------

OPUS_PIN = build_manifest_model(OPUS, "high")
FABLE_PIN = build_manifest_model(FABLE, "low")


@pytest.mark.parametrize(
    ("current", "selection", "history"),
    [
        pytest.param(
            OPUS_PIN, FABLE_PIN, {"claude-opus-5": "anthropic:claude-opus-5"}, id="switch"
        ),
        pytest.param(
            {**OPUS_PIN, REASONING_HISTORY_KEY: {"claude-sonnet-5": "anthropic:claude-sonnet-5"}},
            FABLE_PIN,
            {
                "claude-sonnet-5": "anthropic:claude-sonnet-5",
                "claude-opus-5": "anthropic:claude-opus-5",
            },
            id="second-switch-keeps-the-first",
        ),
        pytest.param(
            {**FABLE_PIN, REASONING_HISTORY_KEY: {"claude-opus-5": "anthropic:claude-opus-5"}},
            {**FABLE_PIN, "effort": "high"},
            {"claude-opus-5": "anthropic:claude-opus-5"},
            id="effort-change-keeps-it",
        ),
        pytest.param(
            {**FABLE_PIN, REASONING_HISTORY_KEY: {"claude-opus-5": "anthropic:claude-opus-5"}},
            OPUS_PIN,
            {"claude-fable-5": "anthropic:claude-fable-5"},
            id="back-to-a-model-drops-its-own-entry",
        ),
        pytest.param(None, FABLE_PIN, {}, id="first-pin"),
        pytest.param(
            {"provider_id": "alkera-anthropic", "model_id": "old"},
            FABLE_PIN,
            {},
            id="model-with-no-format-adds-nothing",
        ),
    ],
)
def test_the_pin_keeps_the_formats_of_the_models_moved_off(
    current: dict[str, Any] | None, selection: dict[str, Any], history: dict[str, str]
) -> None:
    pinned = with_reasoning_history(current, selection)
    assert reasoning_history(pinned) == history
    assert (REASONING_HISTORY_KEY in pinned) is bool(history)
    assert {k: v for k, v in pinned.items() if k != REASONING_HISTORY_KEY} == {
        k: v for k, v in selection.items() if k != REASONING_HISTORY_KEY
    }


@pytest.mark.parametrize(
    "held",
    [None, "nope", ["claude-opus-5"], {"claude-opus-5": 3}, {"claude-opus-5": ""}],
)
def test_a_malformed_history_reads_as_none(held: object) -> None:
    assert reasoning_history({REASONING_HISTORY_KEY: held}) == {}


# --- the builder a box spawns with ---------------------------------------------


@pytest.fixture
def _gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gateway_session,
        "get_settings",
        lambda: type("S", (), {"alkera_gateway_url": "http://gw"})(),
    )


@pytest.mark.usefixtures("_gateway_url")
def test_a_box_config_built_from_the_pin_alone_names_the_model_moved_off() -> None:
    config = default_gateway_config_builder(with_reasoning_history(OPUS_PIN, FABLE_PIN), token="t")
    assert config is not None
    assert set(_entries(config, "alkera-anthropic")) == {
        "claude-fable-5",
        "claude-fable-5::low",
        "claude-fable-5::high",
    }
    note = _entries(config, "alkera-anthropic")["claude-fable-5::low"]["options"]["alkera"]
    assert note == {
        "readsReasoningFormats": ["anthropic:claude-opus-5", "anthropic:claude-sonnet-5"],
        "reasoningFormatsByModel": {"claude-opus-5": "anthropic:claude-opus-5"},
    }


# --- the session -------------------------------------------------------------


async def test_a_switch_survives_the_chat_being_opened_again(tmp_path: Path) -> None:
    """The manifest is folded from the chat's events on open, so the event a
    switch publishes must carry the history too, or a reopened agent is
    spawned not knowing the reasoning its model reads."""
    project = ProjectDirectory(tmp_path / ".alkera")
    runtime = HarnessRuntime(project, adapter_factory=FakeAdapterFactory())
    session = await runtime.open_chat(create=True, model=dict(OPUS_PIN))
    sid = session.session_id
    await session.set_model(dict(FABLE_PIN))
    await runtime.close_chat(sid)

    reopened = await runtime.open_chat(sid)
    try:
        held = reasoning_history(reopened.manifest.model)
        published = [e for e in reopened.events() if isinstance(e, SessionUpdated)]
    finally:
        await runtime.close_chat(sid)

    assert held == {"claude-opus-5": "anthropic:claude-opus-5"}
    assert published and reasoning_history(published[-1].model or {}) == held


# --- the box waking a chat -----------------------------------------------------


def _mirror_service(tmp_path: Path) -> tuple[CloudMirrorService, HarnessRuntime]:
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / ".alkera"),
        adapter_factory=FakeAdapterFactory(FakeAdapter, available=True),
    )
    settings = MirrorSettings(
        api_url="http://127.0.0.1:9",
        token="device-jwt",
        project_dir=tmp_path,
        machine_name="box",
        provider_pod_id="pod-box",
        machine_type_code="cpu3c",
    )
    rest = CloudRestClient(
        api_url=settings.api_url,
        token=settings.token,
        agent_id="machine:box",
        transport=httpx.MockTransport(lambda _request: httpx.Response(404, json={})),
    )
    service = CloudMirrorService(settings, runtime, rest=rest, socket=cast(CloudSocket, object()))
    return service, runtime


def test_a_woken_chat_keeps_the_formats_its_row_pin_does_not_name(tmp_path: Path) -> None:
    """The row names the chat's model alone. Waking the chat must lay that pin
    on the manifest without losing which models it moved off, or the woken
    agent replays their reasoning as text."""
    service, runtime = _mirror_service(tmp_path)
    store = runtime.project.chats()
    store.create(
        session_id=CHAT_ID,
        title="t",
        model=with_reasoning_history(OPUS_PIN, FABLE_PIN),
        harness_type="agent",
    ).close()
    row_pin = {
        "id": "claude-fable-5",
        "display_name": "Fable 5",
        "wire": "anthropic",
        "efforts": ["low", "high"],
        "effort": "high",
        "reasoning_format": "anthropic:claude-fable-5",
        "reads_reasoning_formats": ["anthropic:claude-opus-5"],
    }
    row = {"id": CHAT_ID, "title": "t", "owner_user_id": OWNER, "model": row_pin}

    service._default_mirror(CHAT_ID, row)._seed_local_chat(store)

    chat = store.open(CHAT_ID)
    try:
        model = dict(chat.manifest.model)
    finally:
        chat.close()
    assert model["effort"] == "high"  # the row's pick is laid on
    assert model["reads_reasoning_formats"] == ["anthropic:claude-opus-5"]
    assert reasoning_history(model) == {"claude-opus-5": "anthropic:claude-opus-5"}
