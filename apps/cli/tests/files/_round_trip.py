"""Push a tree into a live backend, wait for it to land, and pull it back.

Shared by the two round-trip modules — the POSIX corpus and the name corpus —
which are separate files so the corpus cases (one backend per test) can spread
across xdist workers while the name cases (one backend for the whole module)
stay together on one.
"""

from __future__ import annotations

import time
from pathlib import Path

from alkera_cli.files.pull import pull
from alkera_cli.files.push import PushSummary, push
from alkera_sdk import AlkeraClient
from files._live_backend import home_path


def push_settled(client: AlkeraClient, root: Path, name: str, home: Path) -> PushSummary:
    """Push the tree to ``name`` in the caller's home, and wait for it to settle.

    The folder goes under the home rather than straight at the drive root: the
    root is a signpost that refuses a create under it outright, so a corpus
    aimed there never lands a single node.
    """
    dest = home_path(client, name)
    summary = push(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=root,
        dest=dest,
        home=home,
        respect_gitignore=False,
    )
    settle(client, dest, summary)
    return summary


def pull_back(client: AlkeraClient, root: Path, name: str, **kwargs: object) -> object:
    """Pull back what :func:`push_settled` put at ``name`` in the caller's home,
    as a person pulls their own drive: every link written as it is."""
    return pull(
        files=client.files,
        http=client.raw_client.get_httpx_client(),
        root=root,
        source=home_path(client, name),
        **kwargs,  # type: ignore[arg-type]
    )


def expected_nodes(summary: PushSummary) -> int:
    """How many nodes the push says it put under the destination.

    Every node a push makes is counted once: the skeleton's folders, the files
    whose bytes moved, the files the server already held at that path, and the
    links and specials recorded without any content. What the push skipped is
    deliberately absent — a sidecar, a pointer and a name that is not UTF-8
    never become nodes, so counting them would leave the wait below waiting for
    something nobody is going to create.
    """
    return (
        summary.folders + summary.uploaded + summary.unchanged + summary.symlinks + summary.specials
    )


def settle(client: AlkeraClient, dest: str, summary: PushSummary, *, seconds: float = 30.0) -> int:
    """Wait until the pushed subtree holds every node the push reported.

    An upload's commit is an *operation*: `complete_upload` returns `202` and
    the file node appears when the server finishes it. The push waits five
    seconds per file as a courtesy and gives up silently, so a corpus of a few
    hundred nodes can finish pushing before the last commit lands. Pulling at
    that moment reads a half-built tree and reports every un-committed file as
    missing — which says nothing about the pull.

    The push counted what it sent, so the target is exact rather than a guess
    at when growth has stopped: the walk reaches that number, or this fails
    saying how far short it stopped. A tree missing a node is caught here,
    where the count names it, instead of downstream as a pull that "lost" a
    file — and a tree holding MORE than the push reported is caught too.
    """
    expected = expected_nodes(summary)
    drive_id = str(client.files.drive()["id"])
    dest_id = str(client.files.item_by_path(drive_id, dest)["id"])
    deadline = time.monotonic() + seconds
    while True:
        count = _count(client, drive_id, dest_id)
        if count == expected:
            return count
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{dest} holds {count} nodes after {seconds:g}s; the push reported {expected}"
            )
        time.sleep(0.1)


def _count(client: AlkeraClient, drive_id: str, item_id: str) -> int:
    total = 0
    for child in client.files.children(drive_id, item_id):
        total += 1
        if child.get("kind") == "folder":
            total += _count(client, drive_id, str(child["id"]))
    return total
