"""Prove the Python SDK works against the open backend.

The committed ``alkera_sdk._generated`` tree is built from the product document.
This check builds the client again from the open document
(``packages/shared-openapi/open/openapi.json``) into a temporary directory and
holds the hand-written wrapper (``packages/py-sdk/alkera_sdk/client.py``) to it:

* ``openapi-python-client`` generates from the open document without error;
* every generated operation and model the wrapper imports exists in that tree;
* every raw route the wrapper calls (the Files surface it drives with httpx)
  is a path the open document serves.

``make gen-open-subset`` runs it. A drift test of the open subset can run the
document half (``missing_in_document``) on every test run.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from open_subset import OPEN_DIR, REPO_ROOT

WRAPPER = REPO_ROOT / "packages" / "py-sdk" / "alkera_sdk" / "client.py"
CONFIG = REPO_ROOT / "packages" / "py-sdk" / "openapi-python-client.yaml"
GENERATED = "alkera_sdk._generated"
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


@dataclass(frozen=True)
class WrapperReferences:
    """What the wrapper needs from the API document."""

    #: (tag package, operation function) pairs imported from ``_generated.api``.
    operations: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    #: Module names imported from ``_generated.models``.
    models: frozenset[str] = field(default_factory=frozenset)
    #: Raw path templates with every parameter written ``{}``.
    raw_paths: frozenset[str] = field(default_factory=frozenset)


def _template(node: ast.JoinedStr, constants: Mapping[str, str]) -> str | None:
    """``f"{_FILES}/drives/{drive_id}"`` -> ``/api/v1/files/drives/{}``, when it
    starts with a module constant naming an API prefix."""
    head, *rest = node.values
    if not (
        isinstance(head, ast.FormattedValue)
        and isinstance(head.value, ast.Name)
        and head.value.id in constants
    ):
        return None
    parts = [constants[head.value.id]]
    for value in rest:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
        else:
            parts.append("{}")
    return "".join(parts)


def wrapper_references(source: str) -> WrapperReferences:
    tree = ast.parse(source)
    operations: set[tuple[str, str]] = set()
    models: set[str] = set()
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{GENERATED}.api."):
                tag = node.module.removeprefix(f"{GENERATED}.api.")
                operations.update((tag, alias.name) for alias in node.names)
            elif node.module.startswith(f"{GENERATED}.models."):
                models.add(node.module.removeprefix(f"{GENERATED}.models."))
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if (
            isinstance(target, ast.Name)
            and isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value.startswith("/api/")
        ):
            constants[target.id] = node.value.value
    raw_paths = {
        template
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr) and (template := _template(node, constants)) is not None
    }
    return WrapperReferences(frozenset(operations), frozenset(models), frozenset(raw_paths))


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _normalized(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


def missing_in_document(refs: WrapperReferences, document: Mapping[str, Any]) -> list[str]:
    """Everything the wrapper uses that ``document`` does not define."""
    served: set[tuple[str, str]] = set()
    for item in document.get("paths", {}).values():
        for verb, operation in item.items():
            if verb not in HTTP_METHODS:
                continue
            for tag in operation.get("tags") or ["default"]:
                served.add((_snake(tag).replace(" ", "_"), operation["operationId"]))
    schemas = {_snake(name) for name in document.get("components", {}).get("schemas", {})}
    paths = {_normalized(path) for path in document.get("paths", {})}
    missing = [
        f"operation {tag}.{name}"
        for tag, name in sorted(refs.operations)
        if (tag, name) not in served
    ]
    missing += [f"model {name}" for name in sorted(refs.models) if name not in schemas]
    missing += [f"path {path}" for path in sorted(refs.raw_paths) if path not in paths]
    return missing


def missing_in_tree(refs: WrapperReferences, package: Path) -> list[str]:
    """Everything the wrapper imports that the generated package lacks."""
    missing = [
        f"operation {tag}.{name}"
        for tag, name in sorted(refs.operations)
        if not (package / "api" / tag / f"{name}.py").is_file()
    ]
    missing += [
        f"model {name}"
        for name in sorted(refs.models)
        if not (package / "models" / f"{name}.py").is_file()
    ]
    return missing


def _generate(document: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv over this interpreter and repo paths, no shell
        [
            sys.executable,
            "-m",
            "openapi_python_client",
            "generate",
            "--path",
            str(document),
            "--output-path",
            str(out),
            "--config",
            str(CONFIG),
            "--meta",
            "none",
            "--overwrite",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _report(problems: Iterable[str]) -> int:
    listed = list(problems)
    for problem in listed:
        sys.stderr.write(f"  {problem}\n")
    return 1 if listed else 0


def main() -> int:
    document_path = OPEN_DIR / "openapi.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    refs = wrapper_references(WRAPPER.read_text(encoding="utf-8"))

    missing = missing_in_document(refs, document)
    if missing:
        sys.stderr.write("the SDK wrapper uses what the open document does not serve:\n")
        return _report(missing)

    with tempfile.TemporaryDirectory(prefix="alkera-open-py-sdk-") as tmp:
        package = Path(tmp) / "_generated"
        result = _generate(document_path, package)
        if result.returncode != 0:
            sys.stderr.write(result.stdout[-4000:] + result.stderr[-4000:])
            sys.stderr.write("openapi-python-client failed on the open document\n")
            return 1
        missing = missing_in_tree(refs, package)
        if missing:
            sys.stderr.write("the client generated from the open document lacks:\n")
            return _report(missing)
        generated = sum(1 for _ in (package / "api").rglob("*.py"))

    sys.stderr.write(
        f"open py-sdk ok: {generated} generated api modules; the wrapper's "
        f"{len(refs.operations)} operations, {len(refs.models)} models and "
        f"{len(refs.raw_paths)} raw paths are all open\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
