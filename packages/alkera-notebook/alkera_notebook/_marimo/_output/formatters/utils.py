# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

import html

import alkera_notebook._marimo._output.data.data as mo_data
from alkera_notebook._marimo._runtime.context.types import (
    ContextNotInitializedError,
    get_context,
)


def src_or_src_doc(html_content: str) -> dict[str, str]:
    """
    Depending if virtual files are supported,
    return the appropriate src or srcdoc attribute for an iframe.

    While `src:text/html;base64` is supported in most modern browsers,
    it does not allow us to resize the iframe to fit the content.

    So, we use `srcdoc` when not running a server (e.g. an html export).
    """

    try:
        ctx = get_context()
    except ContextNotInitializedError:
        # If no context, return srcdoc
        return {"srcdoc": html.escape(html_content)}

    if ctx.virtual_files_supported:
        html_file = mo_data.html(html_content)
        return {"src": html_file.url}
    else:
        return {"srcdoc": html.escape(html_content)}
