"""The box supervisor: the one root process on a box, holding no tenant data.

It claims the machine, reads which orgs have chats on the box, gives each org
a slot (a uid/gid range, a data root, a network), starts one worker process
per org inside that org's user, mount, network and cgroup namespaces, and
hands each worker its chats over a socketpair. A worker serves one org and
nothing else; a bug inside it meets ``EACCES`` on every other org's data, no
route to any other org's sockets, and no other org's processes in its
``/proc``.

The package is the box's trusted base, so it stays small and imports nothing
that touches tenant content (no harness, plugins, files, context, lineage,
cloud sync or mirror code): ``apps/cli/tests/supervisor/test_supervisor_minimal.py``
holds it to that.
"""

from collections.abc import Mapping, Sequence

from alkera_core.compute.box_contract import START_COMMAND, START_MODE_ENV, StartMode

#: The argv that selects the supervisor; the CLI entry hands it over before
#: anything of the command tree is imported (``alkera_cli/__main__.py``).
COMMAND = ("cloud-mirror", "supervise")


def supervisor_args(argv: Sequence[str], env: Mapping[str, str]) -> list[str] | None:
    """The supervisor's arguments when this process is the box supervisor,
    else ``None``.

    It is when the command says so, and when the box's one start command
    (which every build has, so a unit that names it survives an upgrade and a
    rollback alike) runs on a node whose environment says to supervise. That
    start command takes no arguments from the bootstrap; one given any runs as
    written."""
    if tuple(argv[1 : 1 + len(COMMAND)]) == COMMAND:
        return list(argv[1 + len(COMMAND) :])
    if list(argv[1:]) == START_COMMAND.split() and env.get(START_MODE_ENV) == StartMode.SUPERVISE:
        return []
    return None
