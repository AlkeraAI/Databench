"""The format API the engine consumes, as a seam.

The engine reaches ``alkera_notebook.format`` only through :class:`FormatApi`,
so a store, the planner or a test can be handed another implementation, and
so nothing in the engine imports the format module at import time.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast


class FormatApi(Protocol):
    """The functions of ``alkera_notebook.format`` the engine calls.

    The IR values (``NotebookIR`` and ``CellIR``) are the format package's frozen
    dataclasses; the engine builds them through :meth:`notebook_ir` and
    :meth:`cell_ir` and reads them by attribute.
    """

    def read(self, text: str, *, known: Mapping[str, str] | None = None) -> Any: ...

    def write(self, ir: Any) -> str: ...

    def render_cell(self, kind: str, source: str, meta: Mapping[str, Any]) -> str: ...

    def classify(self, code: str) -> tuple[str, str, dict[str, Any]]: ...

    def normalize_code(self, code: str) -> str: ...

    def new_cell_id(self) -> str: ...

    def analyze_code(self, cells: Sequence[tuple[str, str]]) -> dict[str, Any]: ...

    def compile_step(self, code: str, *, cell_id: str | None = None) -> dict[str, Any]: ...

    def setup_with_runtime(self, code: str) -> str: ...

    def notebook_ir(self, **fields: Any) -> Any: ...

    def cell_ir(self, **fields: Any) -> Any: ...


class ModuleFormat:
    """:class:`FormatApi` over the format module."""

    def __init__(self, module_name: str = "alkera_notebook.format") -> None:
        self._m = importlib.import_module(module_name)

    def read(self, text: str, *, known: Mapping[str, str] | None = None) -> Any:
        return self._m.read(text, known=known)

    def write(self, ir: Any) -> str:
        return cast(str, self._m.write(ir))

    def render_cell(self, kind: str, source: str, meta: Mapping[str, Any]) -> str:
        return cast(str, self._m.render_cell(kind, source, meta))

    def classify(self, code: str) -> tuple[str, str, dict[str, Any]]:
        return cast("tuple[str, str, dict[str, Any]]", self._m.classify(code))

    def normalize_code(self, code: str) -> str:
        return cast(str, self._m.normalize_code(code))

    def new_cell_id(self) -> str:
        return cast(str, self._m.new_cell_id())

    def analyze_code(self, cells: Sequence[tuple[str, str]]) -> dict[str, Any]:
        return cast("dict[str, Any]", self._m.analyze_code(cells))

    def compile_step(self, code: str, *, cell_id: str | None = None) -> dict[str, Any]:
        return cast("dict[str, Any]", self._m.compile_step(code, cell_id=cell_id))

    def setup_with_runtime(self, code: str) -> str:
        return cast(str, self._m.setup_with_runtime(code))

    def notebook_ir(self, **fields: Any) -> Any:
        return self._m.NotebookIR(**fields)

    def cell_ir(self, **fields: Any) -> Any:
        return self._m.CellIR(**fields)


def default_format() -> FormatApi:
    return ModuleFormat()
