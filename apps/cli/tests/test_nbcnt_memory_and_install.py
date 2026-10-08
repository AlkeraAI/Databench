"""The kernel sandbox's memory source (its host cgroup, read from fixture files)
and ``system.install`` (the exact command, the capability list, name checks)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _nbcnt_fakes import Rig, make_rig
from alkera_cli.harness.sandbox import CONTAINER_ENV
from alkera_cli.notebooks import memory_cgroup as mc
from alkera_cli.notebooks import system_install as si


class View:
    """The sandbox as the source reads it: a cgroup directory (or none) and
    the OOM count the sandbox carries."""

    def __init__(self, cgroup: Path | None, ooms: int = 0) -> None:
        self.cgroup = cgroup
        self.ooms = ooms

    def cgroup_dir(self) -> Path | None:
        return self.cgroup

    def oom_events(self) -> int:
        return self.ooms


def cgroup(tmp_path: Path, **files: str) -> Path:
    where = tmp_path / "cg"
    where.mkdir(exist_ok=True)
    for name, text in files.items():
        (where / name.replace("_", ".", 1)).write_text(text)
    return where


@pytest.mark.parametrize(
    ("files", "usage", "limit"),
    [
        pytest.param(
            {"memory_current": "734003200\n", "memory_max": "1610612736\n"},
            734003200,
            1610612736,
            id="read-from-the-cgroup",
        ),
        pytest.param(
            {"memory_current": "5\n", "memory_max": "max\n"}, 5, 999, id="max-is-the-limit"
        ),
        pytest.param({}, 0, 999, id="files-gone"),
        pytest.param({"memory_current": "garbage"}, 0, 999, id="unreadable"),
    ],
)
def test_usage_and_limit_come_from_the_host_cgroup(
    tmp_path: Path, files: dict[str, str], usage: int, limit: int
) -> None:
    source = mc.CgroupSource(View(cgroup(tmp_path, **files)), limit=999)
    assert source.usage_bytes() == usage
    assert source.limit_bytes() == limit


def test_a_stopped_sandbox_uses_nothing_against_its_configured_limit() -> None:
    source = mc.CgroupSource(View(None, ooms=3), limit=2048)
    assert source.usage_bytes() == 0
    assert source.limit_bytes() == 2048
    assert source.oom_events() == 3


@pytest.mark.parametrize(
    ("text", "count"),
    [
        pytest.param("oom 2\noom_kill 3\noom_group_kill 1\n", 4, id="both-counters"),
        pytest.param("oom_kill 2\n", 2, id="no-group-kill-line"),
        pytest.param("oom_group_kill 1\n", 1, id="group-kill-only"),
        pytest.param("low 0\nmax 99\n", 0, id="other-events-only"),
    ],
)
def test_oom_events_sum_oom_kill_and_oom_group_kill(tmp_path: Path, text: str, count: int) -> None:
    assert mc.read_oom_events(cgroup(tmp_path, memory_events=text)) == count


def test_oom_events_of_a_gone_cgroup_are_unknown_not_zero(tmp_path: Path) -> None:
    assert mc.read_oom_events(tmp_path / "gone") is None


def test_a_zero_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        mc.CgroupSource(View(None), limit=0)


# --- system.install ------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return make_rig(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("curl", id="plain"),
        pytest.param("libpq-dev", id="dash"),
        pytest.param("g++", id="plus"),
        pytest.param("python3.12", id="dot"),
        pytest.param("7zip", id="leading-digit"),
    ],
)
def test_package_names_the_managers_accept_pass(name: str) -> None:
    assert si.checked_packages([name]) == (name,)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("-y", id="an-option"),
        pytest.param("--reinstall", id="a-long-option"),
        pytest.param("../x", id="a-path"),
        pytest.param("/tmp/x.deb", id="an-absolute-path"),
        pytest.param("Curl", id="uppercase"),
        pytest.param("a b", id="a-space"),
        pytest.param("pkg=1.0", id="a-version-pin"),
        pytest.param("pkg;rm", id="a-shell-word"),
        pytest.param("", id="empty"),
    ],
)
def test_anything_but_a_package_name_is_refused(name: str) -> None:
    with pytest.raises(si.InstallRefusedError, match="is not a package name"):
        si.checked_packages([name])


@pytest.mark.parametrize(
    ("packages", "match"),
    [
        pytest.param([], "at least one", id="none"),
        pytest.param([f"p{i}" for i in range(si.MAX_PACKAGES + 1)], "at most", id="too-many"),
    ],
)
def test_a_request_names_one_to_fifty_packages(packages: list[str], match: str) -> None:
    with pytest.raises(si.InstallRefusedError, match=match):
        si.checked_packages(packages)


def test_apt_runs_as_root_with_exactly_the_install_capabilities(rig: Rig) -> None:
    sandbox = rig.sandbox()
    update, install = si.install_argvs(sandbox, "apt", ["curl", "jq", "curl"])
    for argv in (update, install):
        at = argv.index(rig.config.container)
        assert argv[2:4] == ["exec", "--user=0:0"]
        assert [a for a in argv[:at] if a.startswith("--cap=")] == [
            "--cap=CAP_SETUID",
            "--cap=CAP_SETGID",
            "--cap=CAP_CHOWN",
            "--cap=CAP_DAC_OVERRIDE",
            "--cap=CAP_FOWNER",
        ]
        assert "--cwd=/" in argv[:at]
        env_end = at + 3 + len(si.INSTALL_ENV)
        assert argv[at + 1 : at + 3] == [CONTAINER_ENV, "-i"]
        assert argv[at + 3 : env_end] == [f"{k}={v}" for k, v in si.INSTALL_ENV.items()]
    assert update[update.index(rig.config.container) + 3 + len(si.INSTALL_ENV) :] == [
        "apt-get",
        "update",
        "-qq",
    ]
    tail = install[install.index(rig.config.container) + 3 + len(si.INSTALL_ENV) :]
    assert tail == ["apt-get", "install", "-y", "-qq", "--no-install-recommends", "curl", "jq"]


def test_apk_is_one_command_and_an_unknown_manager_is_refused(rig: Rig) -> None:
    sandbox = rig.sandbox()
    (only,) = si.install_argvs(sandbox, "apk", ["curl"])
    assert only[-4:] == ["apk", "add", "--no-cache", "curl"]
    with pytest.raises(si.InstallRefusedError, match="not a package manager"):
        si.install_argvs(sandbox, "pip", ["curl"])


def test_an_install_runs_in_the_kernel_sandbox_and_says_what_it_is(rig: Rig) -> None:
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        result = si.SystemInstaller(sandbox).install("apt", ["curl"])
        assert result.ok and result.packages == ("curl",)
        assert result.note == "Installed until this workspace sleeps, visible to notebook kernels."
        assert "ran apt-get update -qq" in result.output
        assert "ran apt-get install" in result.output
        assert sandbox.running()  # it started the sandbox it installs into
    finally:
        sandbox.stop()


def test_a_failed_step_stops_the_install(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NBCNT_EXEC_STATUS", "100")
    sandbox = rig.sandbox(stop_grace=0.2)
    try:
        result = si.SystemInstaller(sandbox).install("apt", ["curl"])
        assert not result.ok
        assert "apt-get install" not in result.output  # the update failed; nothing installed
    finally:
        sandbox.stop()


def test_a_bad_request_runs_nothing(rig: Rig) -> None:
    sandbox = rig.sandbox()
    with pytest.raises(si.InstallRefusedError):
        si.SystemInstaller(sandbox).install("apt", ["--allow-downgrades"])
    assert not sandbox.running() and rig.runsc_calls() == []
