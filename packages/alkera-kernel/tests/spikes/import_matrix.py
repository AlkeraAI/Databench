"""The runtime import matrix: start real kernels from ``_alkera_kernel`` in
fresh environments and run the 20-cell fixture notebook in each.

Axes: interpreter (3.10 to 3.14, and 3.14t), environment tool (``uv venv``,
``python -m venv``, a conda env made by micromamba) and installed libraries
(none; pandas; polars; pandas plus pyarrow; a stock marimo older than the
one the platform tracks). Per environment it records the kernel start time
(launch to hello), the runtime's import time measured in a separate process,
the launcher-measured RSS after start, and each fixture cell's result.

Run from the repository root::

    uv run --frozen python packages/alkera-kernel/tests/spikes/import_matrix.py --out results.json

Environments are cached under ``--cache`` (outside the repository). A spike
script, not part of the suite.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kernel"))
sys.path.insert(0, str(HERE))

from alkera_notebook.kernels.launch_local import (  # noqa: E402
    LaunchSpec,
    LocalSubprocessLauncher,
    kernel_files,
)
from alkera_notebook.rpc import MethodRegistry, RpcService, UnixEndpoint, new_token  # noqa: E402
from fixture_cells import CELLS, Cell  # noqa: E402
from nbkrn_harness import KernelSession, step  # noqa: E402

PYTHONS = ["3.10", "3.11", "3.12", "3.13", "3.14", "3.14t"]
TOOLS = ["uv", "venv", "conda"]
CONFIGS: dict[str, dict[str, list[str]]] = {
    # pip requirement strings, and the conda-forge equivalents
    "none": {"pip": [], "conda": []},
    "pandas": {"pip": ["pandas"], "conda": ["pandas"]},
    "polars": {"pip": ["polars"], "conda": ["polars"]},
    "pandas+pyarrow": {"pip": ["pandas", "pyarrow"], "conda": ["pandas", "pyarrow"]},
    "marimo-0.20": {"pip": ["marimo>=0.20,<0.21"], "conda": ["marimo>=0.20,<0.21"]},
}
PROBE_DISTS = ["pandas", "polars", "pyarrow", "numpy", "marimo"]
START_SAMPLES = 3
IMPORT_SAMPLES = 5
TIMEOUT = 60.0


@dataclass
class Env:
    python: str
    tool: str
    config: str
    root: Path

    @property
    def name(self) -> str:
        return f"{self.tool}-py{self.python}-{self.config}"

    @property
    def interpreter(self) -> Path:
        return self.root / "bin" / "python"


# --------------------------------------------------------------------------- building environments


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


def _tail(done: subprocess.CompletedProcess[str]) -> str:
    return ((done.stderr or "") + (done.stdout or ""))[-1500:].strip()


def base_interpreter(python: str) -> str | None:
    """A uv-managed base interpreter. ``--system`` keeps uv from answering
    with the virtual environment this script runs in, and ``3.14`` asks for
    the GIL build explicitly (uv otherwise prefers a newer free-threaded one)."""
    request = f"{python}+gil" if python == "3.14" else python
    done = _run(["uv", "python", "find", "--system", "--managed-python", request])
    return done.stdout.strip() or None


def build(env: Env, micromamba: Path | None) -> str | None:
    """Build ``env`` (cached by a marker file). Returns an error, or None."""
    marker = env.root / ".complete"
    if marker.exists():
        return None
    shutil.rmtree(env.root, ignore_errors=True)
    env.root.parent.mkdir(parents=True, exist_ok=True)
    pkgs = CONFIGS[env.config]
    if env.tool == "conda":
        if micromamba is None:
            return "micromamba not available"
        spec = [f"python={env.python.rstrip('t')}"]
        if env.python.endswith("t"):
            spec.append("python-freethreading")
        cmd = [
            str(micromamba),
            "create",
            "-y",
            "-p",
            str(env.root),
            "-c",
            "conda-forge",
            *spec,
            *pkgs["conda"],
        ]
        mm_env = {**os.environ, "MAMBA_ROOT_PREFIX": str(micromamba.parent.parent / "root")}
        done = _run([*cmd, "--offline"], mm_env)
        if done.returncode != 0:
            done = _run(cmd, mm_env)
        if done.returncode != 0:
            shutil.rmtree(env.root, ignore_errors=True)
            return "conda create failed: " + _tail(done)
    else:
        base = base_interpreter(env.python)
        if base is None:
            return f"no interpreter for {env.python}"
        if env.tool == "uv":
            done = _run(["uv", "venv", "-q", "--python", base, str(env.root)])
        else:
            done = _run([base, "-m", "venv", str(env.root)])
        if done.returncode != 0:
            return f"{env.tool} create failed: " + _tail(done)
        if pkgs["pip"]:
            install = ["uv", "pip", "install", "-q", "--python", str(env.interpreter), *pkgs["pip"]]
            done = _run([*install, "--offline"])
            if done.returncode != 0:
                done = _run(install)
            if done.returncode != 0:
                shutil.rmtree(env.root, ignore_errors=True)
                return "install failed: " + _tail(done)
    marker.write_text("ok")
    return None


def get_micromamba(cache: Path) -> Path | None:
    exe = cache / "mamba" / "bin" / "micromamba"
    if exe.exists():
        return exe
    found = shutil.which("micromamba")
    return Path(found) if found else None


def probe(env: Env) -> dict[str, Any]:
    code = (
        "import json, sys, sysconfig\n"
        "from importlib import metadata\n"
        "gil = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))\n"
        "out = {'version': sys.version.split()[0], 'gil_disabled': gil}\n"
        f"for d in {PROBE_DISTS!r}:\n"
        "    try:\n"
        "        out[d] = metadata.version(d)\n"
        "    except metadata.PackageNotFoundError:\n"
        "        pass\n"
        "print(json.dumps(out))\n"
    )
    done = _run([str(env.interpreter), "-c", code])
    return json.loads(done.stdout) if done.returncode == 0 else {"probe_error": _tail(done)}


# --------------------------------------------------------------------------- the mount


def make_mount(directory: Path) -> Path:
    """A copied (not symlinked) kernel mount, so per-version bytecode lands
    outside the repository and a cold import can clear it."""
    files = kernel_files()
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    shutil.copy2(files.boot, directory / "boot.py")
    shutil.copytree(
        files.package,
        directory / "_alkera_kernel",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    alkera = importlib.util.find_spec("alkera")
    if alkera is not None and alkera.origin is not None:
        shutil.copytree(
            Path(alkera.origin).parent,
            directory / "public" / "alkera",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    return directory


def clear_bytecode(mount: Path) -> None:
    for cache in mount.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def kernel_env(env: Env, home: Path) -> dict[str, str]:
    out = {
        "PATH": f"{env.root / 'bin'}:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
    }
    out["CONDA_PREFIX" if env.tool == "conda" else "VIRTUAL_ENV"] = str(env.root)
    return out


IMPORT_CODE = """
import time
t0 = time.perf_counter()
import site
site.main()
t1 = time.perf_counter()
import importlib, importlib.util, json, os, sys
before = set(sys.modules)
init = os.path.join({mount!r}, "_alkera_kernel", "__init__.py")
spec = importlib.util.spec_from_file_location(
    "_alkera_kernel_v0_0_0", init, submodule_search_locations=[os.path.dirname(init)]
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
importlib.import_module(spec.name + ".connection")
importlib.import_module(spec.name + ".runtime")
t2 = time.perf_counter()
new = len(set(sys.modules) - before)
print(json.dumps({{"site_ms": (t1 - t0) * 1e3, "runtime_ms": (t2 - t1) * 1e3, "new_modules": new}}))
"""


def measure_import(env: Env, mount: Path, home: Path) -> dict[str, Any]:
    """The runtime's import time in a separate process, with the launcher's
    interpreter flags: cold (bytecode cleared) once, then warm samples."""
    home.mkdir(parents=True, exist_ok=True)
    code = IMPORT_CODE.format(mount=str(mount))
    cmd = [str(env.interpreter), "-s", "-S", "-X", "utf8", "-c", code]
    clear_bytecode(mount)
    samples: list[dict[str, Any]] = []
    for _ in range(IMPORT_SAMPLES + 1):
        done = subprocess.run(
            cmd,
            env=kernel_env(env, home),
            capture_output=True,
            text=True,
            check=False,
            cwd=str(home),
        )
        if done.returncode != 0:
            return {"error": _tail(done)}
        samples.append(json.loads(done.stdout.strip().splitlines()[-1]))
    warm = samples[1:]
    return {
        "cold_runtime_ms": round(samples[0]["runtime_ms"], 1),
        "warm_runtime_ms": round(statistics.median(s["runtime_ms"] for s in warm), 1),
        "warm_site_ms": round(statistics.median(s["site_ms"] for s in warm), 1),
        "new_modules": samples[0]["new_modules"],
    }


# --------------------------------------------------------------------------- kernels


async def start(env: Env, mount: Path, work: Path) -> tuple[KernelSession | None, float, str]:
    """Start a kernel; returns the session (None on failure), the seconds
    from launch to hello, and the kernel log."""
    nb_dir = work / "nb"
    nb_dir.mkdir(parents=True, exist_ok=True)
    data_dir = work / "data"
    data_dir.mkdir(exist_ok=True)
    events: list[tuple[str, dict[str, Any]]] = []
    holder: dict[str, KernelSession] = {}

    async def on_event(method: str, params: dict[str, Any]) -> None:
        ks = holder.get("ks")
        if ks is None:
            events.append((method, params))
        else:
            ks.events.append((method, params))
            ks.changed.set()

    endpoint = UnixEndpoint.create()
    token = new_token()
    registry = MethodRegistry()
    service = RpcService(
        endpoint,
        token=token,
        registry=registry,
        hello_result={"kernel_id": "k-matrix", "settings": {}},
        on_notification=on_event,
        data_dir=str(data_dir),
    )
    await service.start()
    log_path = work / f"kernel-{uuid.uuid4().hex[:6]}.log"
    t0 = time.perf_counter()
    kernel = LocalSubprocessLauncher().launch(
        LaunchSpec(
            interpreter=str(env.interpreter),
            notebook_dir=nb_dir,
            kernel_id="k-matrix",
            endpoint=endpoint.uri,
            token=token,
            mount=mount,
            env=kernel_env(env, work),
            log_path=log_path,
        )
    )
    try:
        session = await asyncio.wait_for(service.accept(), TIMEOUT)
    except BaseException:
        elapsed = time.perf_counter() - t0
        await asyncio.sleep(0.2)
        log = kernel.log_tail(8000)
        kernel.kill()
        await service.close()
        endpoint.remove()
        return None, elapsed, log
    elapsed = time.perf_counter() - t0
    ks = KernelSession(service, session, kernel, registry)
    ks.events.extend(events)
    holder["ks"] = ks
    return ks, elapsed, ""


def available(env_probe: dict[str, Any], cell: Cell) -> bool:
    return all(lib in env_probe for lib in cell.requires)


def check(cell: Cell, stdout: str, stderr: str, plain: str) -> list[str]:
    problems = []
    for key, got in (("stdout", stdout), ("stderr", stderr), ("plain", plain)):
        want = cell.expect.get(key)
        if want is not None and want not in got:
            problems.append(f"{key} missing {want!r}: got {got[:200]!r}")
    return problems


async def run_fixture(ks: KernelSession, env_probe: dict[str, Any]) -> list[dict[str, Any]]:
    """Each cell as its own run (so one failure does not hide the rest),
    in order, in one kernel."""
    results = []
    for cell in CELLS:
        if not available(env_probe, cell):
            results.append({"cell": cell.cell_id, "result": "skipped"})
            continue
        try:
            res = await ks.run(step(cell.cell_id, cell.source), timeout=TIMEOUT)
        except TimeoutError as exc:
            results.append({"cell": cell.cell_id, "result": "timeout", "detail": str(exc)[-2000:]})
            continue
        stdout = res.stdout(cell.cell_id)
        stderr = res.stdout(cell.cell_id, "stderr")
        plain = "".join(str(o.get("text/plain", "")) for o in res.outputs(cell.cell_id))
        finished = res.of("cell.finished", cell.cell_id)
        status = finished[0]["status"] if finished else res.status
        entry: dict[str, Any] = {"cell": cell.cell_id, "status": status}
        if status != "ok":
            entry["result"] = "failed"
            entry["detail"] = json.dumps(finished[0].get("error") if finished else res.events[-5:])[
                -3000:
            ]
        else:
            problems = check(cell, stdout, stderr, plain)
            entry["result"] = "failed" if problems else "ok"
            if problems:
                entry["detail"] = "; ".join(problems)
        results.append(entry)
    return results


async def measure_env(env: Env, mount: Path, work: Path) -> dict[str, Any]:
    row: dict[str, Any] = {
        "env": env.name,
        "python": env.python,
        "tool": env.tool,
        "config": env.config,
    }
    env_probe = probe(env)
    row["probe"] = env_probe
    row["import"] = measure_import(env, mount, work)
    starts: list[float] = []
    rss: list[int] = []
    for i in range(START_SAMPLES):
        ks, elapsed, log = await start(env, mount, work / f"k{i}")
        if ks is None:
            row["start_error"] = log
            row["start_s"] = round(elapsed, 3)
            return row
        starts.append(elapsed)
        rss.append(ks.kernel.rss_bytes())
        if i == 0:
            row["cells"] = await run_fixture(ks, env_probe)
            row["rss_after_cells_mb"] = round(ks.kernel.rss_bytes() / 2**20, 1)
            row["kernel_log_tail"] = ks.kernel.log_tail(4000)
        await ks.close()
    row["start_first_s"] = round(starts[0], 3)
    row["start_median_s"] = round(statistics.median(starts), 3)
    row["rss_after_start_mb"] = round(statistics.median(rss) / 2**20, 1)
    return row


# --------------------------------------------------------------------------- report


def summarize(row: dict[str, Any]) -> tuple[int, int, int]:
    cells = row.get("cells", [])
    ok = sum(1 for c in cells if c["result"] == "ok")
    skipped = sum(1 for c in cells if c["result"] == "skipped")
    return ok, skipped, len(cells) - ok - skipped


HEADER = [
    "Python",
    "Tool",
    "Libraries",
    "Versions",
    "Start (first / median s)",
    "Runtime import (cold / warm ms)",
    "RSS after start (MB)",
    "Cells ok / skipped / failed",
    "Notes",
]


def table_row(r: dict[str, Any]) -> list[str]:
    probe_ = r.get("probe", {})
    python = str(probe_.get("version", r["python"]))
    if probe_.get("gil_disabled"):
        python += " (free-threaded)"
    head = [python, r["tool"], r["config"]]
    if "build_error" in r:
        return [*head, "-", "-", "-", "-", "not built", r["build_error"][:160]]
    shown = [d for d in PROBE_DISTS if d in probe_ and d != "numpy"]
    versions = ", ".join(f"{d} {probe_[d]}" for d in shown) or "-"
    imp = r.get("import", {})
    imp_s = "error" if "error" in imp else f"{imp['cold_runtime_ms']} / {imp['warm_runtime_ms']}"
    if "start_error" in r:
        return [*head, versions, "failed", imp_s, "-", "-", "kernel did not start"]
    ok, skipped, failed = summarize(r)
    notes = "; ".join(
        f"{c['cell']}: {c.get('detail', c['result'])[:120]}"
        for c in r["cells"]
        if c["result"] not in ("ok", "skipped")
    )
    return [
        *head,
        versions,
        f"{r['start_first_s']} / {r['start_median_s']}",
        imp_s,
        str(r["rss_after_start_mb"]),
        f"{ok} / {skipped} / {failed}",
        notes or "-",
    ]


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [HEADER, ["---"] * len(HEADER), *(table_row(r) for r in rows)]
    return "".join("| " + " | ".join(cells).replace("\n", " ") + " |\n" for cells in lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "alkera-nbkrn-s2")
    parser.add_argument("--python", action="append", help="limit to these interpreters")
    parser.add_argument("--tool", action="append", help="limit to these environment tools")
    parser.add_argument("--config", action="append", help="limit to these library sets")
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    cache: Path = args.cache
    micromamba = get_micromamba(cache)
    envs = [
        Env(p, t, c, cache / "envs" / f"{t}-py{p}-{c}")
        for p in (args.python or PYTHONS)
        for t in (args.tool or TOOLS)
        for c in (args.config or list(CONFIGS))
    ]
    mount = None if args.build_only else make_mount(cache / "mount")
    rows: list[dict[str, Any]] = []
    for env in envs:
        t0 = time.perf_counter()
        error = build(env, micromamba)
        print(f"[build] {env.name}: {error or 'ok'} ({time.perf_counter() - t0:.1f}s)", flush=True)
        if args.build_only:
            continue
        if error:
            rows.append(
                {
                    "env": env.name,
                    "python": env.python,
                    "tool": env.tool,
                    "config": env.config,
                    "build_error": error,
                }
            )
            continue
        assert mount is not None
        row = await measure_env(env, mount, cache / "work" / env.name)
        ok, skipped, failed = summarize(row)
        print(
            f"[run] {env.name}: start={row.get('start_median_s')} "
            f"rss={row.get('rss_after_start_mb')}MB cells ok={ok} skipped={skipped} "
            f"failed={failed}{' START FAILED' if 'start_error' in row else ''}",
            flush=True,
        )
        rows.append(row)
    if args.build_only:
        return
    report = {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "engine_python": sys.version.split()[0],
        },
        "rows": rows,
    }
    args.out.write_text(json.dumps(report, indent=2))
    args.out.with_suffix(".md").write_text(markdown(rows))
    print(f"wrote {args.out} and {args.out.with_suffix('.md')}")


if __name__ == "__main__":
    asyncio.run(main())
