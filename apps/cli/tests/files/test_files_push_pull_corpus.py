"""The POSIX round-trip corpus, pushed and pulled against a real backend.

The claim under test is the fidelity bar itself: anything a POSIX tree can
contain round-trips byte- and metadata-identical through Files, except a short
list of typed exceptions that are asserted *exactly* — a new kind of divergence
fails even though a known one does not.

Nothing here inspects the push or the pull. The corpus is built by
``corpus/build.py``, the verdict comes from ``corpus/compare.py`` (which walks
both trees with ``os.lstat`` and hashes with the stdlib, sharing no code with
the subject), and the git claims come from ``git`` itself.

Each test boots its own backend (two uvicorn servers, a seeded principal, a
fresh pool) and the module is marked ``spread``: no test reads server state
another one left behind, each pushes to a folder named after itself, and a
push is four hundred real requests, so letting the round-trip tests land on
different workers makes the module's wall time its slowest test rather than
their sum.

The corpus stays per test, and it is built beside the directory the pull
materializes into — ``corpus`` beside ``pulled``, ``root-a`` beside ``root-b``.
That is not cosmetic: the deep path grows until the platform's own PATH_MAX
stops it (1,024 bytes on macOS), so a corpus built at a shorter path than the
root a test pulls into holds a leaf that cannot be written there, and the round
trip fails on the filesystem rather than on anything Files did.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.commands.files import FILES_TRANSFER_TIMEOUT
from alkera_cli.files.push import push, push_paths
from alkera_core.config import settings
from alkera_sdk import AlkeraClient
from files._live_backend import LiveBackend, home_path, live_backend
from files._round_trip import pull_back, push_settled, settle
from files.corpus.build import (
    DEEP_SEGMENT,
    GIT_REPO,
    PULL_SIDECAR,
    Corpus,
    build_corpus,
    git_available,
)
from files.corpus.compare import DiffKind, Excuse, compare, walk_tree

pytestmark = [
    # Every case boots its own backend and pushes to a folder named after itself,
    # so nothing ties one case to another. The name round trip, which IS built once
    # for a whole module, lives in test_files_push_pull_names.py for that reason.
    pytest.mark.spread,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "the corpus needs byte-exact names, symlink loops, FIFO refusals and "
            "2,000-byte paths the Windows runner does not offer"
        ),
    ),
]

#: The classes of divergence the round trip is documented to allow. Anything
#: outside this set is a failure, and a member that never occurs is not
#: asserted present — the platform decides which of them are reachable.
_ALLOWED = {
    Excuse.POINTER,
    Excuse.SIDECAR,
    Excuse.SPECIAL,
    Excuse.NON_UTF8_NAME,
    Excuse.HARDLINK_TOPOLOGY,
    Excuse.CANONICAL_LINK,
    Excuse.LINK_OUTSIDE_TREE,
    Excuse.XATTR,
}


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[LiveBackend]:
    """The real backend, with the per-principal rate limiter off.

    The corpus is a few hundred nodes pushed and pulled back to back by one
    principal, which is exactly the shape the production limiter exists to
    refuse. Throttling is proven where it belongs — the route tests that assert
    the 429 — and leaving it on here would only measure the limiter's clock.

    One server per test, on purpose: a boot costs two seconds, while a push is
    four hundred real requests over the socket, so sharing the server across the
    module would pin every one of those pushes to one xdist worker. Kept
    per test, the module's wall time is its slowest test, not their sum.
    """
    previous = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    try:
        with live_backend(tmp_path) as running:
            yield running
    finally:
        settings.rate_limit_enabled = previous


@pytest.fixture
def client(backend: LiveBackend) -> Iterator[AlkeraClient]:
    with AlkeraClient(
        base_url=backend.base_url, token=backend.token, timeout=FILES_TRANSFER_TIMEOUT
    ) as api:
        yield api


def _corpus(tmp_path: Path, name: str = "corpus", pulled: str = "pulled") -> Corpus:
    """Build the corpus under ``name``, budgeted for the root it is pulled under.

    Both roots are named here because the deep path has to fit under the
    longer of them: the tree is built once and written again by the pull, and
    a chain measured against the build root alone overflows PATH_MAX on the
    way back.
    """
    return build_corpus(tmp_path / name, pulled_under=tmp_path / pulled)


@pytest.fixture
def dest_name(request: pytest.FixtureRequest) -> str:
    """The folder this test pushes into, named after the test.

    The settle below waits for the exact number of nodes one push reported, so
    the destination has to be a tree only this test writes; naming it after the
    test keeps that true whichever server it lands on.
    """
    return str(request.node.name)


def test_an_empty_file_lands_and_comes_back_empty(
    backend: LiveBackend, client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """A zero-byte file — a package's ``__init__.py``, an empty log — is one
    empty part on the wire and one empty version on the drive. The push used
    to open a session for it, send nothing, and ask for a commit the drive
    refused (``files.parts_mismatch``), which took the whole tree down with it."""
    root = tmp_path / "empty-corpus"
    for relative, payload in {
        "pkg/__init__.py": b"",
        "empty.log": b"",
        "note.txt": b"three\n",
    }.items():
        leaf = root / relative
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(payload)

    summary = push_settled(client, root, dest_name, tmp_path / "home")

    assert summary.failed == [] and summary.warnings == []
    assert summary.uploaded == 3
    pulled = tmp_path / "pulled-empty"
    pull_back(client, pulled, dest_name)
    assert (pulled / "pkg" / "__init__.py").read_bytes() == b""
    assert (pulled / "empty.log").read_bytes() == b""
    assert (pulled / "note.txt").read_bytes() == b"three\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


def test_the_corpus_round_trips_with_only_the_typed_exceptions(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """Push the corpus, pull it into a fresh directory, and compare both trees.

    The assertion is two-sided: no difference outside the documented classes,
    and every class that *was* excused is one of them. A regression that lost
    a mode bit, an mtime or a link target shows up as a `Difference`, not as a
    silently widened exception list.
    """
    source = _corpus(tmp_path)
    push_settled(client, source.root, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)

    diff = compare(source.root, destination, excuse_xattrs=True)
    assert not diff.differences, diff.report()
    assert diff.kinds <= _ALLOWED, diff.kinds - _ALLOWED


def test_the_corpus_round_trips_when_every_path_is_named_one_by_one(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """The same fidelity bar for the entry point a live holder uses.

    A holder pushes the paths its watcher reported rather than the tree, so the
    narrow entry point has to carry every shape the corpus does — a link, a
    special, a name that is not UTF-8, an empty directory, a deep path. The
    paths are named from ``walk_tree``, which shares no code with the push, and
    the verdict is the same two-sided comparison.
    """
    source = _corpus(tmp_path)
    dest = home_path(client, dest_name)
    named = [Path(os.fsdecode(relative)) for relative in walk_tree(source.root)]
    summary = push_paths(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=source.root,
        dest=dest,
        home=tmp_path / "home",
        paths=named,
        respect_gitignore=False,
    )
    settle(client, dest, summary)

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)

    diff = compare(source.root, destination, excuse_xattrs=True)
    assert not diff.differences, diff.report()
    assert diff.kinds <= _ALLOWED, diff.kinds - _ALLOWED


def test_a_second_pull_moves_no_bytes(
    client: AlkeraClient, backend: LiveBackend, dest_name: str, tmp_path: Path
) -> None:
    """A pull onto an already-materialized tree requests no content at all.

    Idempotence is only meaningful as a count the *server* saw: the pull hashes
    what is on disk before it asks for anything, so the second run's content
    requests are zero rather than "few".
    """
    source = _corpus(tmp_path)
    push_settled(client, source.root, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)
    before = walk_tree(destination)

    backend.log.clear()
    pull_back(client, destination, dest_name)

    assert backend.log.count("/content") == 0
    after = walk_tree(destination)
    assert {path: fact.digest for path, fact in after.items()} == {
        path: fact.digest for path, fact in before.items()
    }


def test_a_second_pull_leaves_an_unchanged_symlink_alone(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """Re-pulling a tree that already holds its symlinks is a no-op, not a refusal.

    Containment refuses a path whose own last component is a symlink — that is
    what stops a link the pull just wrote from becoming a door out of the root.
    A pull that re-queues every link unconditionally therefore walks into its
    own defence the second time it runs, and the whole tree fails on the first
    link rather than on anything about the link.

    The claim is the pull's stated idempotence, read off the disk: every link
    still says exactly what it said, and none of them was replaced.
    """
    source = _corpus(tmp_path)
    push_settled(client, source.root, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)
    links = sorted(path for path in destination.rglob("*") if path.is_symlink())
    assert links, "the corpus pulled no symlinks at all"
    before = {path: (os.readlink(path), path.lstat().st_ino) for path in links}

    pull_back(client, destination, dest_name)

    assert {path: (os.readlink(path), path.lstat().st_ino) for path in links} == before


def test_a_canonical_link_made_under_one_root_resolves_under_another(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """A link into the org tree points at the right file on the next machine.

    The corpus's canonical link is written as an absolute path under root A. It
    must come back under root B as an absolute path under *B* — rewritten
    lexically, not resolved — and must open the file B holds, not a path that
    only exists on A.
    """
    source = _corpus(tmp_path, "root-a", "root-b")
    push_settled(client, source.root, dest_name, tmp_path / "home")

    other = tmp_path / "root-b"
    pull_back(client, other, dest_name)

    link = other / source.canonical_link
    target = os.readlink(link)
    assert target == os.fsdecode(other / "data" / "notes.txt")
    assert link.resolve().read_bytes() == (source.root / "data" / "notes.txt").read_bytes()
    # The link is not merely valid — it is anchored at B, so nothing about A
    # survives into the target text.
    assert os.fsdecode(source.root) not in target


def test_a_host_link_is_reported_and_never_reaches_another_machine(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """An absolute target outside the root is a door out of whatever tree a
    pull puts it in: the drive refuses it, so the push leaves it behind with a
    warning that names it, and the rest of the tree travels. The relative link
    beside it, which stays inside, arrives as written."""
    source = _corpus(tmp_path, "root-a", "root-b")
    summary = push_settled(client, source.root, dest_name, tmp_path / "home")

    other = tmp_path / "root-b"
    pull_back(client, other, dest_name)

    refused = "was not pushed: it points outside the folder being pushed"
    assert [w.endswith(f"links/host {refused}") for w in summary.warnings if refused in w] == [True]
    assert not os.path.lexists(other / "links" / "host")
    assert os.readlink(other / "links" / "relative") == "../data/notes.txt"


@pytest.mark.skipif(not git_available(), reason="git is not on PATH")
def test_the_pulled_repository_is_clean_and_passes_fsck(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """git itself is the oracle for the repository claim.

    A lost mode bit on a hook, a symlink materialized as its target's bytes, a
    reset mtime or a corrupted packfile each show up here: `status` stops being
    empty, or `fsck` stops exiting zero.
    """
    source = _corpus(tmp_path)
    push_settled(client, source.root, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)

    repo = destination / GIT_REPO
    status = _git(repo, "status", "--porcelain")
    assert status.returncode == 0, status.stderr.decode()
    # ``build/`` and ``ignored.log`` are in the repo's own .gitignore, so a
    # clean status is the real claim rather than an artefact of what was sent.
    assert status.stdout == b"", status.stdout.decode()

    fsck = _git(repo, "fsck", "--strict")
    assert fsck.returncode == 0, fsck.stderr.decode()

    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.stat().st_mode & 0o111, "the user's own hook lost its execute bits"


def test_a_gitignored_path_is_folded_when_the_repository_is_the_push_root(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """`--respect-gitignore` bites in a working tree, and only there.

    Pushed from the repository root the ignored paths never leave the machine;
    the tracked ones still do. Asserting both directions is what stops the
    matcher from folding everything.
    """
    if not git_available():
        pytest.skip("git is not on PATH")

    source = _corpus(tmp_path)

    repo_dest = home_path(client, dest_name)
    summary = push(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=source.root / GIT_REPO,
        dest=repo_dest,
        home=tmp_path / "home",
        respect_gitignore=True,
    )
    settle(client, repo_dest, summary)

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)

    assert not (destination / "build").exists()
    assert not (destination / "ignored.log").exists()
    assert (destination / "src" / "main.py").read_bytes() == b"print('hello')\n"


def test_an_old_mtime_and_an_execute_bit_survive_the_trip(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """The two attributes everything downstream depends on, asserted directly.

    A 1999 mtime is what proves the restore is the pushed value rather than
    "now", and the execute bit is what proves mode travels at all — both are
    also covered by `compare`, and are spelled out here so a failure names the
    property instead of a path.
    """
    source = _corpus(tmp_path)
    original = (source.root / "data" / "old.txt").lstat().st_mtime_ns
    push_settled(client, source.root, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)

    assert (destination / "data" / "old.txt").lstat().st_mtime_ns == original
    assert (destination / "data" / "run.sh").stat().st_mode & 0o111 == 0o111
    assert (destination / "data" / "notes.txt").stat().st_mode & 0o111 == 0


def test_a_dangling_and_a_looping_link_come_back_as_links(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """Neither is followed, on either side of the trip.

    A pull that resolved targets would hang on the loop or fail on the dangling
    link; one that dropped them would lose data the user can see.
    """
    source = _corpus(tmp_path)
    push_settled(client, source.root, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)

    links = destination / "links"
    assert os.readlink(links / "dangling") == "../data/gone.txt"
    assert not (links / "dangling").exists()
    assert os.readlink(links / "loop-a") == "loop-b"
    assert os.readlink(links / "loop-b") == "loop-a"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "a 2,000-byte relative path needs Windows long-path support, which the runner "
        "does not enable; the upload of the deep leaf never completes there"
    ),
)
def test_the_empty_directories_and_the_deep_path_arrive(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """An empty folder is a node, and a 2,000-byte path is not truncated."""
    source = _corpus(tmp_path)
    deepest = source.deep_path
    push_settled(client, source.root, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    pull_back(client, destination, dest_name)

    assert (destination / "empty").is_dir()
    assert not any((destination / "empty").iterdir())
    assert (destination / "nested" / "deep" / "empty").is_dir()
    assert len(deepest) > 260
    assert Path(os.fsdecode(os.fsencode(destination) + b"/" + deepest)).exists()


def test_the_deep_path_fits_where_the_pull_will_write_it(tmp_path: Path) -> None:
    """The corpus budgets its deepest path for the destination, not for itself.

    A pull streams every file into a ``.alkera-part`` sidecar beside its
    target and promotes it once the hash matches, so the longest path a round
    trip creates is the pulled leaf plus that suffix — under a root that need
    not be as short as the one the corpus was built in. A chain grown to fit
    only where it was made is unwritable where it is going, and every case
    that pulls the corpus then dies with ENAMETOOLONG.

    The claim is made by writing the bytes rather than by measuring. The
    destination root here is more than a whole segment plus a sidecar longer
    than the source, so on a filesystem with macOS's 1,024-byte PATH_MAX an
    unbudgeted chain cannot land whatever the root's length happens to be.
    Linux's 4,096 bytes are never reached by the 2,000-byte target, so the
    budget does not bind there and the corpus is byte-for-byte what it was.
    """
    source = tmp_path / "corpus"
    destination = tmp_path / ("pulled-" + "x" * 60)
    slack = len(os.fsencode(destination)) - len(os.fsencode(source))
    assert slack > len(os.fsencode("/" + DEEP_SEGMENT)) + len(PULL_SIDECAR), (
        "the destination root is too close to the source to force the cap"
    )

    corpus = build_corpus(source, pulled_under=destination)

    assert len(corpus.deep_path) > 260
    pulled = os.fsencode(destination) + b"/" + corpus.deep_path
    part = Path(os.fsdecode(pulled + PULL_SIDECAR))
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"deep\n")
    os.replace(part, Path(os.fsdecode(pulled)))
    assert Path(os.fsdecode(pulled)).read_bytes() == b"deep\n"


def test_a_difference_the_exceptions_do_not_cover_is_reported(tmp_path: Path) -> None:
    """The comparator itself must be able to fail.

    Without this, every green round-trip above could be a comparator that
    reports nothing. A single flipped byte in an ordinary file is the smallest
    thing the corpus claims to catch.
    """
    left = build_corpus(tmp_path / "left").root
    right = tmp_path / "right"
    right.mkdir()
    (right / "data").mkdir()
    (right / "data" / "notes.txt").write_bytes(b"different bytes\n")

    diff = compare(left, right, excuse_xattrs=True)

    assert diff
    assert any(
        difference.kind is DiffKind.CONTENT and difference.relative == b"data/notes.txt"
        for difference in diff.differences
    ), diff.report()


def test_a_fifo_beside_a_sparse_file_does_not_take_the_whole_pull_down(
    client: AlkeraClient, dest_name: str, tmp_path: Path
) -> None:
    """A special node is materialized as one; it is never asked for bytes.

    A fifo is stored as a `special` node with no version at all, so a pull that
    routes it to the content route gets a 404 and every file queued behind it
    is lost with it. The sparse file is the other half of the same tree on
    purpose: it is the one file large enough to travel the upload session, and
    its bytes — a hole plus a tail — must come back identical, which is what
    separates "the special is handled" from "the pull stopped asking for
    content".
    """
    if not hasattr(os, "mkfifo"):
        pytest.skip("this platform has no fifos")
    source = tmp_path / "corpus"
    (source / "data").mkdir(parents=True)
    sparse = source / "data" / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.truncate(1 << 20)
        handle.seek(1 << 20)
        handle.write(b"tail\n")
    os.mkfifo(source / "data" / "pipe")

    push_settled(client, source, dest_name, tmp_path / "home")

    destination = tmp_path / "pulled"
    summary = pull_back(client, destination, dest_name)

    assert (destination / "data" / "sparse.bin").read_bytes() == sparse.read_bytes()
    assert stat.S_ISFIFO((destination / "data" / "pipe").lstat().st_mode)
    assert getattr(summary, "specials", None) == 1


@pytest.mark.skipif(not git_available(), reason="git is not on PATH")
def test_the_corpus_repository_is_already_clean_before_it_is_pushed(tmp_path: Path) -> None:
    """The round-trip claim is only about the transfer, so the source must be clean.

    The case-colliding pair is the trap: written after the commit it is untracked,
    which a case-INSENSITIVE filesystem hides (the two names collapse onto one inode,
    so one write simply overwrites the other) but a case-sensitive one does not — it
    keeps both, and the status of the pulled clone is then dirty through no fault of
    the push. This builds the repository and asks git itself, so the case-sensitive
    expectation is pinned on either kind of filesystem rather than only where the CI
    runner happens to live.
    """
    corpus = build_corpus(tmp_path / "corpus")
    repo = corpus.root / GIT_REPO

    porcelain = _git(repo, "status", "--porcelain")
    assert porcelain.returncode == 0, porcelain.stderr.decode()
    assert porcelain.stdout == b"", porcelain.stdout.decode()

    tracked = set(_git(repo, "ls-files", "--", "src").stdout.split())
    if corpus.case_sensitive:
        assert {b"src/Case.txt", b"src/case.txt"} <= tracked
        assert (repo / "src" / "Case.txt").read_bytes() == b"upper\n"
        assert (repo / "src" / "case.txt").read_bytes() == b"lower\n"
    else:
        # One inode under two spellings: exactly one name is in the index, and the
        # corpus says so out loud rather than pretending it built a pair.
        assert "case-colliding pair" in corpus.unsupported
        assert len({b"src/Case.txt", b"src/case.txt"} & tracked) == 1
