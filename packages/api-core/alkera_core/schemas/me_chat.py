"""The reader's own chat settings, over HTTP.

These only ever exist in flight — a response body, a PATCH body — so they are
plain ``BaseModel``s. The thing that is PERSISTED behind them is
:class:`alkera_core.schemas.preferences.Preferences`, which is a
``VersionedModel`` and stays the single shape of a user's preferences whether it
is stored in ``~/.alkera/preferences.yml`` by the CLI or in a row by the server.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alkera_core.schemas.objects.specs import CloudPermissionMode
from alkera_core.schemas.preferences import PERMISSION_MODES


class ChatModelRead(BaseModel):
    """One model the gateway can serve this caller, as the picker reads it.

    The fields are the gateway catalog's own. ``efforts`` is the set of
    reasoning-effort variants the model offers (empty = no variant choice), and
    ``default_effort`` is the one the catalog prefers.
    """

    id: str
    display_name: str
    #: Which provider protocol the model is reached over. The picker does not
    #: act on it; the chat's pin carries it so the box files the model right.
    wire: Literal["anthropic", "openai"]
    efforts: list[str] = Field(default_factory=list)
    default_effort: str | None = None
    family: str = ""
    context_window: int = 0
    #: The reasoning the model emits and the other reasoning it reads on replay.
    reasoning_format: str | None = None
    reads_reasoning_formats: list[str] = Field(default_factory=list)


class ChatModelOption(BaseModel):
    """One model in an open chat's picker, with whether the chat may move to it."""

    model: ChatModelRead
    state: Literal["current", "available", "unavailable"]
    reason_code: (
        Literal["model_not_offered", "harness_wire_unsupported", "reasoning_not_readable"] | None
    ) = None
    #: The sentence every surface shows for an unavailable model.
    message: str | None = None
    #: The same reason without the model's name: the one line a picker shows
    #: above every model that shares it.
    group_message: str | None = None
    #: The model a new chat would use instead, when that is the way out.
    escape_new_chat_model: str | None = None


class ChatModelCurrent(BaseModel):
    model_id: str | None = None
    effort: str | None = None


class ChatModelOptions(BaseModel):
    """What an open chat's model picker offers this caller."""

    current: ChatModelCurrent
    #: Whether this caller may move the chat at all (they may send in it).
    can_switch: bool
    #: When a switch takes effect: the next turn, or once the chat's agent
    #: restarts (a box on a build that cannot move a running agent).
    applies: Literal["next_turn", "after_reopen"] = "next_turn"
    options: list[ChatModelOption] = Field(default_factory=list)
    #: The options are the chat owner's, who pays for its turns, and this
    #: caller is not the owner: their own plan does not decide them.
    billed_to_owner: bool = False


class ChatModelList(BaseModel):
    items: list[ChatModelRead] = Field(default_factory=list)


class ChatDefaultsRead(BaseModel):
    """The new-chat seed: the saved Default Chat Model + Effort, resolved
    against the live catalog.

    ``None`` on either field means "nothing to seed" — the composer falls back
    to the first catalog model. A gateway outage returns the SAVED values
    untouched rather than resetting them, which is the whole reason this is
    resolved on the server and not in the browser.

    ``permission_mode`` is the stance a chat created NOW would open in, resolved
    by the same function the create route uses. A composer that has no chat yet
    can only state the stance by asking someone who knows, and the only thing
    that knows is the resolver — a browser-side constant would be a second
    answer to a question the server already decides, and the two disagree the
    moment a reader saves a default.
    """

    model: str | None = None
    effort: str | None = None
    permission_mode: CloudPermissionMode | None = None


class PreferencesRead(BaseModel):
    """The caller's stored preferences, whole.

    A free-form object on purpose: :class:`Preferences` allows unknown fields so
    a newer client's key survives an older one's read, and a typed mirror here
    would be the thing that dropped it.
    """

    preferences: dict[str, Any] = Field(default_factory=dict, title="UserPreferences")
    chat_model_defaulted: bool = Field(
        default=False,
        description=(
            "True when `default_chat_model` / `default_chat_effort` in this answer "
            "are the platform default rather than the caller's own pick — they "
            "never chose one, or the one they chose is no longer offered. A client "
            "that edits the document shows this as 'no preference' and must not "
            "save the answered model back as a choice."
        ),
    )


class PreferencesUpdate(BaseModel):
    """The fields to apply. MERGED onto what is stored, never a replacement —
    so a key a newer client wrote survives an older client's save.

    ``extra="forbid"`` where the persisted :class:`Preferences` allows extras,
    and the two are not in tension: the STORED document must keep a field it
    cannot name (a newer client wrote it), while a REQUEST that misses the
    envelope has nothing to preserve and everything to lose. A client PATCHing
    the flat ``{"default_permission_mode": …}`` instead of
    ``{"preferences": {…}}`` gets a 422 rather than a 200 with an empty merge
    that silently drops the setting.
    """

    model_config = ConfigDict(extra="forbid")

    preferences: dict[str, Any] = Field(default_factory=dict, title="UserPreferencesPatch")

    @field_validator("preferences")
    @classmethod
    def _known_values_for_known_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Refuse a value the surfaces cannot act on, naming what is allowed.

        Unknown KEYS still pass — that is the forward compatibility the stored
        document is built for. An unknown VALUE for a key we do know is the
        opposite: ``default_permission_mode: "not_a_mode"`` is not a newer
        client's field, it is garbage that every reader then has to normalise,
        and the one that reads it next (a new browser chat) can only fall back
        to its floor. Refusing at the door is the only place the person who
        typed it finds out.
        """
        mode = value.get("default_permission_mode")
        if mode is not None and mode not in PERMISSION_MODES:
            allowed = ", ".join(PERMISSION_MODES)
            raise ValueError(f"default_permission_mode must be one of: {allowed}")
        return value


__all__ = [
    "ChatDefaultsRead",
    "ChatModelList",
    "ChatModelRead",
    "PreferencesRead",
    "PreferencesUpdate",
]
