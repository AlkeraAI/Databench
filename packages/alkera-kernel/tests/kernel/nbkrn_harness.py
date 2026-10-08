"""A scripted test service that starts real kernels through the local
launcher and drives them over the real RPC, standing in for the engine."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.format import compile_step
from alkera_notebook.kernels.launch_local import (
    LaunchSpec,
    LocalKernel,
    LocalSubprocessLauncher,
    prepare_mount,
)
from alkera_notebook.rpc import (
    Call,
    MethodRegistry,
    RpcService,
    ServiceSession,
    UnixEndpoint,
    new_token,
)

KERNEL_TIMEOUT = 30.0


def step(
    cell_id: str, code: str, *, defs: Iterable[str] | None = None, line_offset: int = 0
) -> dict[str, Any]:
    """A run step the way the engine sends one: compiled by the format's
    ``compile_step`` (the body, and the last expression split off onto the
    cell's own lines), with the cell's source as written."""
    tree = ast.parse(code)
    compiled = compile_step(code, cell_id=cell_id)
    body, last = compiled["body"], compiled["last_expr"] or ""
    names = set(defs or ())
    if defs is None:
        for node2 in ast.walk(tree):
            if isinstance(node2, ast.Name) and isinstance(node2.ctx, ast.Store):
                names.add(node2.id)
            elif isinstance(node2, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node2.name)
            elif isinstance(node2, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).partition(".")[0] for a in node2.names)
    return {
        "cell_id": cell_id,
        "body": body,
        "last_expr": last,
        "source": code,
        "filename": f"/notebooks/demo.alknb.py#{cell_id}",
        "line_offset": line_offset,
        "defs": sorted(names),
        "refs": [],
    }


@dataclass
class RunResult:
    run_id: str
    status: str
    events: list[tuple[str, dict[str, Any]]]

    def of(self, method: str, cell_id: str | None = None) -> list[dict[str, Any]]:
        return [
            p
            for m, p in self.events
            if m == method and (cell_id is None or p.get("cell_id") == cell_id)
        ]

    def finished(self, cell_id: str) -> dict[str, Any]:
        (done,) = self.of("cell.finished", cell_id)
        return done

    def stdout(self, cell_id: str, name: str = "stdout") -> str:
        return "".join(p["text"] for p in self.of("cell.stream", cell_id) if p["name"] == name)

    def outputs(self, cell_id: str) -> list[dict[str, Any]]:
        return [p["output"] for p in self.of("cell.output", cell_id)]


@dataclass
class KernelSession:
    service: RpcService
    session: ServiceSession
    kernel: LocalKernel
    registry: MethodRegistry
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    _background: list[asyncio.Future[Any]] = field(default_factory=list)
    #: Arrival time of each entry of ``events`` (monotonic), after the first run.
    received_at: list[float] = field(default_factory=list)

    async def wait_for(self, predicate: Callable[[], Any], limit_s: float = KERNEL_TIMEOUT) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit_s
        while True:
            value = predicate()
            if value:
                return value
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out; last events: {self.events[-5:]}\nlog: {self.kernel.log_tail(2000)}"
                )
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), min(remaining, 0.5))
            except TimeoutError:
                pass

    async def start_run(
        self, steps: list[dict[str, Any]], *, run_id: str | None = None, clear: Iterable[str] = ()
    ) -> str:
        run_id = run_id or uuid.uuid4().hex[:12]
        self.service.scope.begin(run_id)
        result = await self.session.peer.request(
            "run.execute",
            {"run_id": run_id, "steps": steps, "clear": list(clear), "trigger": "run"},
        )
        assert result == {"accepted": True}
        return run_id

    async def finish(self, run_id: str, limit_s: float = KERNEL_TIMEOUT) -> RunResult:
        def done() -> dict[str, Any] | None:
            for m, p in self.events:
                if m == "run.finished" and p["run_id"] == run_id:
                    return p
            return None

        finished = await self.wait_for(done, limit_s)
        mine = [(m, p) for m, p in self.events if p.get("run_id") == run_id]
        return RunResult(run_id, finished["status"], mine)

    async def run(
        self, *steps: dict[str, Any], clear: Iterable[str] = (), limit_s: float = KERNEL_TIMEOUT
    ) -> RunResult:
        run_id = await self.start_run(list(steps), clear=clear)
        return await self.finish(run_id, limit_s)

    async def run_code(self, code: str, cell_id: str = "c1") -> RunResult:
        return await self.run(step(cell_id, code))

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await self.session.peer.request(method, params or {})

    async def interrupt(self, run_id: str, *, hint: bool = True) -> None:
        import signal

        if hint:
            # Like the engine: send the hint, never wait for its answer (a
            # kernel stuck in C holding the GIL cannot answer), then signal.
            task = asyncio.ensure_future(self.request("run.interrupt", {"run_id": run_id}))
            self._background.append(task)
            await asyncio.sleep(0)
        self.kernel.signal(signal.SIGINT)

    async def close(self) -> None:
        if self.kernel.returncode is None:
            try:
                await asyncio.wait_for(self.request("kernel.shutdown"), 2)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.kernel.wait(), 3)
            except TimeoutError:
                pass
        self.kernel.kill()
        await self.service.close()
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self.service.endpoint.remove()


KernelFactory = Callable[..., Awaitable[KernelSession]]


@pytest.fixture(scope="session")
def kernel_mount(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return prepare_mount(tmp_path_factory.mktemp("mount"))


@pytest.fixture
async def start_kernel(kernel_mount: Path, tmp_path: Path) -> AsyncIterator[KernelFactory]:
    sessions: list[KernelSession] = []

    async def factory(
        *,
        interpreter: str = sys.executable,
        notebook_dir: Path | None = None,
        settings: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        methods: dict[str, Callable[[Call], Awaitable[Any]]] | None = None,
        run_scoped: Iterable[str] = (),
        mount: Path | None = None,
    ) -> KernelSession:
        nb_dir = notebook_dir or tmp_path / "nb"
        nb_dir.mkdir(parents=True, exist_ok=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        registry = MethodRegistry()
        scoped = set(run_scoped)
        for name, handler in (methods or {}).items():
            registry.register(name, handler, run_scoped=name in scoped)
        endpoint = UnixEndpoint.create()
        token = new_token()
        holder: dict[str, KernelSession] = {}

        async def on_event(method: str, params: dict[str, Any]) -> None:
            ks = holder.get("ks")
            if ks is not None:
                ks.events.append((method, params))
                ks.received_at.append(time.monotonic())
                ks.changed.set()
            else:
                early.append((method, params))

        early: list[tuple[str, dict[str, Any]]] = []
        service = RpcService(
            endpoint,
            token=token,
            registry=registry,
            hello_result={"kernel_id": "k-test", "settings": settings or {}},
            on_notification=on_event,
            data_dir=str(data_dir),
        )
        await service.start()
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "LANG": "C.UTF-8",
        }
        venv = Path(interpreter).parent.parent
        if (venv / "pyvenv.cfg").exists():
            base_env["VIRTUAL_ENV"] = str(venv)
        base_env.update(env or {})
        kernel = LocalSubprocessLauncher().launch(
            LaunchSpec(
                interpreter=interpreter,
                notebook_dir=nb_dir,
                kernel_id="k-test",
                endpoint=endpoint.uri,
                token=token,
                mount=mount or kernel_mount,
                env=base_env,
                log_path=tmp_path / f"kernel-{len(sessions)}.log",
            )
        )
        try:
            session = await asyncio.wait_for(service.accept(), KERNEL_TIMEOUT)
        except BaseException:
            kernel.kill()
            log = (tmp_path / f"kernel-{len(sessions)}.log").read_text(errors="replace")
            raise RuntimeError(f"kernel did not connect:\n{log}") from None
        ks = KernelSession(service, session, kernel, registry)
        ks.events.extend(early)
        ks.received_at.extend([0.0] * len(early))
        holder["ks"] = ks
        sessions.append(ks)
        return ks

    yield factory
    for ks in sessions:
        await ks.close()


# --------------------------------------------------------------------------- extra environments

RICH_LIBS = [
    "matplotlib",
    "ipywidgets",
    "marimo",
    "plotly",
    "altair",
    "polars",
    "pandas",
    "pyarrow",
    "joblib",
    "pillow",
    "duckdb",
]


def build_env(cache: Path, name: str, packages: list[str], python: str = "3.13") -> str | None:
    """A cached virtual environment with ``packages`` (from uv's cache when
    possible). None when it cannot be built here (no uv, no network).

    Every xdist worker asks for the same environments at once, and a fresh
    cache (a CI runner's) has none: one worker builds under a lock while the
    others wait and take its build, so no worker removes or reads an
    environment another is still building."""
    uv = shutil.which("uv")
    if uv is None:
        return None
    root = cache / name
    if not _take_build_lock(cache / f"{name}.building", root / ".complete"):
        return str(root / "bin" / "python")
    try:
        return _build_env_locked(uv, root, packages, python)
    finally:
        os.rmdir(cache / f"{name}.building")


#: A build lock older than this was left by a run that was killed mid-build.
_STALE_BUILD_S = 20 * 60


def _take_build_lock(lock: Path, built: Path) -> bool:
    """Take the build of one environment (a directory made atomically on
    every platform), or wait for the worker holding it. False when that
    worker finished the build while this one waited."""
    while True:
        try:
            lock.mkdir()
            return True
        except FileExistsError:
            pass
        if built.exists():
            return False
        try:
            if time.time() - lock.stat().st_mtime > _STALE_BUILD_S:
                os.rmdir(lock)
                continue
        except FileNotFoundError:
            continue
        time.sleep(1)


def _build_env_locked(uv: str, root: Path, packages: list[str], python: str) -> str | None:
    py = root / "bin" / "python"
    marker = root / ".complete"
    if marker.exists():
        return str(py)
    shutil.rmtree(root, ignore_errors=True)
    if subprocess.run([uv, "venv", "-q", "-p", python, str(root)], check=False).returncode != 0:
        return None
    env = {**os.environ, "VIRTUAL_ENV": str(root)}
    if packages:
        install = [uv, "pip", "install", "-q", *packages]
        if (
            subprocess.run(
                [*install, "--offline"], env=env, check=False, capture_output=True
            ).returncode
            != 0
        ):
            if subprocess.run(install, env=env, check=False, capture_output=True).returncode != 0:
                return None
    # Build matplotlib's font cache now, not inside the first test's kernel.
    subprocess.run([str(py), "-c", "import matplotlib.pyplot"], check=False, capture_output=True)
    marker.write_text("ok")
    return str(py)


#: Every cached environment a fixture asks for, by the fixture that asks:
#: ``{fixture name: [(environment name, packages), ...]}``. A fixture that builds
#: an environment registers it here so ``prebuild_envs`` builds it before the
#: first test runs.
TEST_ENVS: dict[str, list[tuple[str, list[str]]]] = {
    "rich_python": [("rich-3.13", RICH_LIBS)],
    "library_python": [("pandas-only-3.13", ["pandas"]), ("polars-only-3.13", ["polars"])],
}


def env_cache(config: pytest.Config) -> Path:
    """Where the environments are cached: pytest's cache folder, or, when a run
    turns the cache plugin off (``-p no:cacheprovider``), a folder in the
    system temp directory named for this checkout. Either way every xdist
    worker of the run sees the same folder, so one builds and the rest wait."""
    cache = getattr(config, "cache", None)
    if cache is not None:
        return Path(cache.mkdir("nbkrn-envs"))
    checkout = hashlib.sha256(str(config.rootpath).encode()).hexdigest()[:12]
    root = Path(tempfile.gettempdir()) / f"alkera-nbkrn-envs-{checkout}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cached_env(config: pytest.Config, name: str) -> str | None:
    """The cached environment ``name`` (one of ``TEST_ENVS``), built if needed."""
    packages = {env: libs for specs in TEST_ENVS.values() for env, libs in specs}[name]
    return build_env(env_cache(config), name, packages)


def prebuild_envs(session: pytest.Session) -> None:
    """Build every environment the selected tests ask for, once collection is
    done and before any test runs.

    A build downloads and installs a dozen libraries, and under a loaded run it
    takes longer than the per-test timeout. Built inside the first test's
    fixture setup, the timeout ends it, and every test on that worker that
    shares the session fixture errors with the same cached failure (as do the
    workers waiting on the build lock). Here the build sits outside every
    test's budget; the fixtures then find it finished. The kernel tests are
    POSIX-only, so Windows keeps the lazy build in the fixture."""
    if sys.platform == "win32":
        return
    wanted = sorted(
        {
            env
            for item in session.items
            for fixture in getattr(item, "fixturenames", ())
            for env, _packages in TEST_ENVS.get(fixture, ())
        }
    )
    for env in wanted:
        cached_env(session.config, env)


@pytest.fixture(scope="session")
def rich_python(request: pytest.FixtureRequest) -> str:
    py = cached_env(request.config, "rich-3.13")
    if py is None:
        pytest.skip("could not build the display-library environment")
    return py


class PageTable:
    """An ``inspect.frame`` answer's table page (``{schema, rows, total_rows,
    offset}``), read by column."""

    def __init__(self, page: Any) -> None:
        assert set(page) == {"schema", "rows", "total_rows", "offset"}, page
        self.columns = [(c["name"], c["type"]) for c in page["schema"]]
        self.names = [name for name, _ in self.columns]
        self.rows = [list(row) for row in page["rows"]]
        self.total_rows = page["total_rows"]
        self.offset = page["offset"]
