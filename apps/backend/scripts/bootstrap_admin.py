"""Create the first org + admin on a fresh deployment.

Usage:
    cd apps/backend && uv run python -m scripts.bootstrap_admin \
        --email admin@acme.example --org-name "Acme"

Or entirely via env (how the container / Helm hook invokes it):
    ADMIN_BOOTSTRAP_EMAIL=admin@acme.example \
    ADMIN_BOOTSTRAP_ORG_NAME="Acme" \
    python -m scripts.bootstrap_admin

Idempotent + safe: refuses to do anything if any user already exists, so it can
never mint a second admin on a populated database.

The password is read from ADMIN_BOOTSTRAP_PASSWORD (never an argv flag — argv is
visible in `ps`). If unset, a strong random password is generated and printed
ONCE to stdout; the admin should change it on first login.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

from alkera_core.db.session import AsyncSessionLocal
from backend.composition import install_composition
from backend.seeds.bootstrap_admin import bootstrap_first_admin


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the first org + admin.")
    parser.add_argument("--email", default=os.environ.get("ADMIN_BOOTSTRAP_EMAIL"))
    parser.add_argument("--org-name", default=os.environ.get("ADMIN_BOOTSTRAP_ORG_NAME"))
    parser.add_argument(
        "--first-name", default=os.environ.get("ADMIN_BOOTSTRAP_FIRST_NAME", "Admin")
    )
    parser.add_argument("--last-name", default=os.environ.get("ADMIN_BOOTSTRAP_LAST_NAME", "User"))
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    email: str | None = args.email
    org_name: str | None = args.org_name
    if not email or not org_name:
        print(
            "error: --email/ADMIN_BOOTSTRAP_EMAIL and --org-name/ADMIN_BOOTSTRAP_ORG_NAME "
            "are both required",
            file=sys.stderr,
        )
        return 2

    # Password never comes from argv (visible in `ps`); env or generated.
    password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD") or ""
    generated = not password
    if generated:
        password = secrets.token_urlsafe(16)

    # The org starts with the rows the installed domains register.
    install_composition()
    async with AsyncSessionLocal() as session:
        summary = await bootstrap_first_admin(
            session,
            org_name=org_name,
            admin_email=email,
            admin_first_name=args.first_name,
            admin_last_name=args.last_name,
            admin_password=password,
        )
        await session.commit()

    print(f"==> bootstrap_admin: {summary}")
    if generated and summary.startswith("created"):
        # Only meaningful when we actually created the admin with this password.
        print("\n" + "=" * 60)
        print("  GENERATED ADMIN PASSWORD (shown once — change on first login):")
        print(f"    {password}")
        print("=" * 60 + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
