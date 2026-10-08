"""Ratchet: the notebook engine touches a tree only through ``tree_io``.

The engine runs (on a box) as a uid that owns far more than the workspace
tree it writes into, and anyone who can run a cell can put a link in that
tree. :mod:`alkera_notebook.tree_io` is the one module that creates, writes,
renames, re-modes, lists for removal or deletes there, and it never follows a
link. This scan (over source, by AST) fails when any other module of
``alkera_notebook`` calls a raw filesystem mutator.

The allowlist holds the callers that write only where no other uid can (a
private temporary directory of their own, a laptop's local launcher, the
agent simulator's scratch root) and names that only look like mutators. It
may only shrink: an entry whose file now has fewer calls must be lowered or
removed, and a new caller goes through ``tree_io`` instead of onto this list.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "alkera_notebook"

#: The owner, and the generated copy of the marimo fork (third-party code the
#: engine imports for the format only; it is not the engine's writer).
EXEMPT = frozenset({"tree_io.py"})
GENERATED = frozenset({"_marimo"})

OS_MUTATORS = frozenset(
    {
        "open",
        "fdopen",
        "mkdir",
        "makedirs",
        "unlink",
        "remove",
        "removedirs",
        "rmdir",
        "rename",
        "renames",
        "replace",
        "symlink",
        "link",
        "chmod",
        "chown",
        "lchown",
        "truncate",
        "mkfifo",
    }
)
SHUTIL_MUTATORS = frozenset(
    {"rmtree", "move", "copy", "copy2", "copyfile", "copytree", "copymode", "copystat"}
)
TEMPFILE_MUTATORS = frozenset(
    {"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory", "TemporaryFile"}
)
PATH_MUTATORS = frozenset(
    {
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "rmdir",
        "touch",
        "symlink_to",
        "hardlink_to",
        "chmod",
        "lchmod",
        "rename",
    }
)
WRITE_MODES = frozenset("wax+")

#: file (relative to the package) -> how many raw mutator calls it may make.
ALLOWED: dict[str, int] = {
    # The laptop's local kernel launcher: its own log file in a fresh
    # temporary directory and the platform mount it stages for its kernels,
    # all as the person's own uid.
    "kernels/launch_local.py": 6,
    # The kernel RPC endpoint: a private 0750 directory it makes under the
    # host's socket base, the socket in it, and their removal.
    "rpc/service.py": 7,
    # The command-line runner's own temporary data root.
    "cli/main.py": 2,
    # The agent simulator writes its fixtures into its own scratch root.
    "sim/engine_target.py": 1,
    "sim/machines.py": 2,
}


def _mode_writes(call: ast.Call, position: int, *, positional_must_be_text: bool) -> bool:
    mode: ast.expr | None = call.args[position] if len(call.args) > position else None
    if positional_must_be_text and not (
        isinstance(mode, ast.Constant) and isinstance(mode.value, str)
    ):
        # ``engine.open(path)``: not a file's mode.
        mode = None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(set(mode.value) & WRITE_MODES)
    return True  # a mode worked out at run time counts as a write


def _is_tree(node: ast.expr) -> bool:
    """A :class:`~alkera_notebook.tree_io.Tree` receiver: ``tree``,
    ``self._tree``, ``self._env_tree`` or ``Tree(...)``."""
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "Tree"
    return ast.unparse(node).lower().endswith("tree")


def _is_mutator(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "open" and _mode_writes(call, 1, positional_must_be_text=False)
    if not isinstance(func, ast.Attribute):
        return False
    if _is_tree(func.value) or (isinstance(func.value, ast.Name) and func.value.id == "self"):
        # The owner's own calls, and a class's methods of the same name
        # (``Document.replace(op)``): not a path.
        return False
    owner = func.value.id if isinstance(func.value, ast.Name) else None
    name = func.attr
    if owner == "os":
        return name in OS_MUTATORS
    if owner == "shutil":
        return name in SHUTIL_MUTATORS
    if owner == "tempfile":
        return name in TEMPFILE_MUTATORS
    if owner in ("dataclasses", "str"):
        return False
    if name == "open":
        return _mode_writes(call, 0, positional_must_be_text=True)
    if name == "replace":
        # ``Path.replace(target)`` takes one argument; ``str.replace`` two.
        return len(call.args) == 1 and not call.keywords
    return name in PATH_MUTATORS


def raw_mutators() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel.parts[0] in GENERATED or rel.as_posix() in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_mutator(node):
                found[rel.as_posix()] += 1
    return found


def test_no_module_but_tree_io_mutates_the_filesystem() -> None:
    found = raw_mutators()
    over = {f: n for f, n in found.items() if n > ALLOWED.get(f, 0)}
    assert not over, (
        "these modules call a raw filesystem mutator; go through "
        f"alkera_notebook.tree_io.Tree instead: {over}"
    )


def test_the_allowlist_only_shrinks() -> None:
    found = raw_mutators()
    stale = {f: (n, found.get(f, 0)) for f, n in ALLOWED.items() if found.get(f, 0) < n}
    assert not stale, f"lower these allowlist entries to what the files now call: {stale}"


#: The environment registry reads spec files in the workspace tree and build
#: state under the env root, both of which any cell can write, and what it
#: reads it writes back (a failed change restores the spec) or reports. So in
#: ``envs/`` reads go through ``tree_io`` too. Allowed: the platform's own
#: template directory, which the host chooses and no cell can write.
ENVS = "envs"
READERS = frozenset({"read_bytes", "read_text", "open"})
ALLOWED_ENV_READS: dict[str, int] = {
    # ``default_template`` files copied into a fresh spec.
    "envs/registry.py": 1,
    # The template's own ``pyproject.toml``.
    "envs/template.py": 1,
}


def _is_raw_read(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "open"
    if not isinstance(func, ast.Attribute) or func.attr not in READERS:
        return False
    return not _is_tree(func.value)


def raw_env_reads() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in sorted((PACKAGE / ENVS).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_raw_read(node):
                found[path.relative_to(PACKAGE).as_posix()] += 1
    return found


def test_the_environment_registry_reads_trees_only_through_tree_io() -> None:
    found = raw_env_reads()
    over = {f: n for f, n in found.items() if n > ALLOWED_ENV_READS.get(f, 0)}
    assert not over, (
        "these environment modules read a file without tree_io; read the tree "
        f"through alkera_notebook.tree_io.Tree instead: {over}"
    )
    stale = {f: n for f, n in ALLOWED_ENV_READS.items() if found.get(f, 0) < n}
    assert not stale, f"lower these allowlist entries to what the files now call: {stale}"


def test_the_read_scan_sees_raw_reads_and_not_tree_reads() -> None:
    caught = ["p.read_bytes()", "Path(p).read_text(encoding='utf-8')", "open(p)", "p.open('rb')"]
    ignored = ["self._tree.read_bytes(p)", "tree.read_text(p)", "self._env_tree.read_text(p)"]
    for source in caught:
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call) and _is_raw_read(call), source
    for source in ignored:
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call) and not _is_raw_read(call), source


def test_the_scan_sees_each_kind_of_mutator() -> None:
    """The scan itself: every shape it is meant to catch is caught, and the
    look-alikes are not."""
    caught = [
        "os.replace(a, b)",
        "os.unlink(p)",
        "shutil.rmtree(p)",
        "tempfile.mkstemp(dir=d)",
        "p.write_text('x')",
        "p.mkdir(parents=True)",
        "p.unlink(missing_ok=True)",
        "p.replace(target)",
        "open(p, 'w')",
        "p.open('xb')",
        "open(p, mode=m)",
        "Path(p).touch()",
    ]
    ignored = [
        "text.replace('a', 'b')",
        "dataclasses.replace(x, a=1)",
        "open(p)",
        "p.open('rb')",
        "p.read_text()",
        "engine.open(path)",
        "self.replace(op)",
        "tree.unlink(p)",
        "self._env_tree.write_text(p, t)",
        "Tree(root).unlink(p)",
    ]
    for source in caught:
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call) and _is_mutator(call), source
    for source in ignored:
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call) and not _is_mutator(call), source
