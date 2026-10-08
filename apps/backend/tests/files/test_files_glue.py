"""The wiring every Files route lane builds on, proven on its own.

Each test here pins one contract a route lane inherits without restating it:
the kill switch, the two header dependencies, the error mapping, the router's
discovery rule, and the one function that builds an item payload.
"""

from __future__ import annotations

import sys
import textwrap
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from _files_kit import NOT_FOUND, refusal
from alkera_core.config import settings
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.decider import EffectiveAccess
from alkera_core.files.errors import (
    Conflict,
    FilesError,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
    QuotaExceeded,
)
from alkera_core.files.idempotency import MissingIdempotencyKey
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_snapshots import LeaseFacet as LeaseSnapshot
from backend.api.deps.files import (
    Idempotency,
    IfMatch,
    Lease,
    LeaseContext,
)
from backend.api.deps.files_errors import body_for
from backend.api.routes.files import (
    PREFIX,
    build_files_router,
    discovered_content_routers,
    discovered_routers,
    register_files_error_handlers,
)
from backend.services.files.items import to_item
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# --------------------------------------------------------------------------
# A harness app: the real dependencies and the real handler, one probe route.
# Route lanes have not landed yet, so the contract is proven against the
# wiring itself rather than against a family that could change under it.
# --------------------------------------------------------------------------


def _harness() -> FastAPI:
    app = FastAPI()
    parent = build_files_router()

    @parent.get("/_probe")
    async def probe() -> dict[str, str]:
        return {"ok": "yes"}

    @parent.post("/_probe/mutate")
    async def mutate(key: Idempotency, etag: IfMatch, lease: Lease) -> dict[str, object]:
        assert key is not None
        return {
            "key": key.key,
            "hash": key.request_hash.hex(),
            "etag": etag,
            "epoch": lease.epoch,
            "instance": lease.instance,
        }

    @parent.patch("/_probe/patch")
    async def patch(key: Idempotency, etag: IfMatch) -> dict[str, object]:
        return {"etag": etag}

    @parent.get("/_probe/raise/{which}")
    async def raiser(which: str) -> None:
        raise _ERRORS[which]

    app.include_router(parent)
    register_files_error_handlers(app)
    return app


_ERRORS: dict[str, FilesError] = {
    "not_found": NotFound(),
    "precondition": PreconditionFailed(),
    "conflict": Conflict("files.name_conflict", detail={"node_id": "n-1", "name": "secret.txt"}),
    "quota": QuotaExceeded(kind="bytes"),
    "invalid": InvalidRequest("files.bad_limit", "limit must be 1..1000"),
    "missing_key": MissingIdempotencyKey(),
}


async def _harness_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "expected_status", "expected_body"),
    [
        pytest.param(False, 404, NOT_FOUND, id="dark-answers-the-opaque-404"),
        pytest.param(True, 200, {"ok": "yes"}, id="enabled-answers-the-route"),
    ],
)
async def test_files_enabled_flag_gates_every_route(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    expected_status: int,
    expected_body: dict[str, object],
) -> None:
    """The flag is the only difference between a dark deployment and a live
    one, and dark is byte-identical to a node that does not exist."""
    monkeypatch.setattr(settings, "files_enabled", enabled)
    async with await _harness_client(_harness()) as client:
        response = await client.get(f"{PREFIX}/_probe")
    assert response.status_code == expected_status
    answered = refusal(response) if response.status_code >= 400 else response.json()
    assert answered == expected_body


# --------------------------------------------------------------------------
# Idempotency-Key
# --------------------------------------------------------------------------


async def test_files_non_get_without_idempotency_key_is_428(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        response = await client.post(f"{PREFIX}/_probe/mutate", json={"a": 1})
    assert response.status_code == 428
    assert response.json()["code"] == "files.idempotency_key_required"


async def test_files_get_needs_no_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GET declaring the dependency is not forced to carry a key — otherwise
    every read route would need a special case."""
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        response = await client.get(f"{PREFIX}/_probe")
    assert response.status_code == 200


async def test_files_idempotency_hash_separates_different_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored fingerprint covers the body, which is what makes a replay of
    the same key with a different request a 422 in the library rather than a
    silent replay of the first answer."""
    monkeypatch.setattr(settings, "files_enabled", True)
    headers = {"Idempotency-Key": "k-1", "If-Match": '"3"'}
    async with await _harness_client(_harness()) as client:
        first = await client.post(f"{PREFIX}/_probe/mutate", json={"a": 1}, headers=headers)
        second = await client.post(f"{PREFIX}/_probe/mutate", json={"a": 2}, headers=headers)
        same = await client.post(f"{PREFIX}/_probe/mutate", json={"a": 1}, headers=headers)
    assert first.json()["key"] == "k-1"
    assert first.json()["hash"] != second.json()["hash"]
    assert first.json()["hash"] == same.json()["hash"]


# --------------------------------------------------------------------------
# If-Match
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param("7", 7, id="bare-integer"),
        pytest.param('"7"', 7, id="quoted"),
        pytest.param('W/"7"', 7, id="weak"),
        pytest.param("  7  ", 7, id="whitespace-trimmed"),
    ],
)
async def test_files_if_match_parses_every_etag_spelling(
    monkeypatch: pytest.MonkeyPatch, header: str, expected: int
) -> None:
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        response = await client.patch(
            f"{PREFIX}/_probe/patch",
            json={},
            headers={"Idempotency-Key": "k", "If-Match": header},
        )
    assert response.status_code == 200
    assert response.json()["etag"] == expected


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        pytest.param({"Idempotency-Key": "k"}, 428, id="missing-if-match-is-428"),
        pytest.param(
            {"Idempotency-Key": "k", "If-Match": "not-an-etag"},
            422,
            id="malformed-if-match-is-422",
        ),
    ],
)
async def test_files_if_match_refusals(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], expected_status: int
) -> None:
    """Omitting the header and getting it wrong are different failures: one is
    a request that becomes valid once the header is added, the other is a
    header the caller got wrong."""
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        response = await client.patch(f"{PREFIX}/_probe/patch", json={}, headers=headers)
    assert response.status_code == expected_status


# --------------------------------------------------------------------------
# Lease headers
# --------------------------------------------------------------------------


_FENCE = {"X-Alkera-Lease-Epoch": "4", "X-Alkera-Lease-Instance": "box-a"}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({}, (None, None), id="unfenced-write-carries-neither"),
        pytest.param(_FENCE, (4, "box-a"), id="fenced-write-carries-both"),
        pytest.param(
            {**_FENCE, "X-Alkera-Lease-Final": "1"},
            (4, "box-a"),
            id="a-holder-claiming-the-hand-back-is-an-ordinary-fenced-write",
        ),
        pytest.param(
            {"X-Alkera-Lease-Final": "1"},
            (None, None),
            id="the-claim-alone-is-an-ordinary-unfenced-write",
        ),
    ],
)
async def test_files_lease_context_reads_both_headers_and_no_third(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    expected: tuple[int | None, str | None],
) -> None:
    """The epoch and the instance, and nothing a holder can say about its own
    ceilings: the exemption from them belongs to the release the server
    applies, so a request carrying the old marker is read as the request
    without it rather than as a claim on the drive's limits."""
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        response = await client.post(
            f"{PREFIX}/_probe/mutate",
            json={},
            headers={"Idempotency-Key": "k", "If-Match": "1", **headers},
        )
    assert response.status_code == 200
    body = response.json()
    assert "final" not in body
    assert (body["epoch"], body["instance"]) == expected


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-Alkera-Lease-Epoch": "4"}, id="epoch-without-instance"),
        pytest.param({"X-Alkera-Lease-Instance": "box-a"}, id="instance-without-epoch"),
        pytest.param(
            {"X-Alkera-Lease-Epoch": "nope", "X-Alkera-Lease-Instance": "box-a"},
            id="non-integer-epoch",
        ),
    ],
)
async def test_files_half_set_lease_headers_are_refused(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    """A half-set pair must not be read as "unfenced": that would let a stale
    holder's write through as an ordinary one."""
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        response = await client.post(
            f"{PREFIX}/_probe/mutate",
            json={},
            headers={"Idempotency-Key": "k", "If-Match": "1", **headers},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "files.bad_lease_headers"


def test_files_lease_context_fenced_flag() -> None:
    assert LeaseContext().fenced is False
    assert LeaseContext(epoch=0, instance="a").fenced is True


# --------------------------------------------------------------------------
# The error mapping table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("which", "status", "code"),
    [
        pytest.param("not_found", 404, "not_found", id="NotFound-404"),
        pytest.param("precondition", 412, "files.precondition_failed", id="Precondition-412"),
        pytest.param("conflict", 409, "files.name_conflict", id="Conflict-409"),
        pytest.param("quota", 507, "files.quota_bytes", id="Quota-507"),
        pytest.param("invalid", 422, "files.bad_limit", id="InvalidRequest-422"),
        pytest.param("missing_key", 428, "files.idempotency_key_required", id="MissingKey-428"),
    ],
)
async def test_files_error_classes_map_to_their_status_and_code(
    monkeypatch: pytest.MonkeyPatch, which: str, status: int, code: str
) -> None:
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        response = await client.get(f"{PREFIX}/_probe/raise/{which}")
    assert response.status_code == status
    assert response.json()["code"] == code


async def test_files_error_bodies_never_carry_a_name_or_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal that named the sibling it collided with would be an oracle for
    a node the caller may not read."""
    monkeypatch.setattr(settings, "files_enabled", True)
    async with await _harness_client(_harness()) as client:
        for which in _ERRORS:
            response = await client.get(f"{PREFIX}/_probe/raise/{which}")
            assert "secret.txt" not in response.text, which
            assert "name" not in response.json().get("detail", {}), which


def test_files_conflict_detail_is_withheld_until_the_caller_may_read_the_node() -> None:
    """The same Conflict answers with and without its detail depending on one
    fact the route resolved — not on which exception was raised."""
    error = Conflict("files.name_conflict", detail={"node_id": "n-1", "name": "secret.txt"})
    assert "detail" not in body_for(error)
    opened = body_for(error, may_read_named_node=True)
    assert opened["detail"] == {"node_id": "n-1"}


def test_files_not_found_body_is_the_same_however_it_was_raised() -> None:
    """The three not-yours classes reach the handler as this one exception, so
    proving the body is code-only here proves it for all three."""
    plain = body_for(NotFound())
    with_prose = body_for(NotFound("no node 3f2a for this caller"))
    with_detail = body_for(NotFound(detail={"node_id": "n-1"}))
    assert plain == with_prose == with_detail == {"code": "not_found"}


# --------------------------------------------------------------------------
# Router discovery
# --------------------------------------------------------------------------


def _plant_package(root: Path, name: str) -> ModuleType:
    """A throwaway package with one public family, one private module and one
    content-only module, so the walk's three rules are provable at once."""
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "items.py").write_text(
        textwrap.dedent(
            """
            from fastapi import APIRouter
            router = APIRouter()

            @router.get("/planted")
            async def planted() -> dict[str, str]:
                return {"planted": "yes"}
            """
        )
    )
    (package / "_secret.py").write_text(
        textwrap.dedent(
            """
            from fastapi import APIRouter
            router = APIRouter()

            @router.get("/secret")
            async def secret() -> dict[str, str]:
                return {"secret": "yes"}
            """
        )
    )
    (package / "content.py").write_text(
        textwrap.dedent(
            """
            from fastapi import APIRouter
            content_router = APIRouter()

            @content_router.get("/bytes")
            async def bytes_() -> dict[str, str]:
                return {"bytes": "yes"}
            """
        )
    )
    sys.path.insert(0, str(root))
    import importlib

    return importlib.import_module(name)


def test_files_router_discovery_picks_up_a_family_and_skips_a_private_module(
    tmp_path: Path,
) -> None:
    """A route lane adds a module and is done: nothing in ``__init__`` names it.
    A module starting with ``_`` is infrastructure and is never mounted."""
    planted = _plant_package(tmp_path, f"planted_files_{uuid.uuid4().hex[:8]}")
    paths = {
        route.path  # type: ignore[attr-defined]
        for router in discovered_routers(planted)
        for route in router.routes
    }
    assert paths == {"/planted"}


def test_files_content_routers_are_collected_separately(tmp_path: Path) -> None:
    """Content bytes are served from their own ASGI app on the content domain;
    mounting one under the API host would put user bytes on an origin that
    holds a session cookie."""
    planted = _plant_package(tmp_path, f"planted_content_{uuid.uuid4().hex[:8]}")
    content_paths = {
        route.path  # type: ignore[attr-defined]
        for router in discovered_content_routers(planted)
        for route in router.routes
    }
    api_paths = {
        route.path  # type: ignore[attr-defined]
        for router in discovered_routers(planted)
        for route in router.routes
    }
    assert content_paths == {"/bytes"}
    assert "/bytes" not in api_paths


def test_files_parent_router_carries_the_prefix_and_the_gate() -> None:
    parent = build_files_router()
    assert parent.prefix == PREFIX
    assert parent.dependencies, "the kill switch must sit on the parent"


# --------------------------------------------------------------------------
# to_item
# --------------------------------------------------------------------------


_ALL_ACTIONS = frozenset(action.value for action in FilesAction)


def _access(actions: frozenset[str] = _ALL_ACTIONS) -> EffectiveAccess:
    return EffectiveAccess(role="writer", allowed_actions=actions, flags=frozenset(), in_org=True)


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_builds_the_file_facet_from_the_head_version(fx) -> None:  # type: ignore[no-untyped-def]
    node = await fx.node(b"report.pdf", mode=0o644, mtime_ns=1_700_000_000_000_000_000)
    version = await fx.version(
        node, size_bytes=2048, mime_sniffed="application/pdf", content_hash="cd" * 32
    )
    chain = await _chain(fx, node)

    item = to_item(node, chain, _access(), None, version=version)

    assert item.kind == "file"
    assert item.file is not None
    assert (item.file.size, item.file.mime_type) == (2048, "application/pdf")
    assert item.file.content_hash == "cd" * 32
    assert item.attrs is not None
    assert item.attrs.mode == 0o644
    assert item.attrs.mtime == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert item.path_bytes.endswith(b"/report.pdf")
    assert item.symlink is None and item.object is None and item.lease is None


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_omits_the_file_facet_for_a_folder(fx) -> None:  # type: ignore[no-untyped-def]
    node = await fx.node(b"datasets", kind="folder")
    item = to_item(node, await _chain(fx, node), _access(), None)
    assert item.kind == "folder"
    assert item.file is None


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_renders_a_symlink_target(fx) -> None:  # type: ignore[no-untyped-def]
    node = await fx.node(
        b"lib64", kind="symlink", symlink_target=b"../lib", symlink_kind="relative"
    )
    host = await fx.node(
        b"python", kind="symlink", symlink_target=b"/usr/bin/python3", symlink_kind="host"
    )
    item = to_item(node, await _chain(fx, node), _access(), None)
    assert item.kind == "symlink"
    assert item.symlink is not None
    assert (item.symlink.target, item.symlink.kind) == ("../lib", "relative")
    # The negative twin: a host link must not read as an in-tree one, or a
    # client would rewrite `/usr/bin/python3` to a path inside the org tree.
    host_item = to_item(host, await _chain(fx, host), _access(), None)
    assert host_item.symlink is not None
    assert host_item.symlink.kind == "host"


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_renders_an_object_node(fx) -> None:  # type: ignore[no-untyped-def]
    target = uuid.uuid4()
    node = await fx.node(
        b"kickoff.alkeraquery", kind="object", subtype="query", target_object_id=target
    )
    item = to_item(node, await _chain(fx, node), _access(), None)
    assert item.kind == "object"
    assert item.object is not None
    assert (item.object.type, item.object.id) == ("query", str(target))
    assert item.object.web_url == f"/objects/{target}"


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_renders_a_chat_folder_as_the_chat(fx) -> None:  # type: ignore[no-untyped-def]
    """A chat is a folder and still carries its object facet.

    The facet is what a client routes on — open the chat rather than descend
    into it — so it has to survive the node being a folder, and it has to
    carry the chat's own address rather than the object page's.
    """
    target = uuid.uuid4()
    node = await fx.node(
        b"kickoff.alkerachat", kind="folder", subtype="chat", target_object_id=target
    )
    item = to_item(node, await _chain(fx, node), _access(), None)
    assert item.kind == "folder"
    assert item.object is not None
    assert (item.object.type, item.object.id) == ("chat", str(target))
    assert item.object.web_url == f"/chat/{target}"


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_gives_an_ordinary_folder_no_object_facet(fx) -> None:  # type: ignore[no-untyped-def]
    """The facet keys off ``target_object_id``, so a plain folder has none."""
    node = await fx.node(b"plain", kind="folder")
    item = to_item(node, await _chain(fx, node), _access(), None)
    assert item.object is None


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_renders_the_lease_and_carries_its_stale_through(fx) -> None:  # type: ignore[no-untyped-def]
    """A client must be able to explain a refusal before attempting the write,
    which is what the lease facet and ``stale`` are for.

    ``stale`` is the holder's clock, not the reader's identity: the payload
    carries the library's answer through unchanged, so a non-holder reading a
    freshly synced mount is told it is current and the holder reading its own
    lagging mount is told it is not."""
    node = await fx.node(b"experiment.py")
    now = datetime.now(tz=UTC)

    def _snapshot(*, mine: bool, stale: bool) -> LeaseSnapshot:
        return LeaseSnapshot(
            node_id=NodeId(node.id),
            holder_principal_id=uuid.uuid4(),
            machine="MacBook Pro",
            purpose="mount",
            since=now,
            expires_at=now,
            last_sync_at=now,
            mine=mine,
            stale=stale,
        )

    theirs = to_item(node, await _chain(fx, node), _access(), _snapshot(mine=False, stale=False))
    mine = to_item(node, await _chain(fx, node), _access(), _snapshot(mine=True, stale=True))
    assert theirs.lease is not None and theirs.lease.machine == "MacBook Pro"
    assert theirs.lease.mine is False
    assert theirs.stale is False
    assert mine.stale is True


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_capabilities_follow_the_access(fx) -> None:  # type: ignore[no-untyped-def]
    """The capability map is derived, never asserted by the route: a reader's
    item says why the write button is dead rather than just disabling it."""
    node = await fx.node(b"readme.md")
    chain = await _chain(fx, node)
    writer = to_item(node, chain, _access(), None)
    reader = to_item(node, chain, _access(frozenset({"read", "export"})), None)
    no_export = to_item(node, chain, _access(frozenset({"read"})), None)

    assert writer.capabilities.can_write is True
    assert reader.capabilities.can_write is False
    assert reader.capabilities.can_read is True
    assert reader.capabilities.can_download is True
    # A read without EXPORT can look but not take the bytes off the platform.
    assert no_export.capabilities.can_download is False
    assert reader.capabilities.refusals.get("write")


@pytest.mark.usefixtures("files_on")
async def test_files_to_item_reports_a_windows_unsafe_name(fx) -> None:  # type: ignore[no-untyped-def]
    """The flags are the whole reason a client can warn before a sync fails,
    and the negative twin proves the check is not "always unsafe"."""
    bad = await fx.node(b"NUL.txt")
    good = await fx.node(b"COM10.txt")
    assert to_item(bad, await _chain(fx, bad), _access(), None).name_flags.windows_safe is False
    assert to_item(good, await _chain(fx, good), _access(), None).name_flags.windows_safe is True


async def _chain(fx, node):  # type: ignore[no-untyped-def]
    async with fx.repo.transaction():
        return list(await fx.repo.chain(node))
