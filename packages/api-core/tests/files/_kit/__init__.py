"""Importable helpers for the api-core Files suite.

The package name is unique across the repo so a cross-tree ``pytest`` session —
the one ``make test`` runs, collecting ``apps/backend/tests`` and
``packages/api-core/tests`` together — resolves it unambiguously. ``tests.files``
is a namespace package with a portion in each tree, so a module named
``conftest`` in both portions resolves to whichever tree comes first on the
path; shared names therefore live here rather than in ``conftest``.
"""
