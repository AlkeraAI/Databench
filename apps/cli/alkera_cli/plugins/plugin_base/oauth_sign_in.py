"""Browser sign-in for a connection: the ``OAUTH_SIGN_IN`` extension point.

A plugin's form can offer an OAuth method (an ``OAuthSpec`` on the auth method).
The registry owns the connection side of that sign-in (which method, where the
token bundle lives, the probe that has to pass before the bundle is kept); the
handshake itself (the provider's endpoints, the loopback redirect, PKCE, the
token bundle's format) belongs to whoever registers here.

With nothing registered a build offers no browser sign-in, and an OAuth method
is refused with a plain message.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from alkera_core.extensions import ExtensionError, ExtensionPoint


class SignedIn(Protocol):
    """The token bundle a finished handshake minted, not yet stored."""

    def write(self, path: Path) -> None:
        """Store the bundle at ``path`` (the caller owns the directory)."""
        ...


class OAuthSignIn(Protocol):
    """Runs a provider's browser handshake and reads the bundles it wrote."""

    async def sign_in(
        self,
        provider: str,
        attributes: Mapping[str, str],
        *,
        app: object | None,
        reuse_org_client: bool,
        open_browser: Callable[[str], None],
        timeout: float,  # noqa: ASYNC109 - the browser-authorization deadline, caller-tunable
    ) -> SignedIn:
        """Sign in with ``provider`` for a connection carrying ``attributes``.

        ``app`` is the OAuth client to sign in with, ``None`` for the provider's
        own. ``reuse_org_client`` asks for the client the connection's attributes
        already carry when ``app`` is ``None`` (a second sign-in uses the client
        the first one registered)."""
        ...

    def provider_of(self, path: Path) -> str:
        """The provider that minted the bundle at ``path``, or ``""`` when the
        bundle does not say. Raises ``OSError`` or ``ValueError`` for a bundle
        that cannot be read."""
        ...


#: The browser sign-in. At most one is registered.
OAUTH_SIGN_IN: ExtensionPoint[OAuthSignIn] = ExtensionPoint("oauth_sign_in")


def oauth_sign_in(point: ExtensionPoint[OAuthSignIn] = OAUTH_SIGN_IN) -> OAuthSignIn:
    """The browser sign-in registered on ``point``. Raises ``ValueError`` when
    this build has none, so a caller surfaces it like any other refused method."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError("more than one extension provides browser sign-in")
    if not registered:
        raise ValueError("browser sign-in is not available in this build")
    return registered[0]


__all__ = ["OAUTH_SIGN_IN", "OAuthSignIn", "SignedIn", "oauth_sign_in"]
