"""Import hygiene: the bucket-wide handle stays out of the request path.

Two rules, both mechanical:

* ``ScopedStoreFactory.admin()`` is called only by the janitor, scrub, fsck,
  backup and exit-drill modules — the roles that legitimately cross domains —
  and only at the package root: the allowlist is a set of relative paths, so a
  ``store/gc.py`` does not inherit the exemption by reusing a filename.
* No module under ``alkera_core/files/`` outside ``store/`` annotates a
  parameter or an attribute as ``ObjectStore``; a request path holds a
  ``DomainStore``, so widening the type is a visible edit, not a slip.

Both scanners are exercised against a module that breaks the rule, so the
scan cannot pass by looking at nothing.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import alkera_core.files as files_package
from alkera_core.files.store.protocol import ScopedCredentials
from alkera_core.files.store.s3_compatible import S3Config

FILES_ROOT: Final = Path(files_package.__file__).parent

#: Relative to ``alkera_core/files/``. Paths, not bare filenames: the crossing
#: roles are five specific modules at the package root, and a same-named module
#: in a subpackage is a different module with no claim on the admin handle.
ADMIN_CALLERS_ALLOWED: Final = frozenset(
    {
        Path("gc.py"),
        Path("scrub.py"),
        Path("fsck.py"),
        Path("backup.py"),
        Path("exit_drill.py"),
    }
)


def _modules(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def admin_call_offenders(root: Path) -> list[str]:
    """Modules outside the allowed roles that call ``<something>.admin()``."""
    offenders: list[str] = []
    for module in _modules(root):
        relative = module.relative_to(root)
        if relative in ADMIN_CALLERS_ALLOWED:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "admin"
            ):
                offenders.append(f"{relative.as_posix()}:{node.lineno}")
    return offenders


def object_store_annotation_offenders(root: Path, *, exempt_dir: str = "store") -> list[str]:
    """Modules outside ``store/`` that annotate a parameter or attribute ``ObjectStore``."""
    offenders: list[str] = []
    for module in _modules(root):
        if exempt_dir in module.relative_to(root).parts[:-1]:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        annotations: list[ast.expr] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.annotation is not None:
                annotations.append(node.annotation)
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
        for annotation in annotations:
            names = {sub.id for sub in ast.walk(annotation) if isinstance(sub, ast.Name)} | {
                sub.attr for sub in ast.walk(annotation) if isinstance(sub, ast.Attribute)
            }
            if "ObjectStore" in names:
                offenders.append(f"{module.name}:{annotation.lineno}")
    return offenders


def test_no_module_in_files_calls_the_admin_handle_outside_the_crossing_roles() -> None:
    assert admin_call_offenders(FILES_ROOT) == []


def test_no_request_path_module_annotates_the_bucket_wide_store() -> None:
    assert object_store_annotation_offenders(FILES_ROOT) == []


def test_the_admin_scan_flags_a_module_that_reaches_for_the_bucket_wide_handle(
    tmp_path: Path,
) -> None:
    (tmp_path / "thumbnails.py").write_text(
        "def render(factory):\n    return factory.admin().head('objects/aa/bb/cc')\n",
        encoding="utf-8",
    )
    (tmp_path / "gc.py").write_text(
        "def sweep(factory):\n    return factory.admin().list_prefix('deleted/')\n",
        encoding="utf-8",
    )

    assert admin_call_offenders(tmp_path) == ["thumbnails.py:2"]


def test_the_annotation_scan_flags_a_request_path_that_widens_to_object_store(
    tmp_path: Path,
) -> None:
    (tmp_path / "versions.py").write_text(
        "from alkera_core.files.store.protocol import ObjectStore\n"
        "\n"
        "class Service:\n"
        "    store: ObjectStore\n"
        "\n"
        "def read(store: ObjectStore) -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "driver.py").write_text(
        "from alkera_core.files.store.protocol import ObjectStore\n"
        "\n"
        "def wrap(inner: ObjectStore) -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )

    assert object_store_annotation_offenders(tmp_path) == ["versions.py:4", "versions.py:6"]


def test_the_allowlist_is_a_path_not_a_filename(tmp_path: Path) -> None:
    """A module named `gc.py` in a subpackage does not inherit the exemption.

    A bare-filename allowlist would let any module under `files/` take the
    bucket-wide handle by choosing one of five names — the store package being
    the most tempting place for it.
    """
    (tmp_path / "gc.py").write_text(
        "def sweep(factory):\n    return factory.admin().list_prefix('deleted/')\n",
        encoding="utf-8",
    )
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "gc.py").write_text(
        "def sweep(factory):\n    return factory.admin().list_prefix('deleted/')\n",
        encoding="utf-8",
    )
    (store_dir / "scrub.py").write_text(
        "def scan(factory):\n    return factory.admin().head('objects/aa')\n",
        encoding="utf-8",
    )

    assert admin_call_offenders(tmp_path) == ["store/gc.py:2", "store/scrub.py:2"]


# --- secrets never reach a repr -------------------------------------------------


def test_a_vended_credential_does_not_print_its_secret() -> None:
    """`repr` lands in logs, tracebacks and pytest failure output.

    An id and an expiry identify a credential well enough to debug with; the
    secret and the session token *are* the credential.
    """
    vended = ScopedCredentials(
        access_key_id="AKIAEXAMPLE",
        secret_access_key="s3cr3t-vended-key",
        session_token="tok3n-vended",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        prefix="domains/x/",
        read_only=True,
    )

    printed = repr(vended)

    assert "AKIAEXAMPLE" in printed
    assert "s3cr3t-vended-key" not in printed
    assert "tok3n-vended" not in printed


def test_the_store_config_does_not_print_its_secret_key() -> None:
    config = S3Config(
        endpoint_url="http://localhost:27218",
        region="us-east-1",
        bucket="alkera-files",
        access_key="alkera",
        secret_key="alkera-dev-secret",
    )

    printed = repr(config)

    assert "alkera-files" in printed
    assert "alkera-dev-secret" not in printed
