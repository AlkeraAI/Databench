"""What runs in a chat's sandbox besides its agent server.

The tables below are what the in-container read printed on a local developer
box under gVisor: one chat idle with a background ``sleep`` that
had already ended, one with a ``sleep 7200`` the agent started with ``nohup``.
The agent server is the container's PID 1 and does not reap the orphans it
inherits, so an ended process stays in the table as a zombie; reading it as
work would keep a chat awake for ever.

The listers are driven across the real process boundary: a fake ``runsc``
executable the probe really spawns, and a fake cgroup tree naming real
processes this test started.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import psutil
import pytest
from alkera_cli.harness.adapters.opencode_sandbox import SandboxPlan
from alkera_cli.harness.sandbox import SandboxSpec, chat_cgroup_path, chat_slice_cgroup_path
from alkera_cli.harness.sandbox_processes import (
    CONTAINER_TABLE_SCRIPT,
    RUNSC_PS_TIMEOUT_SECONDS,
    CgroupLister,
    RunscLister,
    SandboxProbes,
    SandboxProcess,
    SandboxProcessProbe,
    descendants,
    kill_tree,
    parse_container_table,
    parse_proc_stat,
    probe_for,
    runsc_kill_argv,
    work_processes,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes and scripts")

#: An idle chat: the agent server, a background sleep that already ended (a
#: zombie), and the reader itself (pid 28, named on the first line).
IDLE_WITH_ZOMBIE = textwrap.dedent(
    """\
    28
    1 (opencode) S 0 1 1 0 0 0 0 0 0 0 471 41 2 1 20 0 6 0 1 767
    22 (sleep) Z 1 21 21 0 0 0 0 0 0 0 0 0 0 0 20 0 1 0 12822 0
    28 (cat) R 0 28 28 0 0 0 0 0 0 0 0 0 0 0 20 0 1 0 19051 1116
    """
)

#: A chat whose agent left ``nohup sleep 7200 &`` running.
RUNNING_SLEEP = textwrap.dedent(
    """\
    21
    1 (opencode) S 0 1 1 0 0 0 0 0 0 0 392 19 4 0 20 0 6 0 1 766
    15 (sleep) S 1 14 14 0 0 0 0 0 0 0 1 0 0 0 20 0 1 0 437 1101
    21 (cat) R 0 21 21 0 0 0 0 0 0 0 1 0 0 0 20 0 1 0 10254 1116
    """
)


# -- parsing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            "15 (sleep) S 1 14 14 0",
            SandboxProcess(pid=15, ppid=1, command="sleep", state="S"),
            id="plain",
        ),
        pytest.param(
            "40 (my (odd) prog) R 7 40 40",
            SandboxProcess(pid=40, ppid=7, command="my (odd) prog", state="R"),
            id="a-command-with-parentheses-is-cut-at-the-last-one",
        ),
        pytest.param(
            "9 (tmux: server) Z 1 9",
            SandboxProcess(pid=9, ppid=1, command="tmux: server", state="Z"),
            id="a-command-with-a-space",
        ),
    ],
)
def test_a_stat_line_reads_as_its_process(line: str, expected: SandboxProcess) -> None:
    assert parse_proc_stat(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("", id="empty"),
        pytest.param("not a stat line", id="prose"),
        pytest.param("x (sleep) S 1", id="pid-not-a-number"),
        pytest.param("15 (sleep)", id="no-state"),
        pytest.param("15 sleep S 1", id="no-parentheses"),
    ],
)
def test_a_line_that_is_not_a_stat_line_is_refused(line: str) -> None:
    with pytest.raises(ValueError):
        parse_proc_stat(line)


def test_the_reader_is_not_the_chats_work() -> None:
    listed = parse_container_table(RUNNING_SLEEP)
    assert [p.pid for p in listed] == [1, 15]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="nothing"),
        pytest.param("1 (opencode) S 0 1 1\n", id="no-reader-line"),
        pytest.param("21\ngarbage\n", id="a-row-that-is-not-a-stat-line"),
    ],
)
def test_a_table_the_script_never_prints_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_container_table(text)


@pytest.mark.parametrize(
    ("table", "work"),
    [
        pytest.param(IDLE_WITH_ZOMBIE, [], id="an-ended-process-is-not-work"),
        pytest.param(RUNNING_SLEEP, [15], id="a-running-sleep-is-work"),
    ],
)
def test_work_is_what_runs_besides_the_agent_server(table: str, work: list[int]) -> None:
    found = work_processes(parse_container_table(table), agent_pids=frozenset({1}))
    assert [p.pid for p in found] == work


def test_the_agent_server_alone_is_not_work() -> None:
    agent = SandboxProcess(pid=1, ppid=0, command="opencode", state="S")
    assert work_processes([agent], agent_pids=frozenset({1})) == ()
    assert work_processes([agent], agent_pids=frozenset()) == (agent,)


# -- gVisor: the container's own table, through a runsc the probe spawns --------


def _fake_runsc(tmp_path: Path, *, prints: str, exit_status: int = 0) -> tuple[Path, Path]:
    """A ``runsc`` that records its argv and prints a captured table."""
    argv_file = tmp_path / "argv.txt"
    table = tmp_path / "table.txt"
    table.write_text(prints, encoding="utf-8")
    script = tmp_path / "runsc"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\ncat "{table}"\nexit {exit_status}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, argv_file


@posix_only
@pytest.mark.parametrize(
    ("table", "work"),
    [
        pytest.param(RUNNING_SLEEP, [15], id="a-background-sleep-holds-the-chat"),
        pytest.param(IDLE_WITH_ZOMBIE, [], id="an-ended-background-process-does-not"),
    ],
)
def test_the_gvisor_probe_reads_the_table_from_inside_the_container(
    tmp_path: Path, table: str, work: list[int]
) -> None:
    runsc, argv_file = _fake_runsc(tmp_path, prints=table)
    probe = SandboxProcessProbe(
        RunscLister(
            runsc=str(runsc), root="/run/alkera-runsc", container="alkera-chat-c1", uid=59998
        )
    )

    found = probe.work()

    assert found is not None and [p.pid for p in found] == work
    # Read as the chat's own uid, inside the chat's own container.
    assert argv_file.read_text(encoding="utf-8").splitlines() == [
        "--root=/run/alkera-runsc",
        "exec",
        "--user=59998:59998",
        "--env",
        "PATH=/usr/bin:/bin",
        "alkera-chat-c1",
        "/bin/sh",
        "-c",
        CONTAINER_TABLE_SCRIPT,
    ]


@posix_only
@pytest.mark.parametrize(
    ("prints", "exit_status"),
    [
        pytest.param("", 1, id="runsc-failed"),
        pytest.param("the container is gone\n", 0, id="output-that-is-not-a-table"),
    ],
)
def test_a_container_that_does_not_answer_is_unknown_not_empty(
    tmp_path: Path, prints: str, exit_status: int
) -> None:
    runsc, _ = _fake_runsc(tmp_path, prints=prints, exit_status=exit_status)
    probe = SandboxProcessProbe(
        RunscLister(runsc=str(runsc), root="/r", container="alkera-chat-c1", uid=1)
    )
    assert probe.work() is None


def _runsc_that_runs_the_command(tmp_path: Path) -> Path:
    """A ``runsc`` that drops its own flags and runs the command it was given
    on this host, the way ``runsc exec`` runs it in the container, with the
    environment it inherited (it ignores ``--env``, as a container whose
    environment the chat had shaped would)."""
    script = tmp_path / "runsc-exec"
    script.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do case "$1" in\n'
        "  /*) break ;; --env) shift 2 ;; *) shift ;;\n"
        "esac; done\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@posix_only
def test_a_cat_the_chat_put_earlier_on_the_path_is_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container is the chat's: a ``cat`` it placed on a path the probe
    searched would run as the probe and answer for it. The probe names every
    binary by its absolute path, so the planted one is never run."""
    planted = tmp_path / "planted"
    planted.mkdir()
    marker = tmp_path / "planted-cat-ran"
    (planted / "cat").write_text(
        f'#!/bin/sh\ntouch "{marker}"\necho 1\necho "4242 (evil) S 1 1"\n', encoding="utf-8"
    )
    (planted / "cat").chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}:{os.environ['PATH']}")
    lister = RunscLister(
        runsc=str(_runsc_that_runs_the_command(tmp_path)), root="/r", container="c", uid=1
    )

    work = SandboxProcessProbe(lister).work()

    assert not marker.exists(), "the planted cat ran"
    assert work is None or all(p.pid != 4242 for p in work)


@posix_only
def test_a_probe_that_prints_without_end_is_cut_and_reads_as_unknown(tmp_path: Path) -> None:
    """Output past the cap kills the probe and counts as a failed read; it
    neither hangs the box nor fills its memory."""
    runsc = tmp_path / "runsc"
    runsc.write_text('#!/bin/sh\necho 1\nexec yes "1 (opencode) S 0 1 1"\n', encoding="utf-8")
    runsc.chmod(0o755)
    started = time.monotonic()
    lister = RunscLister(runsc=str(runsc), root="/r", container="c", uid=1)
    assert SandboxProcessProbe(lister).work() is None
    assert time.monotonic() - started < RUNSC_PS_TIMEOUT_SECONDS


def test_a_runsc_that_is_not_there_is_unknown(tmp_path: Path) -> None:
    probe = SandboxProcessProbe(
        RunscLister(runsc=str(tmp_path / "no-runsc"), root="/r", container="c", uid=1)
    )
    assert probe.work() is None


# -- a box with a per-chat cgroup: real processes, a fake cgroup tree ----------


@pytest.fixture
def spawned() -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _children_of(pid: int) -> list[int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = [child.pid for child in psutil.Process(pid).children()]
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError(f"{pid} started no child")


def _cgroup(tmp_path: Path, *groups: list[int]) -> Path:
    """A cgroup tree with one ``cgroup.procs`` per nested group, the shape a
    systemd slice with a scope under it has."""
    root = tmp_path / "alkera-chat-c1.slice"
    where = root
    for pids in groups:
        where.mkdir(parents=True, exist_ok=True)
        (where / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids), encoding="ascii")
        where = where / "child.scope"
    return root


@posix_only
def test_a_process_left_in_the_chats_cgroup_is_work_and_the_launch_chain_is_not(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    # The launcher stays alive as the agent's parent (``sh -c 'sleep; true'``
    # does not exec), like a cgroup placement that did not exec in place.
    launcher = subprocess.Popen(["/bin/sh", "-c", "sleep 60; true"])
    spawned.append(launcher)
    (agent,) = _children_of(launcher.pid)
    left_behind = subprocess.Popen(["sleep", "60"])
    spawned.append(left_behind)
    gone = subprocess.Popen(["true"])
    gone.wait()

    root = _cgroup(tmp_path, [launcher.pid], [agent, left_behind.pid, gone.pid])
    work = SandboxProcessProbe(CgroupLister(root, agent)).work()

    assert work is not None
    assert [p.pid for p in work] == [left_behind.pid]

    left_behind.kill()
    left_behind.wait()
    assert SandboxProcessProbe(CgroupLister(root, agent)).work() == ()


@posix_only
def test_a_zombie_in_the_chats_cgroup_is_not_work(
    tmp_path: Path, spawned: list[subprocess.Popen[bytes]]
) -> None:
    ended = subprocess.Popen(["true"])
    spawned.append(ended)
    deadline = time.monotonic() + 5
    while psutil.Process(ended.pid).status() != psutil.STATUS_ZOMBIE:
        assert time.monotonic() < deadline, "the child never ended"
        time.sleep(0.02)

    root = _cgroup(tmp_path, [os.getpid(), ended.pid])
    assert SandboxProcessProbe(CgroupLister(root, os.getpid())).work() == ()


def test_a_chat_without_a_cgroup_on_this_host_is_unknown(tmp_path: Path) -> None:
    assert SandboxProcessProbe(CgroupLister(tmp_path / "absent", 1)).work() is None


# -- which probe a launch gets --------------------------------------------------


def _spec(tmp_path: Path, cgroup: str) -> SandboxSpec:
    return SandboxSpec(
        chat_id="chat-1",
        folder=tmp_path,
        uid=59990,
        vcpu=1,
        memory_mb=2048,
        home="/home/alkera",
        mode="gvisor",
        cgroup=cgroup,  # type: ignore[arg-type]
        runsc="/usr/bin/runsc",
        runsc_root=Path("/run/alkera-runsc"),
        cgroup_root=tmp_path / "cg",
    )


def test_a_gvisor_chat_is_probed_inside_its_container(tmp_path: Path) -> None:
    probe = probe_for("gvisor", _spec(tmp_path, "systemd"), 4242)
    assert probe is not None and isinstance(probe.lister, RunscLister)
    assert probe.lister.uid == 59990
    assert probe.lister.container.startswith("alkera-chat-")
    assert probe.lister.agent_pids == frozenset({1})


@pytest.mark.parametrize(
    ("cgroup", "where"),
    [
        pytest.param("systemd", chat_slice_cgroup_path, id="systemd-slice"),
        pytest.param("cgroupfs", chat_cgroup_path, id="cgroupfs-group"),
    ],
)
def test_a_chat_with_a_cgroup_is_probed_through_it(
    tmp_path: Path, cgroup: str, where: object
) -> None:
    spec = _spec(tmp_path, cgroup)
    probe = probe_for("none", spec, 4242)
    assert probe is not None and isinstance(probe.lister, CgroupLister)
    assert probe.lister.cgroup == where("chat-1", spec.cgroup_root)  # type: ignore[operator]
    assert probe.lister.agent_pid == 4242


def test_a_chat_with_no_cgroup_has_no_probe(tmp_path: Path) -> None:
    assert probe_for("none", _spec(tmp_path, "none"), 4242) is None


# -- the registry the box reads, by session id ----------------------------------


@posix_only
def test_a_registry_reads_a_sessions_sandbox_by_its_id_until_it_is_dropped(
    tmp_path: Path,
) -> None:
    """The agent server's launch records its sandbox's probe under the session
    id; whoever holds the registry reads it by the same id, and a session that
    was dropped (or never recorded) reads as nothing to tell."""
    runsc, _ = _fake_runsc(tmp_path, prints=RUNNING_SLEEP)
    probe = SandboxProcessProbe(
        RunscLister(runsc=str(runsc), root="/r", container="alkera-chat-c1", uid=1)
    )
    probes, other = SandboxProbes(), SandboxProbes()
    assert probes.work("chat-c1") is None, "no agent running: nothing to read"

    probes.register("chat-c1", probe)
    work = probes.work("chat-c1")
    assert work is not None and [p.pid for p in work] == [15]
    assert probes.work("another-chat") is None
    assert other.work("chat-c1") is None, "a registry is its own, not the process's"

    probes.drop("chat-c1")
    assert probes.work("chat-c1") is None


def test_the_plan_gives_no_probe_to_an_unsandboxed_session() -> None:
    assert SandboxPlan.probe_of(None, 4242) is None


# -- ending a command's tree inside the container ---------------------------------


class _Container:
    """A container's process table as ``runsc`` would answer for it: the
    table script prints every live process, ``kill --pid`` ends one (a
    process in ``stubborn`` ignores ``SIGTERM`` and dies to ``SIGKILL`` only),
    and every kill is recorded with its argv."""

    def __init__(
        self,
        tree: dict[int, int],
        *,
        stubborn: frozenset[int] = frozenset(),
        unreaped: frozenset[int] = frozenset(),
    ) -> None:
        self.alive = dict(tree)  # pid -> ppid
        self.stubborn = stubborn
        #: Processes whose parent is the agent server, which reaps nothing: a
        #: kill leaves them in the table as zombies rather than removing them.
        self.unreaped = unreaped
        self.zombies: set[int] = set()
        self.kills: list[tuple[int, str]] = []
        self.argvs: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> tuple[int, str]:
        self.argvs.append(tuple(argv))
        if "kill" in argv:
            pid = int(next(a for a in argv if a.startswith("--pid=")).removeprefix("--pid="))
            self.kills.append((pid, argv[-1]))
            if pid in self.alive and (argv[-1] == "SIGKILL" or pid not in self.stubborn):
                if pid in self.unreaped:
                    self.zombies.add(pid)
                else:
                    del self.alive[pid]
            return 0, ""
        lines = ["99"]
        lines.extend(
            f"{pid} (p{pid}) {'Z' if pid in self.zombies else 'S'} {ppid} {pid} {pid} "
            "0 0 0 0 0 0 0 1 0 0 0 20 0 1 0 1 1"
            for pid, ppid in self.alive.items()
        )
        return 0, "\n".join(lines) + "\n"


def _lister(container: _Container) -> RunscLister:
    return RunscLister(
        runsc="/usr/bin/runsc", root="/r", container="c", uid=5, runner=container.run
    )


#: The agent server (1), a timed-out ``sh`` (40) with a ``sleep`` (41) and a
#: ``python`` (42) under it that forked a worker (43), a background job of an
#: earlier command (30) and its child (31), a zombie reaper under the agent.
_TABLE = {1: 0, 30: 1, 31: 30, 40: 1, 41: 40, 42: 40, 43: 42}


def test_descendants_are_the_pid_and_everything_below_it_parents_first() -> None:
    table = parse_container_table(_Container(_TABLE).run(("ps",))[1])
    assert descendants(table, 40) == (40, 41, 42, 43)
    assert descendants(table, 42) == (42, 43)
    assert descendants(table, 30) == (30, 31)
    assert descendants(table, 7) == (7,), "a pid the table lacks is only itself"


def test_the_kill_argv_names_the_container_and_the_one_pid() -> None:
    assert runsc_kill_argv(_lister(_Container({})), 41, "SIGTERM") == (
        "/usr/bin/runsc",
        "--root=/r",
        "kill",
        "--pid=41",
        "c",
        "SIGTERM",
    )


def test_a_killed_command_leaves_nothing_of_its_tree_and_nothing_else_is_touched() -> None:
    """Every process below the command gets TERM; nothing survives; the agent
    server and another command's background job are not signalled."""
    container = _Container(_TABLE)
    slept: list[float] = []
    left = kill_tree(_lister(container), 40, grace=2.5, sleep=slept.append)
    assert left == ()
    assert sorted(container.alive) == [1, 30, 31]
    assert container.kills == [(40, "SIGTERM"), (41, "SIGTERM"), (42, "SIGTERM"), (43, "SIGTERM")]
    assert slept == [2.5]


def test_a_process_that_ignores_term_is_killed_after_the_grace() -> None:
    container = _Container(_TABLE, stubborn=frozenset({43}))
    left = kill_tree(_lister(container), 40, grace=0.1, sleep=lambda _s: None)
    assert left == () and 43 not in container.alive
    assert container.kills[-1] == (43, "SIGKILL")
    assert [k for k in container.kills if k[1] == "SIGKILL"] == [(43, "SIGKILL")]
    assert sorted(container.alive) == [1, 30, 31]


def test_a_killed_process_the_agent_server_has_not_reaped_is_not_a_survivor() -> None:
    """A child whose parent died first is reparented to the agent server,
    which reaps nothing: after the kill it stays in the table as a zombie.
    Dead is dead; it gets no second signal and is not reported as left."""
    container = _Container(_TABLE, stubborn=frozenset({43}), unreaped=frozenset({41, 42, 43}))
    left = kill_tree(_lister(container), 40, grace=0.1, sleep=lambda _s: None)
    assert left == ()
    assert container.zombies == {41, 42, 43}
    assert container.kills == [
        (40, "SIGTERM"),
        (41, "SIGTERM"),
        (42, "SIGTERM"),
        (43, "SIGTERM"),
        (43, "SIGKILL"),
    ]


def test_a_tree_that_cannot_be_read_is_not_guessed_at() -> None:
    """A container that does not answer gets no blind kills: the pids are
    unknown, and the agent server's is 1."""
    container = _Container(_TABLE)

    def refusing(argv: Sequence[str]) -> tuple[int, str]:
        if "kill" in argv:
            return container.run(argv)
        return 1, ""

    lister = RunscLister(runsc="/usr/bin/runsc", root="/r", container="c", uid=5, runner=refusing)
    assert kill_tree(lister, 40, grace=0.1, sleep=lambda _s: None) == ()
    assert container.kills == []


def test_a_command_already_gone_needs_no_kill() -> None:
    container = _Container({1: 0, 30: 1})
    assert kill_tree(_lister(container), 40, grace=0.1, sleep=lambda _s: None) == ()
    assert container.kills == []
