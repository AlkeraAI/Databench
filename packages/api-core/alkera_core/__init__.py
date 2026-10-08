"""Alkera shared Python building blocks.

Source-of-truth shapes used by `apps/backend`, `apps/worker`,
`apps/cli`, and any future Python app. Holds settings, logging, db session,
ORM models, and Pydantic schemas. Does NOT contain HTTP transport (that's
`apps/backend/backend/api/routes/`) or business logic services (those stay
in backend, since they're session-bound and HTTP-flavored).

External / generated client code lives in `packages/py-sdk` instead.
"""

from __future__ import annotations

__version__: str = "0.0.0"
