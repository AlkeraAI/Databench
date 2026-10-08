"""``alkera-notebook``: run, check and create notebooks without any service.

- ``run <file> [--target all|stale] [--known <file>] [--clean --compare]
  [--python <interp>] [--memory <bytes>] [-- --key value ...]`` runs the
  notebook in a fresh local kernel and writes its snapshot. With
  ``--compare`` it first reads the saved snapshot, then reports per cell
  whether the new outputs equal the saved ones; cells with external inputs
  (SQL, network) are marked so changed data is told apart from
  nondeterminism.
- ``check <file>`` reports format violations and graph errors.
- ``new <file>`` writes a notebook with one empty cell.

Exit codes: 0 success; 1 a cell failed, a check found problems, or a
comparison differed; 2 a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from alkera_notebook.cell_names import cell_display_name
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import FormatApi, default_format
from alkera_notebook.document.ops import InsertCell
from alkera_notebook.engine import (
    Actor,
    AllTarget,
    EngineConfig,
    MemoryPolicy,
    NotebookEngine,
    SequentialIds,
    StaleTarget,
    SystemClock,
)
from alkera_notebook.envs.models import EnvDescriptor, EnvRegistry
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.kernels.launcher import (
    KernelLauncher,
    LocalSubprocessLauncher,
    UnixSocketTransport,
)
from alkera_notebook.memory.source import ProcessRssSource
from alkera_notebook.outputs import CellOutputs, SnapshotRead, read_snapshot
from alkera_notebook.plan.graph import CellGraph
from alkera_notebook.sql.provider import SqlEngineProvider, SqlProviderRegistry

GiB = 1024**3

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# Code that reads data from outside the notebook: a changed output there is
# changed data, not nondeterminism.
_EXTERNAL = re.compile(r"\balkera\.sql\b|\bsql\(|\burllib\b|\brequests\b|\bhttpx\b|\bsocket\b")


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse exits otherwise
        raise UsageError(message)


def _parser() -> _Parser:
    p = _Parser(prog="alkera-notebook", description="Run and check Alkera notebooks.")
    sub = p.add_subparsers(dest="command", parser_class=_Parser)
    run = sub.add_parser("run", help="run a notebook in a fresh local kernel")
    run.add_argument("file")
    run.add_argument("--target", choices=("all", "stale"), default="all")
    run.add_argument("--known", help="a previous version of the notebook, for cell ids")
    run.add_argument("--clean", action="store_true", help="start from a fresh kernel")
    run.add_argument("--compare", action="store_true", help="compare outputs with the snapshot")
    run.add_argument("--python", help="the interpreter for the kernel")
    run.add_argument("--memory", type=int, default=4 * GiB, help="memory budget in bytes")
    check = sub.add_parser("check", help="report format violations and graph errors")
    check.add_argument("file")
    new = sub.add_parser("new", help="write a new notebook with one empty cell")
    new.add_argument("file")
    return p


def parse_script_args(args: Sequence[str]) -> dict[str, str]:
    """``--key value`` pairs (and bare ``--flag``) after ``--``."""
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("--") or len(token) == 2:
            raise UsageError(f"expected --key value after --, got {token!r}")
        key, eq, value = token[2:].partition("=")
        if eq:
            out[key] = value
            i += 1
        elif i + 1 < len(args) and not args[i + 1].startswith("--"):
            out[key] = args[i + 1]
            i += 2
        else:
            out[key] = "true"
            i += 1
    return out


def _actor() -> Actor:
    try:
        name = getpass.getuser()
    except (KeyError, OSError):
        name = "cli"
    return Actor(kind="person", id=f"cli:{name}", display_name=name, can_edit=True, can_run=True)


class _KnownFormat:
    """A format whose reads use ``known`` (id -> code) when the caller gives none."""

    def __init__(self, inner: FormatApi, known: Mapping[str, str]) -> None:
        self._inner = inner
        self._known = dict(known)

    def read(self, text: str, *, known: Mapping[str, str] | None = None) -> Any:
        return self._inner.read(text, known=known if known is not None else self._known)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# Comparison ------------------------------------------------------------------


def output_hash(out: CellOutputs | None) -> str:
    """A stable hash of what a cell showed: bundles, console text, error class
    and message (tracebacks carry paths and are left out)."""
    if out is None:
        payload: dict[str, Any] = {"bundles": [], "console": [], "error": None}
    else:
        payload = {
            "bundles": out.bundles,
            "console": [[i.name, i.text] for i in out.console],
            "error": [out.error.ename, out.error.evalue] if out.error else None,
        }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_external(kind: str, code: str) -> bool:
    return kind == "sql" or bool(_EXTERNAL.search(code))


Verdict = Literal["same", "changed", "new", "missing"]


@dataclass(frozen=True)
class CellComparison:
    cell_id: str
    name: str
    verdict: Verdict
    external: bool
    index: int | None = None
    """The cell's position: in the notebook, or in the snapshot for a cell
    removed since."""

    @property
    def label(self) -> str:
        return cell_display_name(self.name, self.index)

    @property
    def counts(self) -> bool:
        """Whether this cell decides the exit code."""
        return not self.external and self.verdict != "same"


# Commands --------------------------------------------------------------------


@dataclass(frozen=True)
class _RunInput:
    path: Path
    fmt: FormatApi
    saved: SnapshotRead | None
    recorded_env: str | None


def _prepare_run(ns: argparse.Namespace, fmt: FormatApi) -> _RunInput:
    """Everything ``run`` reads from disk before the engine starts. The saved
    snapshot is read here, before any run can rewrite it."""
    path = Path(ns.file).resolve()
    if not path.is_file():
        raise UsageError(f"no such notebook: {ns.file}")
    if ns.python and not Path(ns.python).exists():
        raise UsageError(f"no such interpreter: {ns.python}")
    if ns.known:
        known_path = Path(ns.known)
        if not known_path.is_file():
            raise UsageError(f"no such file: {ns.known}")
        known_ir = fmt.read(known_path.read_text(encoding="utf-8"))
        fmt = cast(FormatApi, _KnownFormat(fmt, {c.id: c.code for c in known_ir.cells}))
    recorded = fmt.read(path.read_text(encoding="utf-8")).settings.get("env")
    saved = read_snapshot(path) if ns.compare else None
    return _RunInput(path, fmt, saved, str(recorded) if recorded else None)


async def _run(
    ns: argparse.Namespace,
    prepared: _RunInput,
    script_args: dict[str, str],
    *,
    launcher: KernelLauncher | None,
    mount: str | None,
    sql: Sequence[SqlEngineProvider],
    envs: EnvRegistry | None,
    out: TextIO,
) -> int:
    path, fmt, saved = prepared.path, prepared.fmt, prepared.saved
    data_root = Path(tempfile.mkdtemp(prefix="alknb-run-"))
    try:
        root = path.parent
        config = EngineConfig(
            workspace_root=str(root),
            env_root=str(data_root / "envs"),
            data_root=str(data_root),
            kernel_mount=mount,
            memory=MemoryPolicy(),
            kernel_args=script_args,
        )
        if envs is None:
            envs = await _pick_envs(ns.python, root, path, prepared.recorded_env, out)
        engine = NotebookEngine(
            config,
            store=FileDocumentStore(root, fmt=fmt),
            launcher=launcher or LocalSubprocessLauncher(),
            transport=UnixSocketTransport(),
            memory=ProcessRssSource(ns.memory),
            sql=_sql_registry(sql),
            envs=envs,
            clock=SystemClock(),
            ids=SequentialIds(),
            fmt=fmt,
        )
        try:
            session = await engine.open(path.name)
            client = session.attach(_actor())
            target = StaleTarget() if ns.target == "stale" else AllTarget()
            handle = await client.run(target, confirm_expensive=True)
            record = await handle.wait()
            view = await client.read()
            runtime = session.runtime
            failed = False
            for cell in view.cells:
                label = cell_display_name(cell.name, cell.index)
                status = cell.status
                if status not in ("fresh", "disabled"):
                    failed = True
                o = runtime.outputs.get(cell.id)
                line = ""
                if o is not None and o.origin == "kernel":
                    if o.error is not None:
                        line = f"{o.error.ename}: {o.error.evalue}"
                    else:
                        line = o.plain_text().strip().replace("\n", " | ")
                if len(line) > 200:
                    line = line[:197] + "..."
                print(f"{status:<11} {label}{'  ' + line if line else ''}", file=out)
            if record.status not in ("ok",):
                print(
                    f"run {record.status}{': ' + record.reason if record.reason else ''}", file=out
                )
                failed = True
            code = EXIT_FAILED if failed else EXIT_OK
            if saved is not None:
                comparisons = compare(saved_outputs(saved), runtime.outputs, view_cells(runtime))
                for c in comparisons:
                    tag = " (external)" if c.external else ""
                    print(f"compare {c.verdict:<8} {c.label}{tag}", file=out)
                if any(c.counts for c in comparisons):
                    code = EXIT_FAILED
            return code
        finally:
            await engine.close()
    finally:
        shutil.rmtree(data_root, ignore_errors=True)


def saved_outputs(snapshot: SnapshotRead) -> dict[str, CellOutputs]:
    return {c.id: c.outputs for c in snapshot.cells}


def view_cells(runtime: Any) -> list[tuple[str, str, str, str]]:
    """(id, name, kind, code) of the live cells, in order."""
    return [(c.id, c.name, c.kind, c.code) for c in runtime.doc.live_cells()]


def compare(
    saved: Mapping[str, CellOutputs],
    fresh: Mapping[str, CellOutputs],
    cells: Sequence[tuple[str, str, str, str]],
) -> list[CellComparison]:
    out: list[CellComparison] = []
    live = set()
    for index, (cid, name, kind, code) in enumerate(cells):
        live.add(cid)
        external = is_external(kind, code)
        now = fresh.get(cid)
        ran = now is not None and now.origin == "kernel"
        if cid not in saved:
            verdict: Verdict = "new"
        elif not ran:
            verdict = "missing"
        else:
            verdict = "same" if output_hash(saved[cid]) == output_hash(now) else "changed"
        out.append(CellComparison(cid, name, verdict, external, index))
    for index, cid in enumerate(saved):
        if cid not in live:
            out.append(CellComparison(cid, "_", "missing", False, index))
    return out


def _sql_registry(extra: Sequence[SqlEngineProvider]) -> SqlProviderRegistry:
    """Given providers first, then the person's own connections file
    (``~/.config/alkera/connections.toml``), as a standalone engine resolves
    connection names."""
    registry = SqlProviderRegistry(list(extra))
    try:
        from alkera_notebook.sql.providers.local_config import LocalConfigProvider
    except ImportError:
        return registry
    if not any(p.name == "local-config" for p in registry):
        registry.register(LocalConfigProvider())
    return registry


async def _pick_envs(
    python: str | None, root: Path, path: Path, recorded: str | None, out: TextIO
) -> EnvRegistry:
    if python:
        return StaticEnvRegistry(python)
    if recorded:
        from alkera_notebook.envs import LocalCommandRunner, LocalEnvRegistry

        registry = LocalEnvRegistry(
            root, Path(tempfile.gettempdir()) / "alkera-notebook-envs", runner=LocalCommandRunner()
        )
        try:
            env: EnvDescriptor = await registry.resolve(str(path), str(recorded))
        except Exception as exc:  # any resolution failure falls back, visibly
            print(f"env {recorded!r} not usable ({exc}); using {sys.executable}", file=out)
        else:
            if env.state == "ready":
                return registry
            print(f"env {recorded!r} is {env.state}; using {sys.executable}", file=out)
    return StaticEnvRegistry()


def _check(ns: argparse.Namespace, fmt: FormatApi, out: TextIO) -> int:
    path = Path(ns.file)
    if not path.is_file():
        raise UsageError(f"no such notebook: {ns.file}")
    ir = fmt.read(path.read_text(encoding="utf-8"))
    problems = 0
    for v in ir.violations:
        print(f"{path.name}:{v.line}: {v.code}: {v.message}", file=out)
        problems += 1
    cells = [(c.id, c.code) for c in ir.cells]
    analysis = fmt.analyze_code(cells)
    graph = CellGraph.from_analysis([c for c, _ in cells], analysis)
    labels = {c.id: cell_display_name(c.name, index) for index, c in enumerate(ir.cells)}
    for cid in graph.order:
        for err in graph.errors.get(cid, ()):
            print(f"{labels[cid]}: {err.label()}", file=out)
            problems += 1
    for cycle in _cycles(graph):
        print("cycle: " + " -> ".join(labels.get(cid, cid) for cid in cycle), file=out)
        problems += 1
    if problems == 0:
        print(f"{path.name}: {len(ir.cells)} cells, no problems", file=out)
    return EXIT_FAILED if problems else EXIT_OK


def _cycles(graph: CellGraph) -> list[list[str]]:
    """Strongly connected groups of more than one cell (each a cycle)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    found: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in sorted(graph.children.get(v, ())):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group: list[str] = []
            while True:
                w = stack.pop()
                on.discard(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                found.append(sorted(group, key=graph.order.index))

    for v in graph.order:
        if v not in index:
            visit(v)
    return found


def _new_target(ns: argparse.Namespace) -> Path:
    path = Path(ns.file).resolve()
    if path.exists():
        raise UsageError(f"{ns.file} already exists")
    if not path.parent.is_dir():
        raise UsageError(f"no such folder: {path.parent}")
    return path


async def _new(ns: argparse.Namespace, path: Path, fmt: FormatApi, out: TextIO) -> int:
    store = FileDocumentStore(path.parent, fmt=fmt)
    try:
        # The setup block importing the runtime module, then one empty cell.
        cells = [InsertCell(kind="setup", source=fmt.setup_with_runtime("")), InsertCell()]
        await store.create(path.name, cells, {"dataframe": "polars"}, _actor())
    finally:
        result = store.close()
        if asyncio.iscoroutine(result):
            await result
    print(f"created {ns.file}", file=out)
    return EXIT_OK


async def main_async(
    argv: Sequence[str] | None = None,
    *,
    fmt: FormatApi | None = None,
    launcher: KernelLauncher | None = None,
    mount: str | None = None,
    sql: Sequence[SqlEngineProvider] = (),
    envs: EnvRegistry | None = None,
    out: TextIO | None = None,
) -> int:
    stream = out if out is not None else sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    script: list[str] = []
    if "--" in args:
        cut = args.index("--")
        args, script = args[:cut], args[cut + 1 :]
    try:
        ns = _parser().parse_args(args)
        if ns.command is None:
            raise UsageError("a command is required: run, check or new")
        if script and ns.command != "run":
            raise UsageError("arguments after -- are only for run")
        if ns.command == "run" and ns.compare and not ns.clean:
            raise UsageError("--compare needs --clean")
        fmt = fmt or default_format()
        if ns.command == "run":
            return await _run(
                ns,
                _prepare_run(ns, fmt),
                parse_script_args(script),
                launcher=launcher,
                mount=mount,
                sql=sql,
                envs=envs,
                out=stream,
            )
        if ns.command == "check":
            return _check(ns, fmt, stream)
        return await _new(ns, _new_target(ns), fmt, stream)
    except UsageError as exc:
        print(f"alkera-notebook: {exc}", file=stream)
        return EXIT_USAGE


def main(
    argv: Sequence[str] | None = None,
    *,
    fmt: FormatApi | None = None,
    launcher: KernelLauncher | None = None,
    mount: str | None = None,
    sql: Sequence[SqlEngineProvider] = (),
    envs: EnvRegistry | None = None,
    out: TextIO | None = None,
) -> int:
    return asyncio.run(
        main_async(argv, fmt=fmt, launcher=launcher, mount=mount, sql=sql, envs=envs, out=out)
    )


def entry() -> None:
    sys.exit(main())
