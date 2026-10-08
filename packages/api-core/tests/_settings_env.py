"""Sealing the ambient process environment off from a ``Settings`` assertion.

Lives beside the suite rather than inside ``conftest.py`` so a test module can call
it directly (``from _settings_env import seal_settings_env``) as well as take the
``sealed_settings_env`` fixture that wraps it.
"""

from __future__ import annotations

import pytest


def seal_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable ``Settings`` reads from this process.

    ``Settings(_env_file=None)`` disables the dotenv FILES but NOT ``os.environ`` —
    pydantic-settings always layers real env vars on top of the defaults. A shell
    that exported the dev config (``set -a; . .env.local``), CI's ``test`` job
    (``OAUTH_MOCK_ENABLED=true``) or the ``e2e`` job's provider creds therefore bleed
    into any test that asserts a DEFAULT, or that builds an EXPLICIT config and
    expects the validator to see only what it passed: one ambient ``FILES_ENABLED``
    turns a "production baseline is accepted" case into a refusal about a missing
    signing key, and a refusal case into a match against the wrong message.

    The names come from the model rather than a hand-kept list, so a setting added
    tomorrow is sealed the day it is added instead of the day someone notices.
    """
    from alkera_core.config import Settings

    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
