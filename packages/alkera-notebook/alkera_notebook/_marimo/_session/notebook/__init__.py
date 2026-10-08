# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Notebook file management and storage abstractions."""

from __future__ import annotations

from alkera_notebook._marimo._session.notebook.file_manager import (
    AppFileManager,
    read_css_file,
    read_html_head_file,
)
from alkera_notebook._marimo._session.notebook.loader import (
    load_notebook,
    new_notebook,
)
from alkera_notebook._marimo._session.notebook.serializer import (
    MarkdownNotebookSerializer,
    NotebookSerializer,
    PythonNotebookSerializer,
)
from alkera_notebook._marimo._session.notebook.storage import (
    FilesystemStorage,
    StorageInterface,
)

__all__ = [
    "AppFileManager",
    "FilesystemStorage",
    "MarkdownNotebookSerializer",
    "NotebookSerializer",
    "PythonNotebookSerializer",
    "StorageInterface",
    "load_notebook",
    "new_notebook",
    "read_css_file",
    "read_html_head_file",
]
