"""The Alembic revision the running code expects the database to be at.

Kept next to the code (not read off the migrations directory) so a container
that ships with new code but a not-yet-migrated database answers "not ready"
instead of serving requests against a schema it was not written for. Bump it
in the same change that adds a migration; ``apps/backend/tests`` pins the two
together so a forgotten bump is a failing test rather than a
false-negative health check.
"""

from __future__ import annotations

EXPECTED_SCHEMA_HEAD = "0224"
