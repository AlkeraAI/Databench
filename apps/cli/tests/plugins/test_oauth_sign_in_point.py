"""Browser sign-in is a registration: a build without one refuses an OAuth
method plainly, and two registrations are a composition error."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.oauth_sign_in import OAuthSignIn, oauth_sign_in
from alkera_core.extensions import ExtensionError, ExtensionPoint


class _Probe:
    async def sign_in(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("never dialed")

    def provider_of(self, path: Path) -> str:
        return path.name


def _point(*registered: _Probe) -> ExtensionPoint[OAuthSignIn]:
    point: ExtensionPoint[OAuthSignIn] = ExtensionPoint(f"probe-{uuid.uuid4()}")
    for item in registered:
        point.register(item)
    return point


def test_a_build_without_browser_sign_in_refuses_it_plainly() -> None:
    with pytest.raises(ValueError, match="browser sign-in is not available in this build"):
        oauth_sign_in(_point())


def test_the_one_registered_sign_in_is_the_one_used() -> None:
    probe = _Probe()
    assert oauth_sign_in(_point(probe)) is probe


def test_two_sign_ins_are_a_composition_error() -> None:
    with pytest.raises(ExtensionError, match="more than one"):
        oauth_sign_in(_point(_Probe(), _Probe()))
