"""Both implementations of the RPC framing (the reference in
``alkera_notebook.rpc.frames`` and the kernel's generated copy) replay the
shared conformance vectors, in this process and under every other interpreter
the kernel supports that is installed here."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HERE = Path(__file__).resolve().parent
VECTORS = HERE / "rpc_vectors"
PACKAGES = HERE.parents[1]
REFERENCE = PACKAGES / "alkera-notebook" / "alkera_notebook" / "rpc" / "frames.py"
KERNEL_COPY = PACKAGES / "alkera-kernel" / "_alkera_kernel" / "_frames.py"
GENERATOR = PACKAGES / "alkera-kernel" / "scripts" / "gen_kernel_rpc.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


replay = _load(VECTORS / "replay.py", "_rpc_vector_replay")
generator = _load(GENERATOR, "_gen_kernel_rpc")
IMPLEMENTATIONS = [
    pytest.param(REFERENCE, id="reference"),
    pytest.param(KERNEL_COPY, id="kernel-copy"),
]
VECTOR_NAMES = [v["name"] for v in replay.load_vectors()]


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
@pytest.mark.parametrize("name", VECTOR_NAMES)
def test_rpc_vector(impl: Path, name: str) -> None:
    fm = replay.load_frames(str(impl))
    (vector,) = [v for v in replay.load_vectors() if v["name"] == name]
    assert replay.check(vector, fm) is None


def test_rpc_vectors_cover_every_error_reason_and_tag() -> None:
    vectors = replay.load_vectors()
    reasons = {v["reason"] for v in vectors if not v["valid"]}
    assert reasons >= {
        "truncated",
        "too_large",
        "length_mismatch",
        "bad_header",
        "header_not_object",
        "bad_segments",
        "invalid_message",
    }
    tags: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            if isinstance(obj.get("$t"), str):
                tags.add(obj["$t"])
            for item in obj.values():
                walk(item)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for v in vectors:
        data = bytes.fromhex(v["hex"])
        if v["valid"]:
            (hlen,) = struct.unpack_from(">I", data, 4)
            walk(json.loads(data[8 : 8 + hlen]))
    assert tags >= {
        "decimal",
        "datetime",
        "date",
        "time",
        "timedelta",
        "bytes",
        "float",
        "uuid",
        "int",
        "object",
    }


def _interpreters() -> list[object]:
    found = []
    for name in (
        "python3.10",
        "python3.11",
        "python3.12",
        "python3.13",
        "python3.14",
        "python3.14t",
    ):
        exe = shutil.which(name) or shutil.which(name, path=str(Path.home() / ".local" / "bin"))
        if exe:
            found.append(pytest.param(exe, id=name))
    return found or [
        pytest.param(None, id="none-installed", marks=pytest.mark.skip("no other interpreter"))
    ]


@pytest.mark.parametrize("interpreter", _interpreters())
def test_rpc_kernel_copy_replays_under_interpreter(interpreter: str) -> None:
    done = subprocess.run(
        [interpreter, "-I", str(VECTORS / "replay.py"), str(KERNEL_COPY)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_rpc_kernel_copy_is_current() -> None:
    assert generator.main(["--check"]) == 0, "run make gen-kernel-rpc"
    assert KERNEL_COPY.read_text(encoding="utf-8") == generator.render(
        REFERENCE.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import msgspec\n", id="third-party"),
        pytest.param("from pydantic import BaseModel\n", id="from-third-party"),
        pytest.param("from . import peer\n", id="relative"),
        pytest.param("import alkera_notebook.rpc\n", id="own-package"),
    ],
)
def test_rpc_generator_refuses_non_stdlib_imports(source: str) -> None:
    with pytest.raises(SystemExit, match="only the standard library"):
        generator.render("from __future__ import annotations\nimport json\n" + source)


def test_rpc_generator_admits_stdlib_and_rejects_newer_syntax() -> None:
    assert generator.render("import json, struct\n").endswith("import json, struct\n")
    with pytest.raises(SyntaxError):
        generator.render("type Alias = int\n")  # 3.12 syntax cannot load in 3.10 kernels


def test_rpc_reference_imports_only_stdlib() -> None:
    tree = ast.parse(REFERENCE.read_text(encoding="utf-8"))
    assert generator.non_stdlib_imports(ast.unparse(tree)) == []
