"""Between the format's IR and the document model."""

from __future__ import annotations

from typing import Any

from alkera_notebook.document.fmt import FormatApi
from alkera_notebook.document.model import KNOWN_SETTINGS, DocCell, DocMeta, Document
from alkera_notebook.format.ir import file_settings
from alkera_notebook.format.op_rules import TEMPLATED_KINDS


def cell_code(fmt: FormatApi, kind: str, source: str, meta: dict[str, Any]) -> str:
    """A cell's Python code: templated kinds render their source, the rest are it."""
    if kind in TEMPLATED_KINDS:
        return fmt.render_cell(kind, source, meta)
    return source


def document_from_ir(ir: Any, fmt: FormatApi) -> Document:
    del fmt  # the IR already carries each cell's code as written
    # A document holds what its file sets, never the defaults a reader fills in.
    settings = {k: v for k, v in file_settings(ir).items() if k in KNOWN_SETTINGS}
    settings.setdefault("format", ir.format)
    doc = Document(
        meta=DocMeta(
            format=str(ir.format),
            generated_with=str(ir.generated_with),
            header=str(ir.header_text),
            app=dict(ir.app_config),
        ),
        settings=settings,
        unknown_settings=str(ir.unknown_settings),
        read_only_reason=ir.read_only_reason,
    )
    for c in ir.cells:
        if c.id in doc.cells:
            continue
        doc.cells[c.id] = DocCell(
            id=c.id,
            kind=c.kind,
            name=c.name,
            source=c.source,
            code=c.code,
            config=dict(c.config),
            meta=dict(c.meta),
            extra=dict(c.extra),
        )
        doc.order.append(c.id)
    return doc


def ir_from_document(doc: Document, fmt: FormatApi) -> Any:
    settings = {k: v for k, v in doc.settings.items() if v is not None}
    settings["format"] = doc.meta.format
    cells = [
        fmt.cell_ir(
            id=c.id,
            kind=c.kind,
            name=c.name,
            source=c.source,
            code=c.code,
            config=dict(c.config),
            meta=dict(c.meta),
            extra=dict(c.extra),
            resolution="keyword",
        )
        for c in doc.live_cells()
    ]
    return fmt.notebook_ir(
        format=doc.meta.format,
        header_text=doc.meta.header,
        settings=settings,
        unknown_settings=doc.unknown_settings,
        app_config=dict(doc.meta.app),
        generated_with=doc.meta.generated_with,
        cells=cells,
        violations=(),
        read_only_reason=doc.read_only_reason,
    )
