"""The self-hosted shapes -- the reference compose file, the air-gap bundle and
the smoke scripts -- run Temporal instead of a broker and a scheduler, and
describe one and the same fleet.

Nothing at runtime checks a manifest. A compose file that still starts a broker
the worker no longer reads boots cleanly and does nothing. So these tests read
the shapes with real parsers (PyYAML) and, where the tool is on PATH, render
them for real (`docker compose config`), and pin what an operator relies on:
which services exist and which are gone, how the worker is probed and what it
waits for, that the bundled Temporal creates its own databases on the bundled
Postgres, that the unauthenticated console is never reachable off-host, and
that every third-party image is digest-pinned and the air-gap bundle carries
exactly those.

Every render runs under an environment this module builds (`_tool_env`) rather
than the caller's, so what the manifests resolve to is the file's own defaults
and not whatever the shell happened to export.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from backend.api.body_limit import _CHAT_BODY_MAX_BYTES as CHAT_BODY_MAX_BYTES
from worker.temporal.runner import GRACEFUL_SHUTDOWN

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "deploy" / "docker" / "compose.prod.example.yml"
MAKEFILE = REPO_ROOT / "Makefile"
INSTALL = REPO_ROOT / "deploy" / "INSTALL.md"
SELFDEPLOY = REPO_ROOT / "ops" / "test" / "selfdeploy"
WEB_ENTRYPOINT = REPO_ROOT / "apps" / "web" / "docker-entrypoint.sh"
WEB_NGINX = REPO_ROOT / "apps" / "web" / "nginx.conf.template"

QUEUES = ("money", "email", "sync", "default")
ALL_QUEUES = ",".join(QUEUES)
WORKER_COMMAND = ["python", "-m", "worker", "run", "--queues", ALL_QUEUES]
WORKER_HEALTH_COMMAND = ["CMD", "python", "-m", "worker", "health"]
TEMPORAL_HEALTH_RPC = ["temporal", "operator", "cluster", "health", "--address"]
TEMPORAL_ENV = ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY")
BROKER_ENV = ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "SQS_QUEUE_URL")
#: A retired-shape token: the broker, the scheduler, or the task runner they served.
BROKER_TOKEN = re.compile(r"(?i)redis|celery|\bbeat\b")
DIGEST_PINNED = re.compile(r"^[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")

#: What `docker compose config` needs to interpolate the file (the `:?` keys).
COMPOSE_PLACEHOLDER_ENV = {
    "DB_PASSWORD": "x",
    "TEMPORAL_DB_PASSWORD": "y",
    "AUTH_JWT_SECRET": "x",
    "TOKEN_HASH_PEPPER": "x",
    "PUBLIC_BASE_URL": "http://localhost",
    "FILES_CONTENT_BASE_URL": "http://files.localhost",
    "ADMIN_BOOTSTRAP_EMAIL": "admin@acme.example",
    "ADMIN_BOOTSTRAP_ORG_NAME": "Acme",
}

#: The only environment a render is allowed to see: how to find the tool
#: (PATH, HOME, a temp dir, the locale) and, for docker, how to reach the
#: daemon -- the socket lives in DOCKER_HOST / DOCKER_CONTEXT / a Colima or
#: Docker config dir. Matched case-insensitively so a Windows `SystemRoot`
#: is carried too.
_TOOL_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "USERPROFILE",
        "KUBECONFIG",
    }
)
_TOOL_ENV_PREFIXES = ("DOCKER_", "COLIMA_", "HELM_", "XDG_")


def _tool_env(**placeholders: str) -> dict[str, str]:
    """The environment a manifest render runs under -- built, never inherited.

    `docker compose config` interpolates `${VAR:-default}` from the CALLER's
    environment, so a shell that carries a developer's own values resolves the
    file to those instead of to the defaults an operator gets: this worktree's
    `.env.workspace` alone sets TEMPORAL_ADDRESS and a per-branch port for
    every service, and a real OPENAI_API_KEY or SMTP password in the shell
    would be baked into the rendered document as well. Inheriting the
    environment therefore makes these tests pin the machine they run on --
    green in CI, red on a laptop, and blind either way to a manifest whose
    defaults have drifted.

    So pass only what running the tool needs, plus the `:?` placeholders the
    file demands. Everything else -- ALKERA_*, TEMPORAL_*, DATABASE_*, the
    ports, COMPOSE_*, APP_VERSION, the image tags, the provider keys -- stays
    behind, and the render shows the file's own defaults.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _TOOL_ENV_NAMES or name.upper().startswith(_TOOL_ENV_PREFIXES)
    }
    env.update(placeholders)
    return env


# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = [
    pytest.mark.skipif(
        not COMPOSE.is_file(),
        reason="the self-hosted shapes are not part of this distribution",
    ),
    pytest.mark.xdist_group("self_hosted_manifests"),
]


def _has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "compose", "version"], capture_output=True, env=_tool_env()
            ).returncode
            == 0
        )
    except OSError:
        return False


needs_compose = pytest.mark.skipif(
    sys.platform == "win32" or not _has_compose(), reason="renders the file with docker compose"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _compose() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return doc


def _services() -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = _compose()["services"]
    return services


def _third_party_compose_images() -> set[str]:
    return {
        str(svc["image"])
        for svc in _services().values()
        if not str(svc["image"]).startswith("alkera/")
    }


def _tag(image: str) -> str:
    """`repo:tag@sha256:...` / `repo:tag` -> `repo:tag`."""
    return image.split("@", 1)[0]


def _compose_copy(root: Path) -> Path:
    copy = root / "deploy" / "docker" / COMPOSE.name
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(COMPOSE, copy)
    return copy


def _compose_config(cwd: Path, *profile: str) -> dict[str, Any]:
    """`docker compose config` on the reference file, under a built environment.

    Rendered from a copy laid out as the repository is, under `cwd`: the file
    reads the repository root's `.env` (`env_file: ../../.env`), and the
    checkout's own `.env` is a developer's, not an operator's."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(_compose_copy(cwd)),
            *profile,
            "config",
            "--format",
            "json",
        ],
        cwd=cwd,
        env=_tool_env(**COMPOSE_PLACEHOLDER_ENV),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


def _compose_seconds(value: str | int) -> int:
    """A compose duration (`120s`, `2m`, a bare number of seconds) in seconds."""
    text = str(value).strip()
    match = re.fullmatch(r"(\d+)(s|m|h)?", text)
    assert match, f"not a compose duration: {value}"
    return int(match[1]) * {None: 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def _command_text(service: dict[str, Any]) -> str:
    command = service.get("command", "")
    return command if isinstance(command, str) else " ".join(str(part) for part in command)


# --------------------------------------------------------------------------
# 1. the compose file
# --------------------------------------------------------------------------


class TestComposeRunsTemporalInsteadOfABrokerAndAScheduler:
    def test_the_broker_and_the_scheduler_are_gone_and_temporal_is_present(self) -> None:
        services = _services()
        assert {
            "temporal",
            "temporal-db-init",
            "temporal-ui",
            "worker",
            "backend",
            "gateway",
            "postgres",
            "web",
        } <= set(services)
        assert not [name for name in services if BROKER_TOKEN.search(name)], sorted(services)
        assert not [v for v in _compose()["volumes"] if BROKER_TOKEN.search(v)]

    def test_the_shared_env_names_temporal_and_no_broker(self) -> None:
        env = _compose()["x-app-env"]
        for key in TEMPORAL_ENV:
            assert key in env, key
        assert not set(BROKER_ENV) & set(env), sorted(set(BROKER_ENV) & set(env))
        # Blank = unset: the app treats an empty key as "no key" (plaintext gRPC to
        # the bundled server), so a deploy that sets none of these must still boot.
        assert env["TEMPORAL_ADDRESS"] == "${TEMPORAL_ADDRESS:-temporal:7233}"
        assert env["TEMPORAL_NAMESPACE"] == "${TEMPORAL_NAMESPACE:-default}"
        assert env["TEMPORAL_API_KEY"] == "${TEMPORAL_API_KEY:-}"

    def test_the_worker_serves_every_queue_and_is_probed_by_its_own_health_command(
        self,
    ) -> None:
        worker = _services()["worker"]
        assert worker["image"].startswith("alkera/worker:")
        assert worker["command"] == WORKER_COMMAND
        assert worker["healthcheck"]["test"] == WORKER_HEALTH_COMMAND
        assert worker["healthcheck"]["start_period"] == "90s", (
            "the start period must cover the worker's boot connect-and-retry"
        )
        assert worker["read_only"] is True
        depends = worker["depends_on"]
        assert depends["temporal"]["condition"] == "service_healthy"
        assert depends["backend"]["condition"] == "service_healthy", (
            "the worker waits for the backend so migrations have applied"
        )
        assert depends["postgres"]["condition"] == "service_healthy"

    def test_every_draining_service_outlives_its_own_drain(self) -> None:
        """A compose `stop` sends SIGTERM and SIGKILLs 10 seconds later by default.
        A service that asked uvicorn to spend longer than that finishing open
        connections is therefore killed mid-drain on every redeploy -- the exact
        outcome the deadline was set to avoid, and one that ends a chat turn or a
        provider stream rather than draining it. Compose is not in the image's list
        of supervisors, so the window has to be declared here."""
        offenders = []
        for name, service in _services().items():
            match = re.search(r"--timeout-graceful-shutdown\s+(\d+)", _command_text(service))
            if not match:
                continue
            drain = int(match[1])
            grace = service.get("stop_grace_period")
            if grace is None or _compose_seconds(grace) <= drain:
                offenders.append(f"{name}: drains {drain}s, stop_grace_period={grace}")
        assert not offenders, offenders

    def test_the_worker_outlives_the_drain_its_own_code_asks_for(self) -> None:
        """The worker's window is not on a command line -- the Temporal worker is
        built with graceful_shutdown_timeout, so in-flight activities get that long
        after SIGTERM. Compose's default kills it at 10 seconds and every redeploy
        loses whatever was running."""
        grace = _services()["worker"].get("stop_grace_period")
        assert grace is not None, "the worker drains but compose SIGKILLs it at 10s"
        assert _compose_seconds(grace) > GRACEFUL_SHUTDOWN.total_seconds()

    def test_temporal_runs_on_its_own_database_role_not_the_applications(self) -> None:
        """The bundled server is a third-party image with a Postgres credential.
        That credential must be Temporal's own least-privileged role -- the
        `alkera` role is the instance superuser and owns every application table,
        so handing it over would give another project's image read of all customer
        data. Its two databases are pre-created by temporal-db-init, which is why
        the server never needs CREATE DATABASE."""
        temporal = _services()["temporal"]
        env = temporal["environment"]
        assert env["DB"] == "postgres12"
        assert env["POSTGRES_SEEDS"] == "postgres"
        assert env["POSTGRES_USER"] == "temporal"
        assert env["POSTGRES_PWD"] == "${TEMPORAL_DB_PASSWORD:?must set TEMPORAL_DB_PASSWORD}"
        assert not re.search(r"\$\{DB_PASSWORD\b", env["POSTGRES_PWD"]), (
            "the application database's password reached the third-party server"
        )
        assert env["DBNAME"] == "temporal"
        assert env["VISIBILITY_DBNAME"] == "temporal_visibility"
        assert env["SKIP_DB_CREATE"] == "true"
        assert env["DEFAULT_NAMESPACE"] == "default"
        assert temporal["healthcheck"]["test"] == ["CMD", *TEMPORAL_HEALTH_RPC, "temporal:7233"]
        assert temporal["expose"] == ["7233"]
        assert "ports" not in temporal, "the server is in-network only"
        assert temporal["depends_on"]["postgres"]["condition"] == "service_healthy"
        assert (
            temporal["depends_on"]["temporal-db-init"]["condition"]
            == "service_completed_successfully"
        ), "the server may not start before its role and databases exist"

    def test_the_temporal_role_and_its_databases_are_created_before_the_server(self) -> None:
        """The one-shot that makes the dedicated role real. It runs as the
        superuser (only it can CREATE ROLE), is idempotent so every `up` re-asserts
        the password, and takes ownership of databases an earlier release created
        as `alkera` -- which is what makes an in-place upgrade work."""
        init = _services()["temporal-db-init"]
        assert init["restart"] == "no", "a one-shot must not be restarted"
        assert init["depends_on"]["postgres"]["condition"] == "service_healthy"
        env = init["environment"]
        assert env["PGUSER"] == "alkera", "creating a role needs the superuser"
        assert env["PGPASSWORD"] == "${DB_PASSWORD:?must set DB_PASSWORD}"
        assert env["TEMPORAL_DB_PASSWORD"] == (
            "${TEMPORAL_DB_PASSWORD:?must set TEMPORAL_DB_PASSWORD}"
        ), "the new role's password must be its own required variable"
        script = "\n".join(init["command"])
        assert "CREATE ROLE temporal LOGIN PASSWORD :'pw'" in script
        assert "ALTER ROLE temporal LOGIN PASSWORD :'pw'" in script, (
            "re-running must re-assert the password, so a rotation is just an `up`"
        )
        for statement in (
            "CREATE DATABASE $db OWNER temporal",
            "ALTER DATABASE $db OWNER TO temporal",
            "ALTER SCHEMA public OWNER TO temporal",
        ):
            assert statement in script.replace("$$", "$"), statement
        assert "OWNER TO temporal" in script and "pg_get_userbyid(c.relowner) <> 'temporal'" in (
            script
        ), "an upgrade must move the existing tables to the new owner too"

    def test_no_service_but_the_bootstrap_pair_holds_the_application_superuser(self) -> None:
        """A tight net around the finding: the `alkera` credential belongs to the
        app's own services and to the one-shots that administer the database --
        never to a third-party image."""
        allowed = {"postgres", "temporal-db-init"}
        superuser = re.compile(r"\$\{DB_PASSWORD\b")
        for name, service in _services().items():
            if name in allowed or str(service.get("image", "")).startswith("alkera/"):
                continue
            env = {k: str(v) for k, v in (service.get("environment") or {}).items()}
            holds = sorted(k for k, v in env.items() if superuser.search(v))
            assert not holds, (name, holds)

    def test_the_console_is_opt_in_and_bound_to_loopback_only(self) -> None:
        ui = _services()["temporal-ui"]
        assert ui["profiles"] == ["temporal-ui"]
        assert ui["ports"], "the console must publish a port, on loopback"
        for mapping in ui["ports"]:
            assert str(mapping).startswith("127.0.0.1:"), mapping
        assert ui["depends_on"]["temporal"]["condition"] == "service_healthy"

    @pytest.mark.parametrize("service", ["backend", "gateway", "worker", "bootstrap"])
    def test_no_service_waits_for_a_broker(self, service: str) -> None:
        depends = _services()[service].get("depends_on") or {}
        assert not [d for d in depends if BROKER_TOKEN.search(d)], depends

    def test_every_third_party_image_is_digest_pinned(self) -> None:
        images = _third_party_compose_images()
        assert images, "no third-party image found"
        for image in images:
            assert DIGEST_PINNED.match(image), image
        assert any(_tag(i).startswith("temporalio/auto-setup:") for i in images)
        assert any(_tag(i).startswith("temporalio/ui:") for i in images)

    def test_the_airgap_bundle_pins_exactly_the_compose_infra_images(self) -> None:
        """The offline tarball is what an air-gapped operator loads; an image the
        compose file pulls but the bundle lacks is a failed `up`, and the other
        way round ships bytes nobody runs."""
        match = re.search(
            r"^AIRGAP_INFRA_IMAGES \?= (.+)$", MAKEFILE.read_text(encoding="utf-8"), re.M
        )
        assert match, "AIRGAP_INFRA_IMAGES is no longer defined in the Makefile"
        assert set(match.group(1).split()) == _third_party_compose_images()

    def test_every_operator_csp_example_admits_the_websocket_schemes(self) -> None:
        """Every spelling of the SPA policy -- the image default, the compose
        override example and the deploy notes -- must all keep the
        websocket schemes in connect-src, or a copied example silently breaks
        the portal's live updates."""
        examples = {
            "image default": WEB_ENTRYPOINT.read_text(encoding="utf-8"),
            "compose example": COMPOSE.read_text(encoding="utf-8"),
            "deploy notes": (REPO_ROOT / "ops" / "docs" / "deploy-notes.md").read_text(
                encoding="utf-8"
            ),
        }
        for where, text in examples.items():
            # A full policy names default-src; prose that merely mentions the
            # directive does not.
            policies = [
                line
                for line in text.splitlines()
                if "default-src" in line and "connect-src" in line
            ]
            assert policies, f"{where}: no full CSP example"
            for policy in policies:
                assert re.search(r"connect-src 'self' ws: wss:", policy), (where, policy)

    @needs_compose
    def test_docker_compose_resolves_the_file_with_and_without_the_console_profile(
        self, tmp_path: Path
    ) -> None:
        with_console = _compose_config(tmp_path, "--profile", "temporal-ui")
        assert set(with_console["services"]) == {
            "backend",
            "gateway",
            "postgres",
            "secrets-init",
            "temporal",
            "temporal-db-init",
            "temporal-ui",
            "web",
            "worker",
        }
        worker = with_console["services"]["worker"]
        assert worker["command"] == WORKER_COMMAND
        assert worker["healthcheck"]["test"] == WORKER_HEALTH_COMMAND
        assert set(worker["depends_on"]) == {"backend", "postgres", "secrets-init", "temporal"}
        for service in ("backend", "gateway", "worker"):
            resolved = with_console["services"][service]["environment"]
            assert resolved["TEMPORAL_ADDRESS"] == "temporal:7233", service
            assert resolved["TEMPORAL_NAMESPACE"] == "default", service
            assert resolved["TEMPORAL_API_KEY"] == "", service
            assert not set(BROKER_ENV) & set(resolved), service
        (binding,) = with_console["services"]["temporal-ui"]["ports"]
        assert binding["host_ip"] == "127.0.0.1" and binding["target"] == 8080
        # Three durable volumes and no others: Postgres, the Alkera Files object
        # tree (declared even while Files is off, so turning it on does not need
        # a compose edit), and the server secrets generated on first boot.
        assert set(with_console["volumes"]) == {
            "alkera_pg_data",
            "alkera_files_data",
            "alkera_secrets",
        }

        init = with_console["services"]["temporal-db-init"]
        assert init["environment"]["PGUSER"] == "alkera"
        assert init["environment"]["TEMPORAL_DB_PASSWORD"] != init["environment"]["PGPASSWORD"], (
            "the render must show two distinct credentials, not one reused"
        )
        assert with_console["services"]["temporal"]["environment"]["POSTGRES_USER"] == "temporal"

        without = _compose_config(tmp_path)
        assert "temporal-ui" not in without["services"]
        assert "temporal" in without["services"] and "worker" in without["services"]
        assert "temporal-db-init" in without["services"], (
            "the role bootstrap is not optional -- the server cannot start without it"
        )

    @needs_compose
    def test_the_stack_comes_up_without_the_one_shot_bootstrap_values(self, tmp_path: Path) -> None:
        """`docker compose up` interpolates every service, profiled or not, so a
        required (`:?`) bootstrap value refused the whole stack on an install that
        had not set it -- and the header says to bootstrap AFTER `up`."""
        env = {
            k: v for k, v in COMPOSE_PLACEHOLDER_ENV.items() if not k.startswith("ADMIN_BOOTSTRAP_")
        }
        result = subprocess.run(
            ["docker", "compose", "-f", str(_compose_copy(tmp_path)), "config", "--format", "json"],
            cwd=tmp_path,
            env=_tool_env(**env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

    @needs_compose
    def test_a_money_queue_worker_boots_with_no_compute_plane(self, tmp_path: Path) -> None:
        """The worker serving `money` refuses to boot in production when no
        provider the platform starts machines at is configured, unless the
        deployment says it runs no compute plane. The compose worker serves every
        queue, so without the flag it crash-looped. No hosted provider's key is
        named at all."""
        services = _compose_config(tmp_path)["services"]
        for service in ("backend", "gateway", "worker"):
            resolved = services[service]["environment"]
            assert "RUNPOD_API_KEY" not in resolved, service
            assert resolved["COMPUTE_REQUIRE_PROVIDER_KEY"] == "false", service

    @needs_compose
    def test_the_render_shows_the_files_defaults_not_the_callers_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A developer shell must not be able to change what the render says.

        `make test-py` exports this worktree's `.env.workspace`, which names a
        local Temporal and per-branch ports; a developer also carries provider
        keys and an image version. Compose interpolates all of them, so if the
        render inherited the environment these tests would describe the laptop
        they ran on rather than the file an operator deploys -- and would go
        red on that laptop while passing in CI.
        """
        monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:27230")
        monkeypatch.setenv("TEMPORAL_NAMESPACE", "worktree")
        monkeypatch.setenv("TEMPORAL_PORT", "27230")
        monkeypatch.setenv("API_PORT", "27220")
        monkeypatch.setenv("SMTP_PORT", "27225")
        monkeypatch.setenv("APP_VERSION", "0.0.0-worktree")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-developer-key")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:27231/0")

        services = _compose_config(tmp_path, "--profile", "temporal-ui")["services"]

        for service in ("backend", "gateway", "worker"):
            resolved = services[service]["environment"]
            assert resolved["TEMPORAL_ADDRESS"] == "temporal:7233", service
            assert resolved["TEMPORAL_NAMESPACE"] == "default", service
            assert resolved["SMTP_PORT"] == "587", service
            assert not set(BROKER_ENV) & set(resolved), service
        assert services["backend"]["image"] == "alkera/backend:0.0.0"
        assert services["gateway"]["environment"]["OPENAI_API_KEY"] == ""


# --------------------------------------------------------------------------
# 4. the operator surface around the shapes
# --------------------------------------------------------------------------


class TestTheOperatorSurfaceDescribesTheSameFleet:
    def test_the_selfdeploy_smokes_wait_for_the_worker_and_the_server(self) -> None:
        compose_smoke = (SELFDEPLOY / "compose-smoke.sh").read_text(encoding="utf-8")
        assert "worker=healthy" in compose_smoke and "temporal=healthy" in compose_smoke
        assert "TEMPORAL_DB_PASSWORD=" in compose_smoke, (
            "the smoke must supply the Temporal role's own password"
        )
        assert "temporal-db-init did not succeed" in compose_smoke
        assert "pg_get_userbyid(datdba)" in compose_smoke, (
            "the smoke must prove Temporal's databases belong to the dedicated role"
        )
        backup = (SELFDEPLOY / "compose-backup-restore.sh").read_text(encoding="utf-8")
        assert "docker compose stop backend worker temporal" in backup
        assert "docker compose start backend temporal worker" in backup
        for name, text in (
            ("compose-smoke.sh", compose_smoke),
            ("compose-backup-restore.sh", backup),
        ):
            assert not BROKER_TOKEN.search(text), name

    def test_the_install_guide_covers_the_bundled_temporal(self) -> None:
        text = INSTALL.read_text(encoding="utf-8")
        for phrase in (
            "--profile temporal-ui",
            "TEMPORAL_DB_PASSWORD",
            "temporal-db-init",
            "CREATEDB",
            "temporal_visibility",
            "worker=healthy",
            "TEMPORAL_ADDRESS",
        ):
            assert phrase in text, phrase

    def test_the_compose_shapes_never_name_the_retired_broker(self) -> None:
        """The Compose deployment and its guide never ran a Celery worker or a
        Redis broker, so neither names one."""
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in (COMPOSE, INSTALL)
            if BROKER_TOKEN.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], offenders


# --------------------------------------------------------------------------
# 5. the self-deploy smokes hold every secret they write
# --------------------------------------------------------------------------

#: What the reference compose file demands (`:?`) of any script that brings it
#: up. `compose-smoke.sh` reads TEMPORAL_DB_PASSWORD back after the boot to prove
#: the bundled Temporal runs as its own least-privileged role, so the value the
#: script holds and the value the file carries must be one and the same.
SELFDEPLOY_SECRETS = (
    "DB_PASSWORD",
    "TEMPORAL_DB_PASSWORD",
    "AUTH_JWT_SECRET",
    "TOKEN_HASH_PEPPER",
)
SELFDEPLOY_SMOKES = (
    "compose-smoke.sh",
    "compose-byo-smtp.sh",
    "compose-backup-restore.sh",
    "compose-sso-e2e.sh",
)
#: The line that opens the `.env` heredoc, however the script spells the target.
_ENV_HEREDOC_OPEN = re.compile(r"^cat > .*\.env\"?\s*<<\s*EOF\s*$")
#: A `docker` that answers every call without a daemon: the prologue installs an
#: EXIT trap that tears a stack down, and these tests must not touch one.
_STUB_DOCKER = "#!/bin/sh\nexit 0\n"
#: The variable each smoke sets just before its last line, and its EXIT trap reads
#: back to tell a finished run from an aborted one. See
#: `TestAnAbortedSelfDeploySmokeReportsItsOwnExitStatus` for why the abort's own
#: status is not enough on its own.
_COMPLETION_STAMP = "SMOKE_COMPLETED"
#: A name no smoke defines. Referenced after the prologue it is the `set -u` abort
#: an aborted run has to survive being reported as, byte for byte the shape that
#: was observed (`TEMPORAL_DB_PASSWORD: unbound variable`).
_UNSET_REFERENCE = "ALKERA_NAME_NO_SMOKE_DEFINES"

_selfdeploy_smoke = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("openssl") is None,
    reason="runs the smoke scripts' own prologue under bash with openssl",
)


def _env_prologue(script: Path) -> str:
    """The script's text up to and including the end of its `.env` heredoc.

    Everything the smoke does before it talks to Docker: the defaults, the work
    directory, the secrets, and the file written from them. Running exactly that
    much exercises the shipped bytes with no daemon and no images.
    """
    lines = script.read_text(encoding="utf-8").splitlines(keepends=True)
    opens = [i for i, line in enumerate(lines) if _ENV_HEREDOC_OPEN.match(line.rstrip("\n"))]
    assert len(opens) == 1, f"{script.name}: expected exactly one .env heredoc, found {opens}"
    for index in range(opens[0] + 1, len(lines)):
        if lines[index].rstrip("\n") == "EOF":
            return "".join(lines[: index + 1])
    raise AssertionError(f"{script.name}: the .env heredoc is never closed")


def _run_script(
    script: Path, tmp_path: Path, tail: str, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Run a copy of a smoke's prologue with `tail` appended, off any Docker host.

    The copy is the shipped bytes down to the end of the `.env` heredoc -- the trap
    the smoke installs to tear its stack down included -- so whatever `tail` does
    happens under the script's own `set -euo pipefail` with that trap armed.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    runner = tmp_path / f"prologue-{script.stem}.sh"
    runner.write_text(_env_prologue(script) + tail, encoding="utf-8")
    stub = tmp_path / "stub-bin"
    stub.mkdir(exist_ok=True)
    (stub / "docker").write_text(_STUB_DOCKER, encoding="utf-8")
    (stub / "docker").chmod(0o755)
    env = {
        "PATH": os.pathsep.join([str(stub), os.environ.get("PATH", "")]),
        "HOME": str(tmp_path),
        # The prologue resolves the compose file relative to the repo it ships in;
        # the runner lives outside it, so name the root rather than derive it.
        "REPO": str(REPO_ROOT),
        **overrides,
    }
    return subprocess.run(
        ["bash", str(runner)], env=env, capture_output=True, text=True, timeout=120
    )


@dataclass(frozen=True)
class PrologueRun:
    """What one execution of a smoke's prologue produced."""

    returncode: int
    stderr: str
    shell: dict[str, str]
    """Each secret as the SCRIPT holds it after writing the file."""
    written: dict[str, str]
    """Each secret as the `.env` it hands compose carries it."""


def _run_prologue(script: Path, tmp_path: Path, **overrides: str) -> PrologueRun:
    """Execute a smoke's prologue and read back both sides of every secret.

    The probe echoes each secret the way the rest of the smoke uses it, so a name
    that exists only as a line inside the heredoc is an unbound variable under the
    script's own `set -u` -- the same abort the smoke hits before its role-isolation
    assertion, surfaced here without a Docker host.
    """
    probe = "".join(
        f'\nprintf \'@shell %s=%s\\n\' "{name}" "${name}"' for name in SELFDEPLOY_SECRETS
    )
    result = _run_script(
        script,
        tmp_path,
        # The runner reaches its own end, so it stamps the completion the shipped
        # script stamps before its last line -- otherwise the smoke's EXIT trap
        # would rightly call this truncated run an abort.
        probe + '\nsed "s/^/@file /" "$WORK/.env"\n' + f"{_COMPLETION_STAMP}=1\n",
        **overrides,
    )
    shell: dict[str, str] = {}
    written: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("@shell "):
            name, _, value = line[len("@shell ") :].partition("=")
            shell[name] = value
        elif line.startswith("@file "):
            name, _, value = line[len("@file ") :].partition("=")
            written[name] = value
    return PrologueRun(
        returncode=result.returncode, stderr=result.stderr, shell=shell, written=written
    )


@_selfdeploy_smoke
@pytest.mark.parametrize("name", SELFDEPLOY_SMOKES)
class TestTheSelfDeploySmokesHoldTheSecretsTheyWrite:
    """A secret generated inside the `.env` heredoc is a file line and nothing more.

    `NAME=$(openssl rand -hex 16)` there leaves the script with no `$NAME`: the
    first later use aborts the run under `set -u`, before the assertion it was
    reached for -- and when the caller exported one, the value in the file differs
    from the value the script holds, so a probe authenticating with it is refused.
    Neither failure is visible to `bash -n` or `shellcheck`, so these cases run the
    shipped prologue for real and compare the two sides of every secret.
    """

    def test_the_script_holds_every_secret_it_writes(self, name: str, tmp_path: Path) -> None:
        run = _run_prologue(SELFDEPLOY / name, tmp_path)
        assert run.returncode == 0, run.stderr
        for secret in SELFDEPLOY_SECRETS:
            assert run.shell.get(secret), f"{name}: ${secret} is not a shell variable\n{run.stderr}"
            assert run.written.get(secret) == run.shell[secret], (
                f"{name}: .env carries a different {secret} than the script holds"
            )

    def test_a_caller_supplied_secret_is_the_one_written(self, name: str, tmp_path: Path) -> None:
        """An exported value must reach the file, or compose boots Temporal with one
        password while the smoke probes that role with another."""
        supplied = {secret: f"supplied-{secret.lower()}" for secret in SELFDEPLOY_SECRETS}
        run = _run_prologue(SELFDEPLOY / name, tmp_path, **supplied)
        assert run.returncode == 0, run.stderr
        for secret, value in supplied.items():
            assert run.shell[secret] == value, f"{name}: {secret} ignored the caller's value"
            assert run.written[secret] == value, f"{name}: .env dropped the caller's {secret}"

    def test_an_unsupplied_secret_is_freshly_generated_each_run(
        self, name: str, tmp_path: Path
    ) -> None:
        """Honouring a caller's value must not turn the default into a constant."""
        first = _run_prologue(SELFDEPLOY / name, tmp_path / "first")
        second = _run_prologue(SELFDEPLOY / name, tmp_path / "second")
        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        for secret in SELFDEPLOY_SECRETS:
            assert first.shell[secret] != second.shell[secret], f"{name}: {secret} is not random"
            assert len(first.shell[secret]) >= 32, f"{name}: {secret} is too short"

    def test_no_value_is_generated_inside_the_env_heredoc(self, name: str) -> None:
        """The shape rule behind the three cases above, stated once: a line in the
        heredoc may only reference a value the script already holds."""
        body = _env_prologue(SELFDEPLOY / name).splitlines()
        start = next(i for i, line in enumerate(body) if _ENV_HEREDOC_OPEN.match(line))
        offenders = [line for line in body[start + 1 :] if "$(" in line or "`" in line]
        assert offenders == [], f"{name}: the .env heredoc generates values: {offenders}"


# --------------------------------------------------------------------------
# 6. an aborted self-deploy smoke reports its own exit status
# --------------------------------------------------------------------------


@_selfdeploy_smoke
@pytest.mark.parametrize("name", SELFDEPLOY_SMOKES)
class TestAnAbortedSelfDeploySmokeReportsItsOwnExitStatus:
    """A caller reading only `$?` must be able to tell a pass from an abort.

    Every smoke arms an EXIT trap to tear its stack down, and a trap that does not
    exit hands the script the status of its OWN last command: `rm -rf "$WORK"`
    succeeds, so an aborted run reported 0 and a CI step gated on `$?` went green
    on a smoke that never reached an assertion.

    Re-raising the status the trap was entered with is necessary but not
    sufficient. `/bin/bash` on macOS is 3.2, and it enters the EXIT trap with `$?`
    already 0 when `set -u` aborts on an unbound variable -- exactly the abort that
    was observed -- so on that shell the aborting status cannot be recovered from
    `$?` at all. What survives is the absence of the completion stamp, which is
    why the smokes carry both: the status when there is one, the stamp otherwise.
    """

    def test_an_abort_after_the_trap_is_installed_exits_non_zero(
        self, name: str, tmp_path: Path
    ) -> None:
        """The observed failure: `set -u` fires on an unbound name and the run is
        over, having asserted nothing. It must not look like a pass."""
        run = _run_script(SELFDEPLOY / name, tmp_path, f'\necho "${_UNSET_REFERENCE}"\n')
        assert "unbound variable" in run.stderr, f"{name}: the abort never fired\n{run.stderr}"
        assert run.returncode != 0, (
            f"{name}: a `set -u` abort reported success -- a caller reading $? "
            f"cannot tell it from a passing smoke\n{run.stderr}"
        )

    def test_a_run_that_reaches_its_end_still_exits_zero(self, name: str, tmp_path: Path) -> None:
        """The negative case: the trap must not turn a finished run red."""
        run = _run_script(SELFDEPLOY / name, tmp_path, f"\n{_COMPLETION_STAMP}=1\n")
        assert run.returncode == 0, f"{name}: a completed run was reported as failed\n{run.stderr}"

    def test_an_explicit_status_reaches_the_caller_unchanged(
        self, name: str, tmp_path: Path
    ) -> None:
        """`fail()` exits 1 and a missing binary exits 127; the trap re-raises what
        it was entered with rather than flattening every abort to the same code."""
        run = _run_script(SELFDEPLOY / name, tmp_path, "\nexit 42\n")
        assert run.returncode == 42, f"{name}: exit 42 came back as {run.returncode}\n{run.stderr}"

    def test_the_cleanup_stamps_and_re_raises(self, name: str) -> None:
        """Stated as shape too, because the three cases above run on bash and the
        smokes also run on hosts this suite never sees."""
        body = (SELFDEPLOY / name).read_text(encoding="utf-8")
        assert "local status=$?" in body, f"{name}: cleanup does not capture its entry status"
        assert 'exit "$status"' in body, f"{name}: cleanup does not re-raise the status"
        assert f"{_COMPLETION_STAMP}=1" in body, f"{name}: the run never stamps its completion"


# --------------------------------------------------------------------------
# 5. Alkera Files: the second durable store and the second hostname
# --------------------------------------------------------------------------
#: Sized for Files, not for the portal: a single-call PUT carries up to 64 MiB
#: (`files_single_put_max_bytes`) and a large download runs for hours, while an
#: ingress controller's defaults are ~1 MiB and 60 s.
FILES_INGRESS_ANNOTATIONS = {
    "nginx.ingress.kubernetes.io/proxy-body-size": "64m",
    "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
    "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
}
FILES_CONTENT_HOST = "https://c.acme-content.example"
#: Files on, with an S3-compatible store, the content host and both ingresses.
FILES_ON = (
    "--set",
    "files.enabled=true",
    "--set",
    f"files.contentHost={FILES_CONTENT_HOST}",
    "--set",
    "files.store.provider=s3_compatible",
    "--set",
    "files.store.bucket=alkera-files",
    "--set",
    "files.store.endpoint=http://t-alkera-files-store:8333",
    "--set",
    "ingress.enabled=true",
    "--set",
    "ingress.host=alkera.acme.example",
)


class TestFilesIsOnByDefaultAndCarriesItsOwnStore:
    """Files adds a durable store Postgres cannot reconstruct and a hostname the
    session cookie must never reach. Both shapes have to describe the same thing,
    and a deployment that never asked for Files must render as it did before.
    """

    def test_the_compose_file_defaults_files_on_and_keeps_the_content_origin_separate(
        self,
    ) -> None:
        env = _compose()["x-app-env"]
        assert env["FILES_ENABLED"] == "${FILES_ENABLED:-true}"
        # No default for the content origin: an operator must name a second
        # hostname (compose refuses it unset), and the production validator
        # refuses an empty one while Files is on.
        assert env["FILES_CONTENT_BASE_URL"].startswith("${FILES_CONTENT_BASE_URL?")
        assert env["FILES_CONTENT_BASE_URL"] != env["FRONTEND_BASE_URL"]

    def test_the_spa_container_learns_the_content_origin_for_its_policy(self) -> None:
        """The SPA's Content-Security-Policy has to name the content origin or every
        preview is blocked: the readers fetch the signed URL with CORS, a stored PDF
        is framed from it and media streams from it. The web image derives those
        sources from FILES_CONTENT_BASE_URL, so the container must be handed the same
        value the API gets -- from the same variable, so an operator sets it once."""
        env = _services()["web"]["environment"]
        assert env["FILES_CONTENT_BASE_URL"] == _compose()["x-app-env"]["FILES_CONTENT_BASE_URL"]
        assert "FILES_CONTENT_BASE_URL" in WEB_ENTRYPOINT.read_text(encoding="utf-8"), (
            "the image no longer reads the variable the compose file hands it"
        )

    def test_the_compose_store_defaults_to_a_volume_not_a_scratch_path(self) -> None:
        """`filesystem` on a named volume is the default self-hosted shape. A
        default under a scratch directory would be wiped on reboot -- the app
        validator refuses one, so the reference file must not ship one."""
        env = _compose()["x-app-env"]
        assert env["FILES_STORE_PROVIDER"] == "${FILES_STORE_PROVIDER:-filesystem}"
        root = env["FILES_STORE_ROOT"]
        assert root == "${FILES_STORE_ROOT:-/var/lib/alkera/files}"
        assert "/tmp" not in root

    @pytest.mark.parametrize("service", ["backend", "worker"])
    def test_every_service_that_writes_objects_mounts_the_durable_volume(
        self, service: str
    ) -> None:
        """The filesystem driver is one tree shared by every writer. A service
        writing to its container filesystem loses committed bytes on restart."""
        mounts = _services()[service].get("volumes", [])
        assert "alkera_files_data:/var/lib/alkera/files" in mounts, (
            f"{service} does not mount the Files volume at FILES_STORE_ROOT"
        )
        assert "alkera_files_data" in _compose()["volumes"]

    def test_the_bundled_store_is_opt_in_and_never_published(self) -> None:
        """Its S3 port reaches every tenant's objects behind one static key pair
        with no per-request authorization, so it stays on the internal network."""
        store = _services()["seaweedfs"]
        assert store["profiles"] == ["files-store"]
        assert "ports" not in store, "the bundled store must not publish its S3 port"

    @needs_compose
    def test_docker_compose_resolves_the_file_with_and_without_the_store_profile(
        self, tmp_path: Path
    ) -> None:
        default = _compose_config(tmp_path)
        assert "seaweedfs" not in default["services"]
        with_store = _compose_config(tmp_path, "--profile", "files-store")
        assert "seaweedfs" in with_store["services"]


def _nginx_location(pattern: str) -> str:
    """The body of the first `location <pattern> { ... }` block, brace-matched.

    Read from the template rather than a rendered container: the two variables
    `docker-entrypoint.sh` substitutes are the backend URL and the CSP, neither
    of which appears in a limit, so the template carries the real numbers.
    """
    text = WEB_NGINX.read_text()
    head = text.index(f"location {pattern}")
    start = text.index("{", head)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    raise AssertionError(f"location {pattern} is never closed")


#: Every regex location the SPA proxy relaxes, and the ceiling each one twins in the
#: backend's pre-buffer guard (``GUARDED_PREFIXES`` in ``backend.api.body_limit``): the
#: upload prefix carries a proxied part plus envelope headroom, the drives suffixes the
#: largest single-call content PUT. Each edge cap must sit at or above the app's, so the
#: app's typed 4xx is what a client sees -- and finite, because the guard bounds these
#: patterns only, never the ``/api/v1/files/`` prefix they sit under.
FILES_NGINX_LOCATIONS = {
    "^/api/v1/files/uploads/": 136 * 1024 * 1024,
    "^/api/v1/files/drives/.+/(content|bulk|tree|snapshots|lease/release)$": 64 * 1024 * 1024,
}

#: Files routes that take a body and that the guard does NOT bound. nginx's default cap
#: is their only bound, so no relaxed location may claim them.
UNGUARDED_FILES_PATHS = (
    "/api/v1/files/uploads",
    "/api/v1/files/drives/d1/items/i1/children",
    "/api/v1/files/drives/d1/items/i1/lease/live",
    "/api/v1/files/drives/d1/items/i1/lease/heartbeat",
    "/api/v1/files/drives/d1/items/i1/permissions",
    "/api/v1/files/drives/d1/items/i1/copy",
    "/api/v1/files/drives/d1/items/i1/duplicate",
    "/api/v1/files/drives/d1/items/i1/content-grants",
)


def _nginx_bytes(value: str) -> int:
    """Bytes for an nginx size (`64m`, `512k`, `1g`; a bare number is bytes)."""
    match = re.fullmatch(r"(\d+)([kmg]?)", value.lower())
    assert match, f"not an nginx size: {value}"
    return int(match[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match[2]]


class TestTheSpaProxyCarriesAnUploadPart:
    """An upload part is PROXIED -- the client PUTs it to
    /api/v1/files/uploads/<session>/parts/<n> and the SPA's nginx carries the
    body to the backend. nginx's defaults refuse it: `client_max_body_size` is
    1 MiB, below even one part, and `proxy_request_buffering` is on, which
    spools every part to the container's disk before the backend sees a byte.
    """

    def test_the_files_locations_are_matched_before_the_general_api_one(self) -> None:
        """nginx takes the FIRST matching regex location in file order, so a
        Files block defined after the general `^/(api|admin/v1)/` one would
        never run and every part PUT would inherit the 1 MiB default."""
        text = WEB_NGINX.read_text()
        general_at = text.index("location ~ ^/(api|admin/v1)/")
        for pattern in FILES_NGINX_LOCATIONS:
            assert text.index(f"location ~ {pattern}") < general_at, (
                f"the general API location is matched before {pattern}, so its limits never apply"
            )

    @pytest.mark.parametrize(
        ("pattern", "app_cap"),
        sorted(FILES_NGINX_LOCATIONS.items()),
        ids=["uploads", "drives-bodies"],
    )
    def test_a_guarded_files_location_takes_its_body_without_a_disk_spool(
        self, pattern: str, app_cap: int
    ) -> None:
        body = _nginx_location(f"~ {pattern}")
        match = re.search(r"client_max_body_size\s+(\S+);", body)
        assert match, "nginx's 1 MiB default body cap still applies to these bodies"
        cap = _nginx_bytes(match[1].rstrip(";"))
        # Finite, and no smaller than the app's own ceiling: the app refuses a larger
        # body itself, from the Content-Length, so nginx's unexplained 413 is
        # unreachable -- while a body the app does not bound is still bounded here.
        assert app_cap <= cap <= 2 * app_cap, f"{pattern} caps bodies at {cap} bytes"
        assert re.search(r"proxy_request_buffering\s+off;", body), (
            "every part would be spooled to disk before the backend sees it"
        )
        assert re.search(r"proxy_read_timeout\s+3600s;", body)
        assert re.search(r"proxy_send_timeout\s+3600s;", body)
        assert "proxy_pass" in body, "the Files location does not reach the backend"

    @pytest.mark.parametrize("path", UNGUARDED_FILES_PATHS)
    def test_an_unguarded_files_route_is_claimed_by_no_relaxed_location(self, path: str) -> None:
        """The pre-buffer guard bounds the patterns above and nothing else, and FastAPI
        buffers the whole body into memory before the auth dependency runs. A cap lifted
        across the `/api/v1/files/` prefix would therefore hand an anonymous caller an
        unbounded write on every Files route the guard does not cover."""
        claimed = [p for p in FILES_NGINX_LOCATIONS if re.search(p, path)]
        assert not claimed, f"{path} is served by {claimed}, which lifts its body cap"

    def test_the_rest_of_the_api_keeps_the_default_body_cap(self) -> None:
        """The relaxation is scoped to the guarded Files patterns. A body cap lifted
        across the whole API would let any unauthenticated route take an unbounded
        body -- and so would one lifted across the whole Files prefix."""
        body = _nginx_location("~ ^/(api|admin/v1)/")
        assert "client_max_body_size" not in body
        assert "proxy_request_buffering" not in body


#: The chat turn locations and the app cap they restate.
CHAT_NGINX_LOCATION = "^/api/v1/chats/.+/(messages|answer)$"


class TestTheSelfHostedProxyAdmitsAChatTurn:
    """A typed message may run to `chat_message_max_chars` (a million characters,
    so a pasted log or schema fits). nginx's 1 MiB default turns that into a 413
    the person cannot explain, and the text they typed is lost."""

    def test_the_chat_location_is_matched_before_the_general_api_one(self) -> None:
        """nginx takes the FIRST matching regex location in file order, so a chat
        block defined after `^/(api|admin/v1)/` would never run."""
        text = WEB_NGINX.read_text()
        assert text.index(f"location ~ {CHAT_NGINX_LOCATION}") < text.index(
            "location ~ ^/(api|admin/v1)/"
        )

    def test_the_chat_location_admits_the_body_the_app_admits(self) -> None:
        """The cap restates the guard's own maximum rather than lifting the bound:
        at or above it the app answers first with its typed 4xx, so nginx's
        unexplained 413 is unreachable."""
        body = _nginx_location(f"~ {CHAT_NGINX_LOCATION}")
        match = re.search(r"client_max_body_size\s+(\S+);", body)
        assert match, "nginx's 1 MiB default still refuses a long pasted message"
        cap = _nginx_bytes(match[1].rstrip(";"))
        assert cap >= CHAT_BODY_MAX_BYTES
        assert cap <= 2 * CHAT_BODY_MAX_BYTES
        assert "proxy_pass" in body

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/api/v1/chats", id="list"),
            pytest.param("/api/v1/chats/c1", id="one-chat"),
            pytest.param("/api/v1/chats/c1/stop", id="stop"),
        ],
    )
    def test_no_other_chats_route_is_claimed_by_the_relaxed_location(self, path: str) -> None:
        """The guard bounds the two turn bodies and nothing else, so every other
        chats route must keep the general location's default cap."""
        assert not re.search(CHAT_NGINX_LOCATION, path)


# --- the web container's access log carries no credential -------------------

#: nginx variables that put a credential in a log line: the request line and the
#: raw URI carry the query string (`?invite_token=`, `?ticket=`, `?invite=`), and
#: the Referer carries the page URL a credential rode in on.
_CREDENTIAL_LOG_VARIABLES = re.compile(
    r"\$(request|request_uri|args|query_string|is_args|http_referer|arg_\w+)(?![A-Za-z0-9_])"
)

_NGINX_TOKEN = "kQ3v_9Zx1LmN-tRs7Yb2Wc4Ee6Gg8Ii0Kk2Mm4Oo6Qq8S"


def _nginx_log_formats() -> dict[str, str]:
    """Every `log_format name '...' '...';` in the template, joined per name."""
    text = WEB_NGINX.read_text(encoding="utf-8")
    formats: dict[str, str] = {}
    for match in re.finditer(r"^\s*log_format\s+(\w+)\s+(.*?);\s*$", text, flags=re.M | re.S):
        formats[match.group(1)] = "".join(re.findall(r"'([^']*)'", match.group(2)))
    return formats


def _nginx_map(source: str) -> list[tuple[str, str]]:
    """The (regex, value) arms of the `map <source> ...` block, in order."""
    text = WEB_NGINX.read_text(encoding="utf-8")
    block = re.search(rf"map \${source} \$\w+ \{{(.*?)\n\}}", text, flags=re.S)
    assert block, f"no map over ${source}"
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(r'^\s*"~([^"]+)"\s+"?([^";]+)"?;', block.group(1), flags=re.M)
    ]


def _apply_nginx_map(source: str, value: str) -> str | None:
    for pattern, replacement in _nginx_map(source):
        match = re.search(pattern.replace("(?<", "(?P<"), value)
        if match:
            groups = match.groupdict()
            return re.sub(r"\$(\w+)", lambda v, g=groups: g[v.group(1)], replacement)
    return None


def test_the_web_access_log_format_never_holds_the_query_or_the_referer() -> None:
    formats = _nginx_log_formats()
    assert formats, "the template must declare its own access log format"
    for name, body in formats.items():
        leaked = _CREDENTIAL_LOG_VARIABLES.findall(body)
        assert not leaked, f"log_format {name} logs {leaked}"


def test_every_access_log_in_the_web_server_uses_a_credential_free_format() -> None:
    """Declaring `access_log` in the server replaces the image's inherited
    `main` format, which logs `$request` and `$http_referer`."""
    text = WEB_NGINX.read_text(encoding="utf-8")
    directives = re.findall(r"^\s*access_log\s+([^;]+);", text, flags=re.M)
    assert directives, "no access_log: the base image's `main` format would apply"
    formats = _nginx_log_formats()
    for directive in directives:
        parts = directive.split()
        assert parts[0] == "off" or (len(parts) >= 2 and parts[1] in formats), directive


@pytest.mark.parametrize(
    ("request_uri", "logged"),
    [
        pytest.param(f"/signup?invite={_NGINX_TOKEN}", "/signup", id="spa-invite-link"),
        pytest.param(
            f"/api/v1/auth/oauth/google/start?intent=signup&invite_token={_NGINX_TOKEN}",
            "/api/v1/auth/oauth/google/start",
            id="oauth-start",
        ),
        pytest.param(
            f"/api/v1/auth/oauth/register/context?ticket={_NGINX_TOKEN}",
            "/api/v1/auth/oauth/register/context",
            id="register-context",
        ),
        pytest.param(
            f"/reset-password/{_NGINX_TOKEN}", "/reset-password/[redacted]", id="spa-reset-link"
        ),
        pytest.param(
            f"/api/v1/invitations/by-token/{_NGINX_TOKEN}",
            "/api/v1/invitations/by-token/[redacted]",
            id="invitation-preview",
        ),
        pytest.param(
            f"/api/v1/auth/verify-email/{_NGINX_TOKEN}",
            "/api/v1/auth/verify-email/[redacted]",
            id="verify-email",
        ),
        pytest.param(
            "/api/v1/auth/password-reset/request",
            "/api/v1/auth/password-reset/request",
            id="reset-request-sibling-stays-readable",
        ),
        pytest.param("/health/ready", "/health/ready", id="health"),
    ],
)
def test_the_logged_path_drops_the_query_and_redacts_a_token_segment(
    request_uri: str, logged: str
) -> None:
    formats = _nginx_log_formats()
    assert any("$alkera_log_path" in body for body in formats.values())
    path = _apply_nginx_map("request_uri", request_uri)
    assert path is not None
    result = _apply_nginx_map("alkera_request_path", path) or path
    assert result == logged
    assert _NGINX_TOKEN not in result
