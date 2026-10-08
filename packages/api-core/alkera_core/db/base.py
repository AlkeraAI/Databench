"""SQLAlchemy 2.0 declarative base.

All models inherit from `Base`. Import models into this module's namespace
(via `app.models` package __init__) so Alembic autogenerate sees them.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
