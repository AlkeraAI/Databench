# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""The marimo library.

The marimo library brings marimo notebooks to life with powerful
UI elements to interact with and transform data, dynamic markdown,
and more.

marimo is designed to be:

    1. simple
    2. immersive
    3. interactive
    4. seamless
    5. fun
"""

from sys import platform as _platform

if _platform == "emscripten":
    # Runtime modules imported below capture `threading.Thread` and
    # `threading.local`. Install the Pyodide concurrency patch first so public
    # imports and runtime context storage share the same stdlib view.
    from alkera_notebook._marimo._runtime._wasm import (
        ensure_wasm_runtime_bootstrapped as _ensure_wasm_runtime_bootstrapped,
    )

    _ensure_wasm_runtime_bootstrapped()

__all__ = [  # noqa: RUF022
    # Core API
    "App",
    "Cell",
    "AppMeta",
    "create_asgi_app",
    "MarimoIslandGenerator",
    "MarimoStopError",
    "Thread",
    "current_thread",
    # Other namespaces
    "ai",
    "ui",
    "islands",
    # Application elements
    "accordion",
    "app_meta",
    "as_html",
    "audio",
    "cache",
    "callout",
    "capture_stderr",
    "capture_stdout",
    "carousel",
    "center",
    "cli_args",
    "defs",
    "doc",
    "download",
    "hstack",
    "Html",
    "icon",
    "iframe",
    "image",
    "image_compare",
    "inspect",
    "json",
    "latex",
    "lazy",
    "left",
    "lru_cache",
    "md",
    "mermaid",
    "mpl",
    "nav_menu",
    "notebook_dir",
    "notebook_location",
    "outline",
    "output",
    "pdf",
    "persistent_cache",
    "plain",
    "plain_text",
    "query_params",
    "redirect_stderr",
    "redirect_stdout",
    "refs",
    "right",
    "routes",
    "running_in_notebook",
    "show_code",
    "sidebar",
    "sql",
    "stat",
    "state",
    "status",
    "stop",
    "style",
    "tabs",
    "tree",
    "video",
    "vstack",
    "watch",
    "__version__",
]
import alkera_notebook._marimo._ai as ai
import alkera_notebook._marimo._islands as islands
from alkera_notebook._marimo._ast.app import App
from alkera_notebook._marimo._ast.cell import Cell
from alkera_notebook._marimo._islands._island_generator import MarimoIslandGenerator
from alkera_notebook._marimo._output.doc import doc
from alkera_notebook._marimo._output.formatting import as_html, iframe, plain
from alkera_notebook._marimo._output.hypertext import Html
from alkera_notebook._marimo._output.justify import center, left, right
from alkera_notebook._marimo._output.md import latex, md
from alkera_notebook._marimo._output.outline import outline
from alkera_notebook._marimo._output.show_code import show_code
from alkera_notebook._marimo._plugins import ui
from alkera_notebook._marimo._plugins.stateless import mpl, status
from alkera_notebook._marimo._plugins.stateless.accordion import accordion
from alkera_notebook._marimo._plugins.stateless.audio import audio
from alkera_notebook._marimo._plugins.stateless.callout import callout
from alkera_notebook._marimo._plugins.stateless.carousel import carousel
from alkera_notebook._marimo._plugins.stateless.download import download
from alkera_notebook._marimo._plugins.stateless.flex import hstack, vstack
from alkera_notebook._marimo._plugins.stateless.icon import icon
from alkera_notebook._marimo._plugins.stateless.image import image
from alkera_notebook._marimo._plugins.stateless.image_compare import image_compare
from alkera_notebook._marimo._plugins.stateless.inspect import inspect
from alkera_notebook._marimo._plugins.stateless.json_component import json
from alkera_notebook._marimo._plugins.stateless.lazy import lazy
from alkera_notebook._marimo._plugins.stateless.mermaid import mermaid
from alkera_notebook._marimo._plugins.stateless.nav_menu import nav_menu
from alkera_notebook._marimo._plugins.stateless.pdf import pdf
from alkera_notebook._marimo._plugins.stateless.plain_text import plain_text
from alkera_notebook._marimo._plugins.stateless.routes import routes
from alkera_notebook._marimo._plugins.stateless.sidebar import sidebar
from alkera_notebook._marimo._plugins.stateless.stat import stat
from alkera_notebook._marimo._plugins.stateless.style import style
from alkera_notebook._marimo._plugins.stateless.tabs import tabs
from alkera_notebook._marimo._plugins.stateless.tree import tree
from alkera_notebook._marimo._plugins.stateless.video import video
from alkera_notebook._marimo._runtime import output, watch
from alkera_notebook._marimo._runtime.app_meta import AppMeta
from alkera_notebook._marimo._runtime.capture import (
    capture_stderr,
    capture_stdout,
    redirect_stderr,
    redirect_stdout,
)
from alkera_notebook._marimo._runtime.context.utils import running_in_notebook
from alkera_notebook._marimo._runtime.control_flow import MarimoStopError, stop
from alkera_notebook._marimo._runtime.runtime import (
    app_meta,
    cli_args,
    defs,
    notebook_dir,
    notebook_location,
    query_params,
    refs,
)
from alkera_notebook._marimo._runtime.state import state
from alkera_notebook._marimo._runtime.threads import Thread, current_thread
from alkera_notebook._marimo._save.save import cache, lru_cache, persistent_cache
from alkera_notebook._marimo._server.asgi import create_asgi_app
from alkera_notebook._marimo._sql.sql import sql
from alkera_notebook._marimo._version import __version__
