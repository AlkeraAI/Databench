"""No box module writes into a chat's tree except through the chat tree seam.

The daemon runs as root and the agent controls every name in its tree, so a
bare ``os.replace`` or ``Path.unlink`` there is a door: it follows a link the
agent planted, or leaves a root-owned file the agent cannot edit. This scans
the modules that touch chat trees for every call that creates, replaces,
moves, removes, re-modes or opens a file, and fails on any that does not go
through :class:`~alkera_cli.files.chat_fs.ChatTree` (``tree`` / ``target`` /
``self.tree`` / ``self._tree``) and is not listed below with the reason it
never touches a chat tree. A new call fails here until it is routed or its
reason is written down.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]

#: Every module that reads or writes a chat's tree on a box: the sync and the
#: pull, the mirror and the service around them, the harness runtime that
#: looks into the chat's trees, the sandbox launch that owns them, and the
#: tools that spill results, payloads and materialized blobs into the root.
_MODULES = (
    "apps/cli/alkera_cli/files/live_sync.py",
    "apps/cli/alkera_cli/files/live_fetch.py",
    "apps/cli/alkera_cli/files/live_peer.py",
    "apps/cli/alkera_cli/files/pull.py",
    "apps/cli/alkera_cli/files/target.py",
    "apps/cli/alkera_cli/cloud/folder.py",
    "apps/cli/alkera_cli/cloud/folder_wipe.py",
    "apps/cli/alkera_cli/cloud/mirror.py",
    "apps/cli/alkera_cli/cloud/service.py",
    "apps/cli/alkera_cli/cloud/transport.py",
    "apps/cli/alkera_cli/harness/runtime.py",
    "apps/cli/alkera_cli/harness/sandbox.py",
    "apps/cli/alkera_cli/harness/sandbox_env.py",
    "apps/cli/alkera_cli/harness/sandbox_layout.py",
    "apps/cli/alkera_cli/harness/sandbox_ownership.py",
    "apps/cli/alkera_cli/harness/sandbox_steps.py",
    "apps/cli/alkera_cli/harness/adapters/opencode_http.py",
    "apps/cli/alkera_cli/harness/adapters/opencode_secrets.py",
    "apps/cli/alkera_cli/plugins/plugin_base/agent_tree.py",
    "apps/cli/alkera_cli/plugins/plugin_base/bash_exec.py",
    "apps/cli/alkera_cli/plugins/plugin_base/bash_tool.py",
    "apps/cli/alkera_cli/plugins/plugin_base/blob_write_tools.py",
    "apps/cli/alkera_cli/plugins/data_common/graph_python_tool.py",
    "apps/cli/alkera_cli/plugins/data_common/integration_sdk_tool.py",
)

_OS_CALLS = frozenset(
    {
        "replace",
        "rename",
        "renames",
        "unlink",
        "remove",
        "link",
        "symlink",
        "mkdir",
        "makedirs",
        "chmod",
        "chown",
        "lchown",
        "mkfifo",
        "mknod",
        "open",
        "truncate",
        "utime",
        "rmdir",
        "removedirs",
        "setxattr",
    }
)
_PATH_CALLS = frozenset(
    {
        "unlink",
        "write_bytes",
        "write_text",
        "read_bytes",
        "read_text",
        "mkdir",
        "rename",
        "replace",
        "touch",
        "symlink_to",
        "hardlink_to",
        "chmod",
        "lchmod",
        "rmdir",
        "open",
    }
)
#: The ``shutil`` calls that make, move, remove or re-own files; ``which``
#: and the rest only look.
_SHUTIL_CALLS = frozenset(
    {
        "copy",
        "copy2",
        "copyfile",
        "copyfileobj",
        "copymode",
        "copystat",
        "copytree",
        "move",
        "rmtree",
        "chown",
    }
)
_SEAMS = frozenset({"tree", "target", "self.tree", "self._tree"})


def _is_seam(owner: str) -> bool:
    """A chat tree held in a well-known name, or made on the spot."""
    return owner in _SEAMS or owner.startswith("ChatTree(")


_LOCAL_PULL = "a pull on a person's own machine (no chat tree); the chat tree branch routes above"
_CONFIG_ROOT = (
    "the agent config root is the daemon's own (root, 0700), bound read-only into the "
    "container; the chat owns only its state directory, whose name sits in the root"
)
_HARNESS_DIR = "the chat's runtime state directory, root's own under the chat's private directory"
_PID_FILE = (
    "the command's pid file in the daemon's temp directory, written by runsc, never the tree"
)
_CHAT_RECORDS = "opens the chat's records (transcript, manifest) under the daemon's chats root"

_SECRETS_FILE_REASON = (
    "the agent's secrets file, made fresh, owner-only and through no link; "
    "a tree write would leave it group-readable"
)

#: (module, enclosing function, call) -> why it never writes into a chat tree.
_ALLOWED: dict[tuple[str, str, str], str] = {
    (
        "harness/adapters/opencode_secrets.py",
        "write_agent_secrets",
        "os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | nofollow)",
    ): _SECRETS_FILE_REASON,
    (
        "harness/adapters/opencode_secrets.py",
        "write_agent_secrets",
        "os.open(SECRETS_FILE, flags, 384, dir_fd=parent)",
    ): _SECRETS_FILE_REASON,
    (
        "harness/adapters/opencode_secrets.py",
        "write_agent_secrets",
        "os.open(directory / SECRETS_FILE, flags | getattr(os, 'O_BINARY', 0), 384)",
    ): _SECRETS_FILE_REASON,
    (
        "files/live_sync.py",
        "LiveSync._apply_write",
        "fetched.unlink()",
    ): "the spool file the download landed in",
    (
        "files/live_fetch.py",
        "fetch_inbound",
        "temporary.unlink()",
    ): "the spool file a failed download leaves",
    (
        "files/live_sync.py",
        "RestLiveApi.download",
        "into.parent.mkdir(parents=True, exist_ok=True)",
    ): "the spool, handed in by fetch_inbound",
    (
        "files/live_sync.py",
        "RestLiveApi.submit_conflict",
        "staged.open('rb')",
    ): "reads a staging file the sync made through the tree; an upload, never a write",
    (
        "files/target.py",
        "MaterializationTarget.__init__",
        "self.root.mkdir(parents=True, exist_ok=True)",
    ): "the root itself, which the daemon names under its chats root",
    (
        "files/target.py",
        "MaterializationTarget._walk_openat2",
        "os.open(self._root_real, os.O_RDONLY | os.O_DIRECTORY)",
    ): "a read-only descriptor on the root for the containment walk",
    (
        "files/target.py",
        "MaterializationTarget.mkdir",
        "where.mkdir(parents=True, exist_ok=True)",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.write_bytes",
        "where.parent.mkdir(parents=True, exist_ok=True)",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.open_read",
        "self.resolve(relative).open('rb')",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.open_write",
        "where.parent.mkdir(parents=True, exist_ok=True)",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.open_write",
        "where.open('ab' if append else 'wb')",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.replace",
        "there.parent.mkdir(parents=True, exist_ok=True)",
    ): _LOCAL_PULL,
    ("files/target.py", "MaterializationTarget.replace", "os.replace(here, there)"): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.unlink",
        "self.resolve_leaf(relative).unlink(missing_ok=missing_ok)",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.rmdir",
        "self.resolve(relative).rmdir()",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.copy",
        "there.parent.mkdir(parents=True, exist_ok=True)",
    ): _LOCAL_PULL,
    ("files/target.py", "MaterializationTarget.copy", "there.unlink()"): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.copy",
        "there.write_bytes(here.read_bytes())",
    ): _LOCAL_PULL,
    ("files/target.py", "MaterializationTarget.copy", "here.read_bytes()"): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.make_fifo",
        "where.parent.mkdir(parents=True, exist_ok=True)",
    ): _LOCAL_PULL,
    ("files/target.py", "MaterializationTarget.make_fifo", "os.mkfifo(where)"): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.write_pointer",
        "self.write_bytes(relative, encoded)",
    ): "write_bytes, which routes the chat tree",
    (
        "files/target.py",
        "MaterializationTarget.flush_links",
        "where.parent.mkdir(parents=True, exist_ok=True)",
    ): _LOCAL_PULL,
    ("files/target.py", "MaterializationTarget.flush_links", "where.unlink()"): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.flush_links",
        "os.symlink(local_target, os.fsencode(where))",
    ): _LOCAL_PULL,
    (
        "files/target.py",
        "MaterializationTarget.set_attrs",
        "os.chmod(where, effective)",
    ): _LOCAL_PULL,
    ("files/target.py", "_stamp_mtime", "os.utime(where, ns=(mtime_ns, mtime_ns))"): _LOCAL_PULL,
    (
        "files/target.py",
        "_stamp_mtime",
        "os.utime(where, ns=(mtime_ns, mtime_ns), follow_symlinks=False)",
    ): _LOCAL_PULL,
    (
        "cloud/folder.py",
        "ChatFolders._take_locked",
        "root.mkdir(parents=True, exist_ok=True)",
    ): "the chat's root itself, under the daemon's chats root",
    (
        "cloud/folder.py",
        "ChatFolders._drop_journal",
        "Path(f'{path}{suffix}').unlink(missing_ok=True)",
    ): "the journal under the daemon's home",
    (
        "cloud/folder_wipe.py",
        "wipe_copy",
        "node_map_path(root, home=home).unlink(missing_ok=True)",
    ): "the node map under the daemon's home",
    (
        "cloud/folder_wipe.py",
        "wipe_copy",
        "shutil.rmtree(root)",
    ): "removes the whole tree after the hand-back; rmtree never follows a link",
    (
        "cloud/mirror.py",
        "ChatMirror._open_on",
        "working_dir.mkdir(parents=True, exist_ok=True)",
    ): "the tree's root itself, whose name sits in the chat's private directory",
    ("cloud/mirror.py", "ChatMirror._seed_local_chat", "store.open(self._chat_id)"): _CHAT_RECORDS,
    ("harness/runtime.py", "HarnessRuntime._open_browser", "webbrowser.open(url)"): "a browser",
    (
        "harness/runtime.py",
        "HarnessRuntime.open_chat",
        "self._chats_store.open(session_id)",
    ): _CHAT_RECORDS,
    (
        "harness/runtime.py",
        "HarnessRuntime.mark_chat_seen._stamp",
        "self._chats_store.open(session_id)",
    ): _CHAT_RECORDS,
    (
        "harness/sandbox_steps.py",
        "_default_write",
        "path.write_text(content, encoding='utf-8')",
    ): (
        "a launch's write steps land in the bundle and cgroup, never an owned tree "
        "(test_sandbox pins that no write step names one)"
    ),
    (
        "harness/sandbox_steps.py",
        "run_steps",
        "os.chmod(step.path, step.mode)",
    ): (
        "the mode of a write step's own file, in the bundle or cgroup, never an owned "
        "tree (test_sandbox pins that no write step names one)"
    ),
    (
        "harness/adapters/opencode_http.py",
        "_read_owner_breadcrumb",
        "path.read_text(encoding='utf-8')",
    ): _CONFIG_ROOT,
    (
        "harness/adapters/opencode_http.py",
        "sweep_stale_agent_config_roots",
        "shutil.rmtree(entry, ignore_errors=True)",
    ): "removes a config root whole; rmtree walks by descriptor and follows no link",
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter._start_once",
        "self._harness_dir.mkdir(parents=True, exist_ok=True)",
    ): _HARNESS_DIR,
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter.stop",
        "(self._harness_dir / 'pid').unlink()",
    ): _HARNESS_DIR,
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter._reap_orphan_opencode",
        "pid_path.unlink(missing_ok=True)",
    ): _HARNESS_DIR,
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter._reap_orphan_opencode",
        "pid_path.unlink()",
    ): _HARNESS_DIR,
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter._ensure_agent_config_root",
        "self._agent_config_root.mkdir(parents=True, exist_ok=True, mode=448)",
    ): _CONFIG_ROOT,
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter._ensure_agent_config_root",
        "(self._agent_config_dir / 'agent').mkdir(parents=True, exist_ok=True, mode=448)",
    ): _CONFIG_ROOT,
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter._ensure_agent_config_root",
        "self._agent_state_dir.mkdir(exist_ok=True, mode=448)",
    ): _CONFIG_ROOT,
    (
        "harness/adapters/opencode_http.py",
        "OpencodeHttpAdapter._remove_agent_config_root",
        "shutil.rmtree(self._agent_config_root, ignore_errors=True)",
    ): "removes the config root whole; rmtree walks by descriptor and follows no link",
    (
        "plugins/plugin_base/bash_exec.py",
        "_exec_pid",
        "pid_file.read_text(encoding='ascii')",
    ): _PID_FILE,
    ("plugins/plugin_base/bash_exec.py", "run_command", "pid_file.unlink()"): _PID_FILE,
    (
        "plugins/plugin_base/blob_write_tools.py",
        "_require_sandbox",
        "sandbox.mkdir(parents=True, exist_ok=True)",
    ): "a local session's sandbox root under the chat's records; never a bounded session's",
    (
        "plugins/plugin_base/agent_tree.py",
        "tool_output",
        "root.mkdir(parents=True, exist_ok=True)",
    ): "a local session's root under the daemon's own directories; never a bounded session's",
}


def _receiver(call: ast.Call) -> str | None:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    return ast.unparse(func.value)


def _raw_calls(module: str, repo: Path = _REPO) -> list[tuple[str, str]]:
    """Every (enclosing function, call) in ``module`` that touches a file
    other than through the seam."""
    tree = ast.parse((repo / module).read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    stack: list[str] = []

    def visit(node: ast.AST) -> None:
        named = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        if named:
            stack.append(node.name)  # type: ignore[attr-defined]
        if isinstance(node, ast.Call):
            func = node.func
            hit = False
            if isinstance(func, ast.Name) and func.id == "open":
                hit = True
            elif isinstance(func, ast.Attribute):
                owner = _receiver(node)
                if owner == "shutil":
                    hit = func.attr in _SHUTIL_CALLS
                elif owner == "os":
                    hit = func.attr in _OS_CALLS
                elif owner is not None and func.attr in _PATH_CALLS:
                    # Path.replace takes the one target; str.replace takes two
                    # strings and datetime.replace keywords only: not files.
                    is_file_replace = func.attr != "replace" or (
                        len(node.args) == 1 and not node.keywords
                    )
                    hit = not _is_seam(owner) and is_file_replace
            if hit:
                found.append((".".join(stack), ast.unparse(node)))
        for child in ast.iter_child_nodes(node):
            visit(child)
        if named:
            stack.pop()

    visit(tree)
    return found


@pytest.mark.parametrize("module", _MODULES)
def test_every_file_touch_goes_through_the_chat_tree_or_says_why_not(module: str) -> None:
    short = module.removeprefix("apps/cli/alkera_cli/")
    unexplained = [
        f"{where}: {call}"
        for where, call in _raw_calls(module)
        if (short, where, call) not in _ALLOWED
    ]
    assert unexplained == [], (
        "route these through alkera_cli.files.chat_fs.ChatTree, or add them to "
        "_ALLOWED with the reason they never touch a chat's tree:\n" + "\n".join(unexplained)
    )


def test_every_allowance_still_names_a_real_call() -> None:
    """A stale allowance would let the next call with that spelling through."""
    present = {
        (module.removeprefix("apps/cli/alkera_cli/"), where, call)
        for module in _MODULES
        for where, call in _raw_calls(module)
    }
    assert sorted(set(_ALLOWED) - present) == []


def test_the_guard_catches_a_bare_replace(tmp_path: Path) -> None:
    """The scan is not vacuous: a bare os.replace, Path.unlink, Path.read_text
    or shutil.rmtree in a scanned module is found, while a call through the
    chat tree (held or made on the spot), a string's replace, a datetime's
    replace and a shutil lookup are not."""
    module = tmp_path / "apps/cli/alkera_cli/files/sample.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import os, shutil\n"
        "def land(a, b, tree, when, version):\n"
        "    tree.rename(a, b)\n"
        "    ChatTree(a.parent).read_bytes(a.name)\n"
        "    os.replace(a, b)\n"
        "    b.unlink()\n"
        "    a.read_text()\n"
        "    shutil.rmtree(a)\n"
        "    shutil.which('bash')\n"
        "    'x'.replace('a', 'b')\n"
        "    url.replace('{version}', version)\n"
        "    when.replace(tzinfo=None)\n",
        encoding="utf-8",
    )
    assert _raw_calls("apps/cli/alkera_cli/files/sample.py", tmp_path) == [
        ("land", "os.replace(a, b)"),
        ("land", "b.unlink()"),
        ("land", "a.read_text()"),
        ("land", "shutil.rmtree(a)"),
    ]
