"""The ``working_copy.*`` daemon façade, end to end against a real drive.

The façade translates RPC calls into the working-copy library and passes back
what a pass did; these drive it the way the editor does (open, attach, sync,
resolve) against the real backend in ``files._chat_backend``, with the stored
sign-in pointed at that backend. What each case asserts is what the editor
would see (the response, the notification) and what the drive then holds.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.daemon import methods as _register_methods  # noqa: F401 — populates METHODS
from alkera_cli.daemon.methods import working_copy as wc
from alkera_cli.daemon.protocol import METHODS, NOTIFICATIONS
from alkera_cli.daemon.server import AuthRequiredError
from alkera_cli.files import working_copy_live
from alkera_cli.files.working_copy import WorkingCopyStore
from alkera_cli.host import paths as cli_paths
from alkera_sdk import AlkeraClient
from files._chat_backend import Chat, chat_backend, make_chat, remote_bytes, rewrite_remote, upload
from files._live_backend import LiveBackend

#: One backend serves the whole module, so its tests stay on one worker.
pytestmark = pytest.mark.xdist_group("working_copy_daemon")


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveBackend]:
    with chat_backend(tmp_path_factory.mktemp("server")) as served:
        yield served


@pytest.fixture
def api(backend: LiveBackend) -> Iterator[AlkeraClient]:
    with AlkeraClient(base_url=backend.base_url, token=backend.token) as client:
        yield client


@pytest.fixture
def chat(api: AlkeraClient) -> Chat:
    return make_chat(api)


@pytest.fixture
def signed_in(
    backend: LiveBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    monkeypatch.setattr(wc, "api_credentials", lambda: (backend.base_url, backend.token))
    monkeypatch.setattr(cli_paths, "ALKERA_HOME", tmp_path / "home")
    yield
    for server in list(working_copy_live._BY_SERVER.keys()):
        asyncio.run(wc._stop_every_copy(server))


class Editor:
    """The editor end of the connection: it hears every notification."""

    def __init__(self) -> None:
        self.heard: list[tuple[str, Any]] = []

    async def notify(self, method: str, params: Any = None) -> None:
        self.heard.append((method, params))


async def _until(check: Any, *, within: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.05)


def _open(chat_id: str, tmp_path: Path, *, kind: str = "chat") -> wc.WorkingCopyOpenResponse:
    request = wc.WorkingCopyOpenRequest(kind=kind, id=chat_id, location=str(tmp_path / "copies"))
    return asyncio.run(wc.working_copy_open(Editor(), request))


# ---------------------------------------------------------------------------
# The protocol: additions only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "request_type", "response_type"),
    [
        pytest.param(
            "working_copy.open", wc.WorkingCopyOpenRequest, wc.WorkingCopyOpenResponse, id="open"
        ),
        pytest.param(
            "working_copy.attach",
            wc.WorkingCopyAttachRequest,
            wc.WorkingCopyAttachResponse,
            id="attach",
        ),
        pytest.param(
            "working_copy.sync", wc.WorkingCopySyncRequest, wc.WorkingCopySyncResponse, id="sync"
        ),
        pytest.param(
            "working_copy.resolve",
            wc.WorkingCopyResolveRequest,
            wc.WorkingCopyResolveResponse,
            id="resolve",
        ),
        pytest.param(
            "working_copy.set_dirty",
            wc.WorkingCopySetDirtyRequest,
            wc.WorkingCopySetDirtyResponse,
            id="set-dirty",
        ),
        pytest.param(
            "working_copy.resolve_deletions",
            wc.WorkingCopyResolveDeletionsRequest,
            wc.WorkingCopyResolveDeletionsResponse,
            id="resolve-deletions",
        ),
    ],
)
def test_each_method_is_registered_with_its_types(
    name: str, request_type: type, response_type: type
) -> None:
    spec = METHODS[name]
    assert (spec.request_type, spec.response_type) == (request_type, response_type)


def test_the_synced_notification_is_registered() -> None:
    assert NOTIFICATIONS["working_copy.synced"] is wc.WorkingCopySyncedNotification


def test_an_open_request_from_an_editor_that_names_no_kind_opens_a_chat() -> None:
    """The kind is defaulted, so the first editor build that only knows chats
    keeps working once a workspace kind exists."""
    assert wc.WorkingCopyOpenRequest(id="x", location="/tmp").kind == "chat"


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


def test_open_makes_the_copy_and_says_where(
    signed_in: None, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    upload(api, tmp_path, chat.drive_id, chat.working_id, "report.md", b"# hi\n")

    answer = _open(chat.id, tmp_path)

    assert answer.status == "opened"
    assert answer.title == "Quarterly plan"
    assert answer.root is not None
    assert (Path(answer.root) / "report.md").read_bytes() == b"# hi\n"
    assert answer.report is not None and answer.report.downloaded == ["report.md"]


@pytest.mark.parametrize(
    ("kind", "target", "status"),
    [
        pytest.param("workspace", str(uuid.uuid4()), "not_found", id="a-server-without-workspaces"),
        pytest.param("chat", str(uuid.uuid4()), "not_found", id="no-such-chat"),
        pytest.param("chat", "../../etc", "invalid", id="not-an-id"),
    ],
)
def test_open_answers_a_target_it_cannot_copy_with_a_status_not_an_error(
    signed_in: None, tmp_path: Path, kind: str, target: str, status: str
) -> None:
    answer = _open(target, tmp_path, kind=kind)

    assert answer.status == status
    assert answer.message
    assert answer.root is None


def test_open_refuses_a_relative_location(signed_in: None, chat: Chat) -> None:
    request = wc.WorkingCopyOpenRequest(id=chat.id, location="copies")

    answer = asyncio.run(wc.working_copy_open(Editor(), request))

    assert answer.status == "invalid"


def test_open_signed_out_asks_for_a_sign_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wc, "api_credentials", lambda: None)

    with pytest.raises(AuthRequiredError) as refused:
        _open(str(uuid.uuid4()), tmp_path)
    assert refused.value.reason == "missing"


def test_open_with_a_refused_sign_in_asks_for_a_new_one(
    backend: LiveBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wc, "api_credentials", lambda: (backend.base_url, "not-a-token"))

    with pytest.raises(AuthRequiredError) as refused:
        _open(str(uuid.uuid4()), tmp_path)
    assert refused.value.reason == "rejected"


# ---------------------------------------------------------------------------
# attach, sync, resolve
# ---------------------------------------------------------------------------


def test_attach_to_a_folder_that_is_no_copy_attaches_nothing(
    signed_in: None, tmp_path: Path
) -> None:
    request = wc.WorkingCopyAttachRequest(root=str(tmp_path))

    answer = asyncio.run(wc.working_copy_attach(Editor(), request))

    assert answer.attached is False


def test_an_attached_copy_sends_up_what_changed_while_nothing_was_watching(
    signed_in: None, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plot.py", b"print(1)\n")
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = Path(opened.root)
    (root / "plot.py").write_bytes(b"print(2)\n")
    editor = Editor()

    async def scenario() -> wc.WorkingCopyAttachResponse:
        answer = await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=str(root)))
        await _until(lambda: bool(editor.heard))
        return answer

    answer = asyncio.run(scenario())

    assert (answer.attached, answer.kind, answer.id) == (True, "chat", chat.id)
    method, params = editor.heard[0]
    assert method == "working_copy.synced"
    assert params.root == str(root)
    assert params.report.uploaded == ["plot.py"]
    assert remote_bytes(api, tmp_path, chat.drive_id, node) == b"print(2)\n"


def test_sync_and_resolve_reach_the_attached_copy(
    signed_in: None, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "plan.md", b"base\n")
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = str(opened.root)

    async def scenario() -> tuple[Any, Any, Any]:
        editor = Editor()
        await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
        await asyncio.to_thread(rewrite_remote, api, chat.drive_id, node, b"theirs\n")
        (Path(root) / "plan.md").write_bytes(b"mine\n")
        synced = await wc.working_copy_sync(editor, wc.WorkingCopySyncRequest(root=root))
        settled = await wc.working_copy_resolve(
            editor,
            wc.WorkingCopyResolveRequest(root=root, path="plan.md", keep="theirs"),
        )
        unknown = await wc.working_copy_sync(
            editor,
            wc.WorkingCopySyncRequest(root=str(tmp_path / "elsewhere")),
        )
        return synced, settled, unknown

    synced, settled, unknown = asyncio.run(scenario())

    assert synced.attached is True
    assert [(c.path, c.reason) for c in synced.report.conflicts] == [("plan.md", "edited_both")]
    assert settled.report.conflicts == []
    assert (Path(root) / "plan.md").read_bytes() == b"theirs\n"
    assert unknown.attached is False


def test_the_editors_dirty_files_are_held_until_the_buffer_is_clean(
    signed_in: None, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    """A dirty file is held while the drive moves on; when the editor says it is
    clean again without a save (a revert), the drive's bytes come down."""
    node = upload(api, tmp_path, chat.drive_id, chat.working_id, "live.md", b"base\n")
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = str(opened.root)
    editor = Editor()

    async def scenario() -> tuple[Any, Any]:
        await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
        held = await wc.working_copy_set_dirty(
            editor, wc.WorkingCopySetDirtyRequest(root=root, paths=["live.md"])
        )
        await asyncio.to_thread(rewrite_remote, api, chat.drive_id, node, b"newer\n")
        await asyncio.to_thread(
            lambda: wc.sessions_of(editor).get(root).runner.copy.sync()  # a full pass meanwhile
        )
        assert (Path(root) / "live.md").read_bytes() == b"base\n"
        released = await wc.working_copy_set_dirty(
            editor, wc.WorkingCopySetDirtyRequest(root=root, paths=[])
        )
        await _until(lambda: (Path(root) / "live.md").read_bytes() == b"newer\n")
        unknown = await wc.working_copy_set_dirty(
            editor, wc.WorkingCopySetDirtyRequest(root=str(tmp_path / "elsewhere"), paths=[])
        )
        return (held, released), unknown

    (held, released), unknown = asyncio.run(scenario())

    assert held.attached and released.attached
    assert unknown.attached is False


def test_a_copy_whose_tree_is_gone_stops_and_says_so(
    signed_in: None, chat: Chat, tmp_path: Path
) -> None:
    """The editor hears why the copy stopped, and the daemon stops serving it."""
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = opened.root
    (Path(root) / "kept.md").write_text("local work\n", encoding="utf-8")
    store = WorkingCopyStore(tmp_path / "home")
    record = store.load("chat", chat.id)
    assert record is not None
    record.node_id = str(uuid.uuid4())  # a tree this account can no longer find
    store.save(record)
    editor = Editor()

    async def scenario() -> Any:
        await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
        await _until(lambda: bool(editor.heard))
        await asyncio.sleep(0.2)
        return await wc.working_copy_sync(editor, wc.WorkingCopySyncRequest(root=root))

    after = asyncio.run(scenario())

    method, note = editor.heard[0]
    assert method == "working_copy.synced"
    assert note.access_lost is True
    assert note.error == "You no longer have access to Quarterly plan. Local files are kept."
    assert after.attached is False
    assert (Path(root) / "kept.md").read_text(encoding="utf-8") == "local work\n"


def test_each_server_keeps_its_own_copies(signed_in: None, chat: Chat, tmp_path: Path) -> None:
    """Sessions belong to the server that attached them: another server (a
    second daemon in one process, a test's) neither sees nor stops them."""
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = opened.root
    first, second = Editor(), Editor()

    async def scenario() -> tuple[Any, Any]:
        await wc.working_copy_attach(first, wc.WorkingCopyAttachRequest(root=root))
        await wc._stop_every_copy(second)
        mine = await wc.working_copy_sync(first, wc.WorkingCopySyncRequest(root=root))
        theirs = await wc.working_copy_sync(second, wc.WorkingCopySyncRequest(root=root))
        return mine, theirs

    mine, theirs = asyncio.run(scenario())

    assert mine.attached is True
    assert theirs.attached is False


def test_attaches_racing_for_one_folder_start_one_runner(
    signed_in: None, chat: Chat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor attaches on every daemon start and every sign-in, often both
    at once. Two runners on one folder would sync it twice, and the one the
    session table lost would never be stopped."""
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = opened.root
    started: list[object] = []

    class CountedRunner(wc.WorkingCopyRunner):
        def start(self) -> None:
            started.append(self)
            super().start()

    monkeypatch.setattr(wc, "WorkingCopyRunner", CountedRunner)
    editor = Editor()

    async def scenario() -> list[Any]:
        return list(
            await asyncio.gather(
                *(
                    wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
                    for _ in range(3)
                )
            )
        )

    answers = asyncio.run(scenario())

    assert [answer.attached for answer in answers] == [True, True, True]
    assert len(started) == 1


def test_held_deletions_are_answered_through_the_daemon(
    signed_in: None, api: AlkeraClient, chat: Chat, tmp_path: Path
) -> None:
    for i in range(6):
        upload(api, tmp_path, chat.drive_id, chat.working_id, f"f{i}.md", b"x\n")
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = opened.root
    for child in list(Path(root).iterdir()):
        child.unlink()
    editor = Editor()

    async def scenario() -> Any:
        await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
        await _until(lambda: bool(editor.heard))
        return await wc.working_copy_resolve_deletions(
            editor, wc.WorkingCopyResolveDeletionsRequest(root=root, apply=False)
        )

    restored = asyncio.run(scenario())

    _, note = editor.heard[0]
    assert note.report.deletions_held == [f"f{i}.md" for i in range(6)]
    assert restored.attached is True
    assert restored.report.restored == [f"f{i}.md" for i in range(6)]
    assert len(list(api.files.children(chat.drive_id, chat.working_id))) == 6


def test_a_copy_whose_folder_is_gone_stops_and_says_so(
    signed_in: None, chat: Chat, tmp_path: Path
) -> None:
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = opened.root
    shutil.rmtree(root)
    editor = Editor()

    async def scenario() -> None:
        await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
        await _until(lambda: bool(editor.heard))

    asyncio.run(scenario())

    _, note = editor.heard[0]
    assert note.detached is True and note.access_lost is False
    assert (
        note.error == "The local folder for Quarterly plan is gone. Nothing was changed in Alkera."
    )
    assert not Path(root).exists()


# ---------------------------------------------------------------------------
# A refused sign-in mid-session, and what a failed pass tells the editor
# ---------------------------------------------------------------------------


def _attached_with_a_revoked_sign_in(
    root: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[Editor, AlkeraClient]:
    """Attach ``root`` and let its first pass land (it sends a local edit, so
    the editor hears it); then the stored sign-in is refused on every later
    call the copy makes."""
    (Path(root) / "plan.md").write_bytes(b"edited here\n")
    made: list[AlkeraClient] = []

    class RecordedClient(AlkeraClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(wc, "AlkeraClient", RecordedClient)
    editor = Editor()

    async def attach() -> None:
        await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
        await _until(lambda: bool(editor.heard))

    asyncio.run(attach())
    (client,) = made
    client.raw_client.get_httpx_client().headers["Authorization"] = "Bearer not-a-token"
    return editor, client


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda editor, root: wc.working_copy_sync(editor, wc.WorkingCopySyncRequest(root=root)),
            id="sync",
        ),
        pytest.param(
            lambda editor, root: wc.working_copy_resolve(
                editor, wc.WorkingCopyResolveRequest(root=root, path="plan.md", keep="mine")
            ),
            id="resolve",
        ),
        pytest.param(
            lambda editor, root: wc.working_copy_resolve_deletions(
                editor, wc.WorkingCopyResolveDeletionsRequest(root=root, apply=True)
            ),
            id="resolve-deletions",
        ),
    ],
)
def test_a_sign_in_refused_mid_session_asks_the_editor_for_a_new_one(
    call: Any,
    signed_in: None,
    api: AlkeraClient,
    chat: Chat,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload(api, tmp_path, chat.drive_id, chat.working_id, "plan.md", b"base\n")
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    editor, _ = _attached_with_a_revoked_sign_in(opened.root, monkeypatch)

    with pytest.raises(AuthRequiredError) as refused:
        asyncio.run(call(editor, opened.root))

    # The server answers this error as -32001 with the reason (pinned in
    # test_files_facade_errors), the code the extension's sign-in flow keys on.
    assert refused.value.reason == "rejected"


class _Failing:
    """A pass that fails the way a library call can, with words the person
    must not see: a URL and an absolute path."""

    URL = "https://api.alkera.example/api/v1/drives/5f0c/nodes/secret-node/content"
    PATH = "/Users/someone/Alkera/Quarterly plan/private notes.md"

    @classmethod
    def errors(cls) -> list[BaseException]:
        request = httpx.Request("GET", cls.URL)
        return [
            httpx.HTTPStatusError(
                f"Server error '500 Internal Server Error' for url '{cls.URL}'",
                request=request,
                response=httpx.Response(500, request=request),
            ),
            PermissionError(13, "Permission denied", cls.PATH),
            RuntimeError(f"could not move {cls.PATH} after reading {cls.URL}"),
        ]


@pytest.mark.parametrize(
    "failure", [pytest.param(e, id=type(e).__name__) for e in _Failing.errors()]
)
def test_a_failed_pass_tells_the_editor_a_sentence_not_the_exception(
    failure: BaseException,
    signed_in: None,
    chat: Chat,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _open(chat.id, tmp_path)
    assert opened.root is not None
    root = opened.root

    class FailingRunner(wc.WorkingCopyRunner):
        async def sync_now(self, *, pull: bool = True) -> Any:
            raise failure

    monkeypatch.setattr(wc, "WorkingCopyRunner", FailingRunner)
    editor = Editor()

    async def scenario() -> None:
        await wc.working_copy_attach(editor, wc.WorkingCopyAttachRequest(root=root))
        await _until(lambda: bool(editor.heard))

    asyncio.run(scenario())

    _, note = editor.heard[0]
    assert note.error
    assert _Failing.URL not in note.error and "api.alkera.example" not in note.error
    assert _Failing.PATH not in note.error and "/Users/" not in note.error
    assert not (note.auth_required or note.access_lost or note.detached)


def test_the_realtime_stream_announces_itself_in_the_runners_terms(backend: LiveBackend) -> None:
    """The runner treats the stream's opened marker as "look at everything"
    and as a healthy connection for its backoff; the realtime client's marker
    reaches it as the runner's own, unchanged."""
    from alkera_cli.files.working_copy_live import STREAM_OPENED

    async def first() -> Any:
        frames = wc._frames(backend.base_url, backend.token)
        try:
            return await asyncio.wait_for(anext(frames), timeout=20.0)
        finally:
            await frames.aclose()

    assert asyncio.run(first())["type"] == STREAM_OPENED
