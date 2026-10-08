"""The local developer box provider (``alkera_core.compute.localdev``).

Driven against :class:`~alkera_test_support.compute.fake_docker.FakeDocker`, an
in-memory Docker that keeps the state the provider's commands act on, so every
test asserts what ``docker ps`` would show: which boxes exist, whether they
run, and how they are labelled and wired.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.availability import SizeQuery
from alkera_core.compute.bootstrap import (
    LOCALDEV_AGENT_SOURCE,
    LOCALDEV_CONTAINER_ENV,
    LOCALDEV_FORWARD_ENV,
    LOCALDEV_SOURCE,
)
from alkera_core.compute.localdev import (
    AGENT_BUILD_SCRIPT,
    IMAGE_CONTEXT,
    LABEL_ALLOCATION,
    LABEL_PROJECT,
    LOCALDEV_TYPE_CODE,
    DockerResult,
    DockerTimeoutError,
    LocaldevProvider,
    image_tag,
    loopback_targets,
    run_agent_build,
)
from alkera_core.compute.provider import (
    GONE,
    INVALID_FAILURE,
    RUNNING,
    STARTING,
    ComputeProviderError,
    ComputeProviderUnavailableError,
    NodeLaunch,
)
from alkera_test_support.compute.fake_docker import FakeDocker

SECRETS = {
    "ALKERA_MACHINE_CREDENTIAL": "mc_secret_value",
    "ALKERA_BOX_TOKEN": "eyJ.box.token",
    "ALKERA_BOX_TOKEN_EXPIRES": "2026-12-01T00:00:00+00:00",
}


def _stage_agent(root: Path, names: tuple[str, ...] = ("opencode", "rg")) -> None:
    """What the harness build script leaves in the source tree."""
    staged = root / LOCALDEV_AGENT_SOURCE
    staged.mkdir(parents=True, exist_ok=True)
    for name in names:
        (staged / name).write_text("#!/bin/sh\n")
        (staged / name).chmod(0o755)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A source tree with the box image's build context and the staged Linux
    harness, as a checkout that ran the harness build has."""
    root = tmp_path / "src"
    (root / IMAGE_CONTEXT).mkdir(parents=True)
    (root / IMAGE_CONTEXT / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    _stage_agent(root)
    return root


@pytest.fixture
def bare_source(tmp_path: Path) -> Path:
    """A checkout that never built the box's Linux harness."""
    root = tmp_path / "bare"
    (root / IMAGE_CONTEXT).mkdir(parents=True)
    (root / IMAGE_CONTEXT / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    return root


class FakeAgentBuild:
    """The harness build script, stood in for: records each run, and stages
    the harness (or not) and exits as told."""

    def __init__(
        self, *, stages: tuple[str, ...] = ("opencode", "rg"), code: int = 0, stderr: str = ""
    ) -> None:
        self.stages = stages
        self.code = code
        self.stderr = stderr
        self.runs: list[Path] = []

    async def __call__(self, root: Path, deadline_s: float) -> DockerResult:
        self.runs.append(root)
        if self.stages:
            _stage_agent(root, self.stages)
        return DockerResult(self.code, "", self.stderr)


def _provider(source: Path, docker: FakeDocker, **overrides: Any) -> LocaldevProvider:
    base: dict[str, Any] = {
        "enabled": True,
        "project": "alkera-feature-x",
        "source_root": source,
        "state_dir": source.parent / "state",
        "forward_ports": (38338, 38320),
        "loopback_names": ("files.localhost",),
        "docker": docker,
        # Never the real script: a test that needs a build passes its own.
        "build_agent": FakeAgentBuild(code=1, stages=(), stderr="no harness build in a test"),
    }
    base.update(overrides)
    return LocaldevProvider(**base)


def _launch(allocation_id: UUID, script: str = "#!/usr/bin/env bash\necho boot\n") -> NodeLaunch:
    return NodeLaunch(
        allocation_id=allocation_id,
        name=f"alkera-node-{allocation_id.hex[:8]}",
        type_code=LOCALDEV_TYPE_CODE,
        storage_gb=10,
        script=script,
        secrets=SECRETS,
        tags={"alkera:managed": "true", "alkera:allocation_id": str(allocation_id)},
    )


def _env_file(path: Path) -> tuple[int, dict[str, str], str]:
    """The env file's permission bits, the secrets it holds, and the launch
    record kept beside it."""
    secrets = dict(line.split("=", 1) for line in path.read_text().splitlines())
    return stat.S_IMODE(path.stat().st_mode), secrets, (path.parent / "launch.json").read_text()


async def _started(provider: LocaldevProvider) -> tuple[UUID, str]:
    allocation_id = uuid4()
    await provider.store_credential(allocation_id, SECRETS)
    name = await provider.run(_launch(allocation_id))
    return allocation_id, name


# -- refusing outside local dev ------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda p: p.store_credential(uuid4(), SECRETS), id="store_credential"),
        pytest.param(lambda p: p.run(_launch(uuid4())), id="run"),
        pytest.param(lambda p: p.ensure_running("any"), id="ensure_running"),
        pytest.param(lambda p: p.describe("any"), id="describe"),
        pytest.param(lambda p: p.find(uuid4()), id="find"),
        pytest.param(lambda p: p.list_pods(), id="list_pods"),
        pytest.param(lambda p: p.stop("any"), id="stop"),
        pytest.param(lambda p: p.terminate("any"), id="terminate"),
    ],
)
async def test_every_operation_refuses_outside_a_local_deployment(source: Path, call: Any) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker, enabled=False)
    assert not provider.configured()
    with pytest.raises(ComputeProviderUnavailableError, match="APP_ENV=local"):
        await call(provider)
    assert docker.containers == {} and docker.builds == []


async def test_a_session_pod_is_refused_even_locally(source: Path) -> None:
    provider = _provider(source, FakeDocker())
    with pytest.raises(ComputeProviderUnavailableError, match="workspace node"):
        await provider.create_pod(name="x", machine_type=None, ssh_public_key="")  # type: ignore[arg-type]


async def test_a_checkout_without_a_source_tree_is_not_configured(tmp_path: Path) -> None:
    provider = LocaldevProvider(
        enabled=True,
        project="p",
        source_root=tmp_path / "nowhere",
        state_dir=tmp_path / "s",
        docker=FakeDocker(),
    )
    assert not provider.configured()
    with pytest.raises(ComputeProviderUnavailableError, match="no source tree"):
        await provider.describe("x")


# -- starting a box ---------------------------------------------------------------


async def test_a_started_box_runs_as_a_privileged_labelled_container_on_the_source(
    source: Path,
) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    allocation_id, name = await _started(provider)

    box = docker.containers[name]
    assert box.status == "running"
    assert box.privileged
    assert box.restart == "unless-stopped"
    assert box.labels[LABEL_PROJECT] == "alkera-feature-x"
    assert box.labels[LABEL_ALLOCATION] == str(allocation_id)
    assert f"{source}:{LOCALDEV_SOURCE}:ro" in box.volumes
    assert box.env[LOCALDEV_CONTAINER_ENV] == name
    assert box.env[LOCALDEV_FORWARD_ENV] == "38338 38320"
    # The harness the box runs is the root-owned copy its boot installs on its own
    # disk: never the host's build (a Linux box cannot run it) and never a file on
    # the mounted source (gVisor will not load it as the chat's uid).
    assert box.env["ALKERA_OPENCODE_BIN"] == "/opt/alkera/agent/opencode"
    assert box.env["ALKERA_RIPGREP_BIN"] == "/opt/alkera/agent/rg"
    assert "files.localhost:127.0.0.1" in box.hosts
    assert "host.docker.internal:host-gateway" in box.hosts
    assert box.image == image_tag(source / IMAGE_CONTEXT)


# -- the box's Linux harness ------------------------------------------------------


async def test_a_staged_harness_starts_the_box_without_building(source: Path) -> None:
    docker = FakeDocker()
    build = FakeAgentBuild(code=1, stages=())
    _, name = await _started(_provider(source, docker, build_agent=build))
    assert build.runs == []
    assert docker.containers[name].status == "running"


async def test_a_missing_harness_is_built_once_before_the_box_starts(bare_source: Path) -> None:
    docker = FakeDocker()
    build = FakeAgentBuild()
    _, name = await _started(_provider(bare_source, docker, build_agent=build))
    assert build.runs == [bare_source]
    assert docker.containers[name].status == "running"


@pytest.mark.parametrize(
    ("build", "said"),
    [
        pytest.param(
            FakeAgentBuild(code=1, stages=(), stderr="bun: command not found"),
            "the automatic build failed: bun: command not found",
            id="build-fails",
        ),
        pytest.param(FakeAgentBuild(code=0, stages=()), None, id="build-stages-nothing"),
        pytest.param(FakeAgentBuild(code=0, stages=("opencode",)), None, id="build-stages-half"),
    ],
)
async def test_a_harness_the_build_cannot_stage_refuses_the_start_naming_the_script(
    bare_source: Path, build: FakeAgentBuild, said: str | None
) -> None:
    """Refused as ``invalid`` (not tried again), before any container exists:
    the machine reads as one that could not start, not a box that boot-loops."""
    docker = FakeDocker()
    provider = _provider(bare_source, docker, build_agent=build)
    with pytest.raises(ComputeProviderError) as refused:
        await _started(provider)
    assert refused.value.kind == INVALID_FAILURE
    assert str(AGENT_BUILD_SCRIPT) in str(refused.value)
    assert LOCALDEV_AGENT_SOURCE in str(refused.value)
    if said is not None:
        assert said in str(refused.value)
    else:
        assert "automatic build failed" not in str(refused.value)
    assert len(build.runs) == 1
    assert docker.containers == {}


async def test_a_deleted_box_is_not_recreated_without_its_harness(source: Path) -> None:
    docker = FakeDocker()
    build = FakeAgentBuild(code=1, stages=(), stderr="no docker")
    provider = _provider(source, docker, build_agent=build)
    _, name = await _started(provider)
    del docker.containers[name]
    for staged in (source / LOCALDEV_AGENT_SOURCE).iterdir():
        staged.unlink()
    with pytest.raises(ComputeProviderError, match=r"build-localdev-agent\.sh") as refused:
        await provider.ensure_running(name)
    assert refused.value.kind == INVALID_FAILURE
    assert name not in docker.containers


@pytest.mark.skipif(sys.platform == "win32", reason="the build script is a bash script")
@pytest.mark.parametrize(
    ("body", "deadline", "code", "said"),
    [
        pytest.param("echo built\n", 30.0, 0, "", id="exits-0"),
        pytest.param("echo nope >&2\nexit 3\n", 30.0, 3, "nope", id="exits-nonzero"),
        pytest.param("sleep 5\n", 0.2, 124, "did not finish", id="times-out"),
    ],
)
async def test_the_real_build_runs_the_script_in_the_source_tree(
    tmp_path: Path, body: str, deadline: float, code: int, said: str
) -> None:
    script = tmp_path / AGENT_BUILD_SCRIPT
    script.parent.mkdir(parents=True)
    script.write_text(f"[ -f pyproject.toml ] || exit 9\n{body}")
    (tmp_path / "pyproject.toml").write_text("")
    result = await run_agent_build(tmp_path, deadline)
    assert result.returncode == code
    assert said in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="Windows files carry no POSIX mode bits")
async def test_a_harness_that_is_not_executable_is_not_staged(bare_source: Path) -> None:
    _stage_agent(bare_source)
    (bare_source / LOCALDEV_AGENT_SOURCE / "rg").chmod(0o644)
    provider = _provider(bare_source, FakeDocker(), build_agent=FakeAgentBuild(code=1, stages=()))
    assert not provider.agent_staged()
    with pytest.raises(ComputeProviderError, match=r"build-localdev-agent\.sh"):
        await _started(provider)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows files carry no POSIX mode bits")
async def test_the_env_file_is_readable_by_its_owner_only(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)

    mode, _, _ = _env_file(Path(docker.containers[name].env_file))
    assert mode == 0o600


async def test_the_node_secrets_reach_the_box_only_through_a_private_env_file(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)

    env_file = Path(docker.containers[name].env_file)
    _, stored, launch = _env_file(env_file)
    assert stored == SECRETS
    box = docker.containers[name]
    for value in SECRETS.values():
        assert value not in " ".join(box.env.values())
        assert value not in launch


async def test_a_box_cannot_start_before_its_secrets_are_stored(source: Path) -> None:
    docker = FakeDocker()
    with pytest.raises(ComputeProviderError, match="secrets were not stored"):
        await _provider(source, docker).run(_launch(uuid4()))
    assert docker.containers == {}


@pytest.mark.parametrize(
    "secrets",
    [
        pytest.param({"PATH": "/evil"}, id="not-an-alkera-name"),
        pytest.param({"ALKERA_BOX_TOKEN": "a\nALKERA_X=b"}, id="newline-in-value"),
        pytest.param({"alkera_box_token": "x"}, id="lowercase-name"),
    ],
)
async def test_a_secret_that_could_inject_an_env_line_is_refused(
    source: Path, secrets: dict[str, str]
) -> None:
    with pytest.raises(ComputeProviderError, match="refusing node secret"):
        await _provider(source, FakeDocker()).store_credential(uuid4(), secrets)


async def test_the_image_is_built_once_and_rebuilt_when_its_context_changes(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    await _started(provider)
    await _started(provider)
    assert len(docker.builds) == 1

    (source / IMAGE_CONTEXT / "Dockerfile").write_text("FROM ubuntu:24.04\nRUN true\n")
    await _started(provider)
    assert len(docker.builds) == 2
    assert docker.builds[0] != docker.builds[1]


# -- coming back ------------------------------------------------------------------


async def test_a_running_box_is_left_alone(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)
    assert await provider.ensure_running(name) == "running"
    assert docker.containers[name].status == "running"


async def test_a_stopped_box_is_started_again(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)
    docker.containers[name].status = "exited"

    assert await provider.ensure_running(name) == "started"
    assert docker.containers[name].status == "running"


@pytest.mark.parametrize("verb", ["run", "start"])
async def test_a_box_docker_brought_up_after_the_cli_gave_up_counts_as_up(
    source: Path, verb: str
) -> None:
    """A loaded Docker can outlast the CLI's wait and still start the box; a
    provision failed then would leave a running container no row accounts for."""
    docker = FakeDocker(late={verb})
    provider = _provider(source, docker)
    if verb == "run":
        _, name = await _started(provider)
    else:
        docker.late.clear()
        _, name = await _started(provider)
        docker.containers[name].status = "exited"
        docker.late.add("start")
        assert await provider.ensure_running(name) == "started"
    assert docker.containers[name].status == "running"


@pytest.mark.parametrize("verb", ["run", "start"])
async def test_a_box_docker_never_brought_up_is_still_a_failure(source: Path, verb: str) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    if verb == "run":
        docker.hung.add("run")
        with pytest.raises(DockerTimeoutError):
            await _started(provider)
        assert docker.containers == {}
    else:
        _, name = await _started(provider)
        docker.containers[name].status = "exited"
        docker.hung.add("start")
        with pytest.raises(DockerTimeoutError):
            await provider.ensure_running(name)
        assert docker.containers[name].status == "exited"


async def test_a_deleted_box_is_recreated_with_the_same_wiring(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)
    before = docker.containers.pop(name)

    assert await provider.ensure_running(name) == "recreated"
    after = docker.containers[name]
    assert after.status == "running"
    assert after.labels == before.labels
    assert after.env == before.env
    assert after.env_file == before.env_file
    assert after.volumes == before.volumes


async def test_a_deleted_box_with_nothing_kept_cannot_come_back(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    allocation_id, name = await _started(provider)
    docker.containers.pop(name)
    await provider.delete_credential(allocation_id)

    with pytest.raises(ComputeProviderError, match="nothing is kept"):
        await provider.ensure_running(name)
    assert name not in docker.containers


# -- describing it ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        pytest.param("running", RUNNING, id="running"),
        pytest.param("created", STARTING, id="created"),
        pytest.param("restarting", STARTING, id="restarting"),
        pytest.param("exited", STARTING, id="stopped-but-restartable"),
        pytest.param("dead", STARTING, id="dead-but-restartable"),
    ],
)
async def test_a_box_that_exists_is_described_by_its_container_state(
    source: Path, status: str, phase: str
) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)
    docker.containers[name].status = status
    assert (await provider.describe(name)).phase == phase


async def test_a_deleted_box_that_can_be_recreated_is_starting_not_gone(source: Path) -> None:
    """The reconcile marks a ready box whose machine is gone as lost; a local box
    the keep-alive is about to recreate must not be thrown away meanwhile."""
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)
    docker.containers.pop(name)
    assert (await provider.describe(name)).phase == STARTING


async def test_a_box_that_is_gone_for_good_is_gone(source: Path) -> None:
    provider = _provider(source, FakeDocker())
    assert (await provider.describe(f"{provider.project}-box-000000000000")).phase == GONE


async def test_listings_see_only_this_checkouts_boxes(source: Path, tmp_path: Path) -> None:
    """Two worktrees share one Docker; neither backend may adopt or reap the
    other's boxes."""
    docker = FakeDocker()
    mine = _provider(source, docker)
    theirs = _provider(source, docker, project="alkera-other-branch", state_dir=tmp_path / "other")
    my_alloc, my_box = await _started(mine)
    their_alloc, their_box = await _started(theirs)

    assert [pod.pod_id for pod in await mine.list_pods()] == [my_box]
    assert [pod.pod_id for pod in await theirs.list_pods()] == [their_box]
    assert await mine.find(their_alloc) is None
    found = await mine.find(my_alloc)
    assert found is not None and found.machine_id == my_box and found.phase == RUNNING


async def test_a_listed_box_carries_the_name_the_plane_gave_it(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    allocation_id, _ = await _started(provider)
    (pod,) = await provider.list_pods(name_prefix="alkera-node-")
    assert pod.name == f"alkera-node-{allocation_id.hex[:8]}"
    assert pod.created_at is not None and pod.created_at.year == 2026
    assert await provider.list_pods(name_prefix="something-else") == []


# -- stopping and ending it --------------------------------------------------------


async def test_a_stopped_box_stays_stopped_until_started(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    _, name = await _started(provider)
    await provider.stop(name)
    assert docker.containers[name].status == "exited"
    await provider.start(name)
    assert docker.containers[name].status == "running"


async def test_stopping_or_terminating_a_box_that_is_already_gone_succeeds(source: Path) -> None:
    provider = _provider(source, FakeDocker())
    await provider.stop("alkera-feature-x-box-000000000000")
    await provider.terminate("alkera-feature-x-box-000000000000")


async def test_a_terminated_box_leaves_nothing_behind(source: Path) -> None:
    docker = FakeDocker()
    provider = _provider(source, docker)
    allocation_id, name = await _started(provider)

    await provider.terminate(name)

    assert name not in docker.containers
    assert not any(volume.startswith(name) for volume in docker.volumes)
    assert not (provider.state_dir / str(allocation_id)).exists()
    assert (await provider.describe(name)).phase == GONE


# -- catalog and availability -------------------------------------------------------


async def test_the_catalog_offers_one_free_local_size(source: Path) -> None:
    provider = _provider(source, FakeDocker())
    assert await provider.catalog_prices() == {LOCALDEV_TYPE_CODE: 0}
    entry = (await provider.catalog_entries())[LOCALDEV_TYPE_CODE]
    assert entry["price_nanos"] == 0 and entry["availability"] == "HIGH"


@pytest.mark.parametrize(
    ("enabled", "down", "status"),
    [
        pytest.param(True, False, "available", id="docker-up"),
        pytest.param(True, True, "unknown", id="docker-down"),
        pytest.param(False, False, "unavailable", id="not-local"),
    ],
)
async def test_availability_follows_docker_and_the_deployment(
    source: Path, enabled: bool, down: bool, status: str
) -> None:
    provider = _provider(source, FakeDocker(down=down), enabled=enabled)
    answers = await provider.availability([SizeQuery(LOCALDEV_TYPE_CODE, 2)])
    assert answers[LOCALDEV_TYPE_CODE].status == status


# -- what a box must reach on the developer's machine ---------------------------------


@pytest.mark.parametrize(
    ("urls", "ports", "names"),
    [
        pytest.param(["http://localhost:38338"], (38338,), (), id="localhost"),
        pytest.param(["http://127.0.0.1:9000"], (9000,), (), id="loopback-ip"),
        pytest.param(
            ["http://files.localhost:38320"], (38320,), ("files.localhost",), id="dot-localhost"
        ),
        pytest.param(["https://s3.amazonaws.com", "http://minio:9000"], (), (), id="remote-hosts"),
        pytest.param([None, ""], (), (), id="unset"),
        pytest.param(["http://localhost"], (80,), (), id="default-port"),
    ],
)
def test_only_loopback_urls_are_forwarded(
    urls: list[str | None], ports: tuple[int, ...], names: tuple[str, ...]
) -> None:
    assert loopback_targets(urls) == (ports, names)
