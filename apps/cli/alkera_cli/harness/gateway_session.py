"""Default gateway-config builder shared by the CLI + daemon.

Both wire opencode through the model gateway via the same `HarnessRuntime`. At
session start the runtime calls this builder to (re)build the inline opencode
config from the chat's pinned model selection (stored in `manifest.model`) plus
the credential the agent presents to the gateway. Building it per-open (rather
than persisting it) means:

- the credential is never written into the chat manifest, and
- an expired/rotated one is picked up (or surfaces a clear error) at open.

Which credential depends on who opens the chat. A CLOUD chat — one a box
serves for the cloud (``alkera cloud-mirror run``) — is opened with the chat's
own gateway token (``token=``), minted by the backend for that chat alone: the
gateway bills it as the box's session and the API refuses it on every route, so
the agent never holds a credential that acts as the box's person. A LOCAL chat
— ``alkera run``, the editor daemon on a local project — binds
the sign-in profile it resolved when it opened (``account.binding``), and the
runtime hands that profile's token in here the same way; nothing in this module
reads ``~/.alkera/auth.yml``, so a switch of the current profile mid-chat can
never move the chat's billing to another org.

Returns ``None`` for chats that aren't gateway-routed (e.g. opencode's hosted
default model), so non-gateway / test sessions are left untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from alkera_cli.account.auth_file import ProfileResolutionError
from alkera_cli.account.binding import ChatCredential
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.gateway.client import REMEMBERED_CATALOG
from alkera_cli.harness.adapters.opencode_alkera import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    build_alkera_opencode_config,
)
from alkera_cli.harness.turn_model import reasoning_history
from alkera_cli.host.config import get_settings


class GatewayConfigBuilder(Protocol):
    """(manifest.model dict) -> an agent_config dict, or None if the chat isn't
    gateway-routed. The runtime merges a non-None result into the session's
    harness_native WITHOUT persisting it (the credential must not hit disk).

    ``token`` is the credential the agent presents to the gateway: a cloud
    chat's own gateway token, or a local chat's bound profile token. ``None``
    means the chat has no credential, which a gateway-routed chat refuses."""

    def __call__(
        self, manifest_model: dict[str, Any], *, token: str | None = None
    ) -> dict[str, Any] | None: ...


_GATEWAY_PROVIDER_WIRES = {ANTHROPIC_PROVIDER_ID: "anthropic", OPENAI_PROVIDER_ID: "openai"}


class GatewayAuthRequiredError(Exception):
    """The chat routes through the gateway but no valid auth token is available.
    The caller (CLI/daemon) should prompt the user to run ``alkera login``."""


def gateway_credential(token: str | None) -> str:
    """The credential the agent presents to the gateway: the chat's own
    ``token``. Raises :class:`GatewayAuthRequiredError` when the chat has none;
    there is no fallback to whatever sign-in is stored now."""
    if token:
        return token
    raise GatewayAuthRequiredError(
        "this chat routes through the model gateway but you're not signed in — run `alkera login`"
    )


def chat_token(credential: ChatCredential | None) -> str | None:
    """The token ``credential`` presents right now, or None for a chat that has
    no credential. A local chat whose bound profile was logged out raises
    :class:`GatewayAuthRequiredError`: it stops rather than act as another
    profile."""
    if credential is None:
        return None
    try:
        return credential.token()
    except ProfileResolutionError as exc:
        raise GatewayAuthRequiredError(str(exc)) from exc


#: What a box's harness raises when a gateway component is asked to act before
#: any chat's credential was bound to it. A box holds no credential the gateway
#: accepts — its machine credential opens the API, never a model — so there is
#: nothing to fall back to, and reading the operator's login off the disk is
#: exactly what must never happen.
UNBOUND_MESSAGE = "this gateway component presents a chat's own credential and none is bound to it"


@runtime_checkable
class CredentialBound(Protocol):
    """A harness component that presents a credential to the gateway and can
    be handed one chat's own in place of whatever it holds.

    The runtime binds the auto-mode judge, the subagent model resolver and the
    org web-tool flags to each chat's gateway token as it opens the chat and
    runs its turns, so on a box every gateway call a chat causes is made and
    billed as that chat — the per-turn credential the cloud chat already
    hands its agent — and the box's own bearer, or a login on the box's disk,
    is never what the gateway sees.
    """

    def bound_to(self, credential: str) -> Any: ...


T = TypeVar("T")


def bound_to(component: T, credential: str | None) -> T:
    """``component`` presenting ``credential`` to the gateway, when it can be
    bound and there is one; ``component`` itself otherwise — a local chat has
    no per-chat token, and a fake in a test has nothing to bind."""
    if credential is None or not isinstance(component, CredentialBound):
        return component
    return cast(T, component.bound_to(credential))


def gateway_config_builder_for_catalog(
    catalog: Sequence[GatewayModel],
) -> GatewayConfigBuilder:
    """A gateway-config builder that spawns opencode knowing the WHOLE model
    catalog (every selectable provider + model), defaulting to the chat's pinned
    model. The CLI's in-app `/model` picker can then switch the model mid-session
    — the next prompt carries the new `{provider_id, model_id}` and opencode
    already has that provider configured, so no respawn is needed. (The daemon
    sticks with `default_gateway_config_builder`: the editor pins the model at
    chat creation and only changes the reasoning effort mid-session, so it never
    needs the other providers spawned.)

    Falls back to the single-model builder when the catalog is empty (gateway was
    unreachable at launch) so a chat still spawns with its pinned model.
    """

    def build(manifest_model: dict[str, Any], *, token: str | None = None) -> dict[str, Any] | None:
        provider_id = str(manifest_model.get("provider_id") or "")
        if _GATEWAY_PROVIDER_WIRES.get(provider_id) is None:
            return None  # not a gateway-routed chat
        if not catalog:
            return default_gateway_config_builder(manifest_model, token=token)
        credential = gateway_credential(token)
        model_id = str(manifest_model.get("model_id") or "")
        chosen = manifest_model.get("effort")
        return build_alkera_opencode_config(
            gateway_url=get_settings().alkera_gateway_url,
            token=credential,
            models=list(catalog),
            default_model=model_id,
            default_effort=chosen if isinstance(chosen, str) else None,
            reasoning_formats_by_model=reasoning_history(manifest_model),
        )

    return build


def remembered_catalog_config_builder(
    manifest_model: dict[str, Any], *, token: str | None = None
) -> dict[str, Any] | None:
    """The whole catalog this process last read from the gateway, when it holds
    the chat's pinned model, so the session can switch models in place; the
    pinned model alone otherwise (``default_gateway_config_builder``)."""
    catalog = list(REMEMBERED_CATALOG.models)
    pinned = str(manifest_model.get("model_id") or "")
    if not any(m.id == pinned for m in catalog):
        return default_gateway_config_builder(manifest_model, token=token)
    return gateway_config_builder_for_catalog(catalog)(manifest_model, token=token)


def _manifest_str(value: Any) -> str | None:
    """A non-empty string from a persisted manifest field, else ``None``."""
    return value if isinstance(value, str) and value else None


def _manifest_int(value: Any) -> int:
    """A non-negative int from a persisted manifest field, else 0 (= unknown limit).
    An older manifest written before limits were persisted simply yields 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def default_gateway_config_builder(
    manifest_model: dict[str, Any], *, token: str | None = None
) -> dict[str, Any] | None:
    provider_id = str(manifest_model.get("provider_id") or "")
    wire = _GATEWAY_PROVIDER_WIRES.get(provider_id)
    if wire is None:
        return None  # not a gateway-routed chat — leave opencode's default alone

    credential = gateway_credential(token)

    model_id = str(manifest_model.get("model_id") or "")
    efforts = tuple(e for e in (manifest_model.get("efforts") or []) if isinstance(e, str))
    chosen = manifest_model.get("effort")
    chosen_effort = chosen if isinstance(chosen, str) else None
    model = GatewayModel(
        id=model_id,
        display_name=str(manifest_model.get("display_name") or model_id),
        wire=wire,  # type: ignore[arg-type]  # constrained by the lookup above
        efforts=efforts,
        default_effort=chosen_effort,
        # Carry the limits the manifest persisted (build_manifest_model) so the
        # single-model config emits opencode's per-model `limit` and arms native
        # auto-compaction on the daemon/editor path too. Defensive: missing → 0.
        context_window=_manifest_int(manifest_model.get("context_window")),
        max_output_tokens=_manifest_int(manifest_model.get("max_output_tokens")),
        reasoning_format=_manifest_str(manifest_model.get("reasoning_format")),
        reads_reasoning_formats=tuple(
            f for f in manifest_model.get("reads_reasoning_formats") or () if isinstance(f, str)
        ),
    )
    return build_alkera_opencode_config(
        gateway_url=get_settings().alkera_gateway_url,
        token=credential,
        models=[model],
        default_model=model_id,
        default_effort=chosen_effort,
        reasoning_formats_by_model=reasoning_history(manifest_model),
    )
