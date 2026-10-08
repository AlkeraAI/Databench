"""Org-level capability flags a :class:`~alkera_cli.harness.runtime.HarnessRuntime`
is built with.

Every long-lived runtime — the editor daemon, the cloud mirror's publisher —
is constructed before (and independently of) a gateway round-trip, so an org
toggle cannot be pinned at construction time. These providers are the lazy
form the runtime awaits for each chat: as the chat opens and at the start of
each of its turns, bound to the chat's own credential, never once per process.
"""

from __future__ import annotations

from alkera_cli.gateway.client import fetch_catalog
from alkera_cli.harness.web_flags import WebToolFlags
from alkera_cli.host.config import get_settings


class OrgWebFlags:
    """The org's web-tools toggles, read from the gateway as one credential.

    A runtime is built before (and independently of) sign-in, so the flags
    can't be pinned at construction — the runtime awaits this provider as each
    chat opens and again at the start of each turn, so an admin who turns the
    org toggle off (or an operator who flips the deployment's ``web.fetch``
    switch and restarts the gateway) sees the tools drop on the next turn.

    Neither the local provider (:data:`web_search_org_flag`) nor a box's
    (:data:`machine_web_flags`) holds a credential of its own: the runtime
    binds each to the chat's credential (:meth:`bound_to`) as the chat opens
    and at each turn. A local chat's credential is the sign-in profile it
    bound when it opened, a cloud chat's its own gateway token, so the toggle
    a chat sees is its own org's, never the org of whichever sign-in is
    current now or of a login on a box's disk.

    No credential reads as disabled. A gateway/catalog failure propagates to
    the runtime's resolver, which logs it and reads it as disabled too — the
    capability gate fails closed, and neither a False nor a failed read is
    memoized, so a later rebuild (post-login, gateway back) can still enable it.
    """

    def __init__(self, *, credential: str | None = None) -> None:
        self._credential = credential

    def bound_to(self, credential: str) -> OrgWebFlags:
        """These flags read as ``credential``."""
        return OrgWebFlags(credential=credential)

    async def __call__(self) -> WebToolFlags:
        token = self._credential
        if token is None:
            return WebToolFlags()
        catalog = await fetch_catalog(gateway_url=get_settings().alkera_gateway_url, token=token)
        return WebToolFlags(search=catalog.web_search_enabled, fetch=catalog.web_fetch_enabled)


#: The local runtime's provider: the org toggles as each chat's bound profile.
web_search_org_flag = OrgWebFlags()
#: A box's provider: nothing until bound to a chat's gateway token.
machine_web_flags = OrgWebFlags()


__all__ = ["OrgWebFlags", "WebToolFlags", "machine_web_flags", "web_search_org_flag"]
