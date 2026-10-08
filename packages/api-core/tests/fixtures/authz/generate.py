"""Regenerate the actor-document lineage fixtures at the CURRENT writer version.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/authz/generate.py

Writes one ``ActorChainRecord`` dump per credential shape into
``packages/api-core/tests/fixtures/authz/v<SCHEMA_VERSION>/``. Commit the diff alongside the
schema change that prompted the regeneration.

NEVER edit old fixture files by hand. Migrations go in the model's
``MIGRATIONS`` dict; fixtures stay frozen as historical evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from alkera_core.authz import (
    ActingContext,
    ActorChainRecord,
    CredentialKind,
    PrincipalRecord,
)

# Fixed ids so a regenerate is byte-identical until the shape changes.
ORG_ID = UUID("00000000-0000-4000-8000-00000000000a")
USER_ID = UUID("00000000-0000-4000-8000-000000000001")
CI_TOKEN_ID = UUID("00000000-0000-4000-8000-000000000002")
PROXY_TOKEN_ID = UUID("00000000-0000-4000-8000-000000000003")
PAT_ID = UUID("00000000-0000-4000-8000-000000000004")
EMAIL = "member@example.com"
SESSION_ID = "sess-01HZXK4Q"


def contexts() -> dict[str, ActingContext]:
    """One acting context per credential shape the backend resolves."""
    return {
        "user": ActingContext.for_user(user_id=USER_ID, org_id=ORG_ID, email=EMAIL),
        "agent": ActingContext.for_agent(
            user_id=USER_ID, org_id=ORG_ID, email=EMAIL, session_id=SESSION_ID
        ),
        "service_ci": ActingContext.for_service(
            token_id=CI_TOKEN_ID,
            org_id=ORG_ID,
            label="ci: main",
            credential=CredentialKind.CI_TOKEN,
        ),
        "service_proxy": ActingContext.for_service(
            token_id=PROXY_TOKEN_ID,
            org_id=ORG_ID,
            label="proxy: laptop",
            credential=CredentialKind.PROXY_TOKEN,
        ),
        "pat": ActingContext.for_pat(
            token_id=PAT_ID,
            org_id=ORG_ID,
            label="pat: nightly-sync",
            user_id=USER_ID,
            email=EMAIL,
        ),
    }


def records() -> dict[str, ActorChainRecord]:
    return {name: ActorChainRecord.from_context(ctx) for name, ctx in contexts().items()}


def _corpus_version() -> str:
    """The corpus is stamped with the MAX SCHEMA_VERSION across the record and
    every model embedded in it, so a bump on either writes a fresh directory."""
    versions = {
        tuple(int(p) for p in model.SCHEMA_VERSION.split("."))
        for model in (ActorChainRecord, PrincipalRecord)
    }
    return ".".join(str(p) for p in max(versions))


def _version_dir(root: Path, version: str) -> Path:
    return root / f"v{version.replace('.', '_')}"


def regenerate(root: Path | None = None) -> Path:
    """Write every record under ``<root>/v<X_Y_Z>/<name>.json``; returns the
    target directory."""
    root = root or Path(__file__).parent
    version_dir = _version_dir(root, _corpus_version())
    version_dir.mkdir(parents=True, exist_ok=True)
    for name, record in records().items():
        payload = record.model_dump(mode="json")
        # LF on every platform: the lineage test compares these bytes with the
        # committed corpus, and text mode would write CRLF on Windows.
        (version_dir / f"{name}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", newline="\n"
        )
    return version_dir


if __name__ == "__main__":
    target = regenerate()
    print(f"wrote fixtures under {target}")
