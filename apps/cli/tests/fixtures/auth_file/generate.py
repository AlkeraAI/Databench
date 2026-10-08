"""Regenerate the current writer's ``auth.yml`` fixture.

Run from the repo root when ``AuthFileV2.SCHEMA_VERSION`` changes:

    uv run python apps/cli/tests/fixtures/auth_file/generate.py

It writes ``v<version>.yml`` beside this file. Never edit or delete an older
fixture: the lineage test loads every one with the current reader.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from _profiles import API, ORG_A, ORG_B, make_jwt  # noqa: E402
from alkera_cli.account.auth_file import AuthFileV2, profile_from_token  # noqa: E402

EXPIRES = datetime(2100, 1, 1, tzinfo=UTC)


def main() -> None:
    a = profile_from_token(
        API, make_jwt(org=ORG_A, exp=4102444800), org_name="Acme", expires_at=EXPIRES
    )
    b = profile_from_token(
        API, make_jwt(org=ORG_B, exp=4102444800), org_name="Bravo", expires_at=EXPIRES
    )
    doc = AuthFileV2(current=a.key, profiles=[a, b]).to_document()
    version = AuthFileV2.SCHEMA_VERSION.replace(".", "_")
    (HERE / f"v{version}.yml").write_text(yaml.safe_dump(doc, sort_keys=False))


if __name__ == "__main__":
    main()
