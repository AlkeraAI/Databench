"""The generic SQL connector — a catch-all for any SQL database reachable via a
SQLAlchemy URL, built on the shared SQL base with no engine-specific features."""

from __future__ import annotations

from alkera_cli.plugins.generic_sql.plugin import GenericSqlPlugin

__all__ = ["GenericSqlPlugin"]
