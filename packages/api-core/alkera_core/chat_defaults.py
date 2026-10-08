"""Resolve the new-chat Default Model + reasoning Effort from saved preferences.

The single authority for the rules every client that seeds a brand-new chat
obeys — the CLI, the editor's composer through the daemon, and
the browser's composer through the backend:

- a transient gateway/catalog fetch failure NEVER wipes a saved default;
- a saved model the catalog no longer offers (deleted, disabled, no enabled
  route, or never real) falls back gracefully — it is never an error;
- a reader who has chosen nothing, or whose choice no longer resolves, starts on
  the platform default where the deployment names one
  (:func:`hosted_default_model`), else on the catalog's first entry;
- the effort fallback is the catalog ``default_effort``, then the middle-most
  offered effort;
- "nothing chosen" is never written back as a choice: the platform default is a
  preference Alkera can move, and a reader who never picked must move with it.

This is the ONE resolver every reader of the saved Default Chat Model goes
through — the preferences read, the new-chat seed, the create route, Slack and
template starts, the TUI, the daemon and headless runs.

No I/O — so it is unit-testable with plain data, and it lives in
``alkera_core`` rather than in the CLI because the backend resolves the same
defaults for a browser reader and the two must not drift. The callers wrap it
with the catalog fetch and the persistence of a correction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from alkera_core.config import settings
from alkera_core.gateway import HOSTED_DEFAULT_MODEL_SLUG, split_model_effort


class EffortModel(Protocol):
    """What the resolver needs to know about one catalog entry.

    A protocol rather than a class so the CLI's ``GatewayModel`` and the
    backend's catalog row both satisfy it without either importing the other.
    Read-only members: a caller's tuple of efforts is as good as a list.
    """

    @property
    def id(self) -> str: ...

    @property
    def efforts(self) -> Sequence[str]: ...

    @property
    def default_effort(self) -> str | None: ...


def middle_effort(efforts: Sequence[str]) -> str | None:
    """The middle-most effort (index ``len // 2``), or ``None`` when there are
    none. With ``("low", "medium", "high")`` this is ``"medium"``; the even case
    leans to the upper of the two middles (``len // 2``)."""
    return efforts[len(efforts) // 2] if efforts else None


def default_effort_for(model: EffortModel) -> str | None:
    """The effort to auto-pick for ``model`` when no valid one is saved: the
    catalog's per-model ``default_effort`` when it's set and actually offered, else
    the middle-most offered effort, else ``None`` (the model has no effort
    variants)."""
    if model.default_effort and model.default_effort in model.efforts:
        return model.default_effort
    return middle_effort(model.efforts)


def hosted_default_model() -> str | None:
    """The model a reader with no saved choice starts on, or ``None`` when the
    deployment does not steer one.

    Alkera's hosted SaaS pins one model for everybody who has not chosen
    (``HOSTED_DEFAULT_MODEL_SLUG``); a self-hosted install is never steered onto
    a model Alkera picked, so it keeps the catalog's own first entry. The pin is
    only consulted when there is no saved preference to honour, and only when the
    catalog actually offers the slug — an org whose entitlement or BYOK filter
    excludes it falls back to the first offered model rather than to nothing."""
    return None if settings.is_self_hosted else HOSTED_DEFAULT_MODEL_SLUG


@dataclass(frozen=True)
class ChatDefaults:
    """The resolved new-chat seed.

    ``defaulted`` is True when ``model_id`` is not the reader's own pick — they
    never chose, or what they chose no longer resolves — so it came from the
    platform default or the catalog's first entry.

    ``reset`` is True when the SAVED preference is stale and the caller should
    persist :attr:`saved_model` / :attr:`saved_effort` so it converges. It is
    never True for a reader who saved nothing: writing the default back would
    turn "follow the platform default" into a pin the next default change could
    not move.
    """

    model_id: str | None
    effort: str | None
    reset: bool
    defaulted: bool = False

    @property
    def saved_model(self) -> str | None:
        """What the stored ``default_chat_model`` should hold after a correction:
        nothing when the pick no longer resolves (the reader follows the platform
        default again), else the pick itself."""
        return None if self.defaulted else self.model_id

    @property
    def saved_effort(self) -> str | None:
        """The stored ``default_chat_effort`` paired with :attr:`saved_model`."""
        return None if self.defaulted else self.effort


def resolve_chat_defaults(
    selectable: Sequence[EffortModel],
    pref_model: str | None,
    pref_effort: str | None,
) -> ChatDefaults:
    """Resolve the saved Default Chat Model + Effort against the live catalog.

    ``selectable`` is the catalog already filtered to human-pickable models, in
    gateway order — the gateway lists only enabled models with an enabled route,
    so "offered" here means exists, enabled and routable. An EMPTY
    ``selectable`` means the gateway is unreachable: the saved values are
    returned untouched (``reset=False``) so a one-off outage never wipes the
    user's default.

    Otherwise: an absent / no-longer-offered ``pref_model`` falls back to the
    deployment's platform default when the catalog offers it, else to the first
    selectable model. A saved ``"<model>::<effort>"`` variant is read as its two
    halves. The effort is kept when the resolved model still offers it, else
    re-derived via ``default_effort_for`` (``None`` when the model has no
    effort variants).
    """
    if not selectable:
        return ChatDefaults(pref_model, pref_effort, reset=False)

    wanted, variant_effort = split_model_effort(pref_model) if pref_model else (None, None)
    wanted_effort = pref_effort if pref_effort is not None else variant_effort
    by_id = {m.id: m for m in selectable}
    model = by_id.get(wanted) if wanted else None
    defaulted = model is None
    if model is None:
        model = by_id.get(hosted_default_model() or "") or selectable[0]

    if not model.efforts:
        effort: str | None = None
    elif not defaulted and wanted_effort in model.efforts:
        effort = wanted_effort
    else:
        effort = default_effort_for(model)

    result = ChatDefaults(model.id, effort, reset=False, defaulted=defaulted)
    stale = pref_model is not None and (
        result.saved_model != pref_model or result.saved_effort != pref_effort
    )
    return ChatDefaults(model.id, effort, reset=stale, defaulted=defaulted)


__all__ = [
    "ChatDefaults",
    "EffortModel",
    "default_effort_for",
    "hosted_default_model",
    "middle_effort",
    "resolve_chat_defaults",
]
