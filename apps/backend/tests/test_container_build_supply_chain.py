"""What the backend image is built FROM, and what checks its dependencies.

Two contracts, both about things a code review cannot see:

1. Every image the backend image's Dockerfile pulls content out of is
   addressed by digest. A
   registry tag is a pointer its owner (or anyone who takes the namespace) can
   re-aim at different bytes, and the build that consumes it runs
   on a merge with an empty repository diff. The `uv` binary is the sharpest case
   — it is what resolves and installs the entire production dependency closure,
   so it sits ABOVE uv.lock in the trust chain — but the rule is the same for any
   `FROM` or `COPY --from`.

2. The repository has a dependency-vulnerability gate that reads the lockfiles.
   Image scanning alone needs a docker build to say anything, which is why the
   existing `make scan` never made it into a workflow; a lockfile scan runs in
   seconds anywhere and so has no excuse not to be wired in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
#: The open backend image (Databench/). A layer built over it pins its own pulls
#: and carries its own copy of this check.
BACKEND_DOCKERFILES = (REPO_ROOT / "Databench/apps/backend/Dockerfile",)
#: Build contexts a Dockerfile takes by name (`--build-context open=...`): the
#: build names the image, not the Dockerfile, so they are not registry pulls.
NAMED_CONTEXTS = {"open"}
MAKEFILE = REPO_ROOT / "Makefile"

#: `FROM <image>` / `COPY --from=<image>` where the image is a registry
#: reference rather than a named earlier stage.
_IMAGE_REF = re.compile(
    r"^\s*(?:FROM\s+(?P<from>\S+)|COPY\s+--from=(?P<copy>\S+))",
    re.IGNORECASE | re.MULTILINE,
)

# Stage names introduced by `FROM ... AS <name>`; a `COPY --from=<name>` that
# points at one of those is an intra-build reference, not a registry pull.
_STAGE_NAMES = re.compile(r"^\s*FROM\s+\S+\s+AS\s+(\S+)", re.IGNORECASE | re.MULTILINE)


def _dockerfile() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in BACKEND_DOCKERFILES)


def _external_image_refs(text: str) -> list[str]:
    stages = {name.lower() for name in _STAGE_NAMES.findall(text)}
    refs: list[str] = []
    for match in _IMAGE_REF.finditer(text):
        ref = match.group("from") or match.group("copy")
        if ref.lower() in stages or ref in NAMED_CONTEXTS or ref.startswith("$"):
            continue
        refs.append(ref)
    return refs


def test_the_dockerfile_pulls_from_more_than_one_image() -> None:
    """Sanity: if the scan below ever finds nothing, its verdict is meaningless."""
    assert len(_external_image_refs(_dockerfile())) >= 3


@pytest.mark.parametrize(
    "ref",
    _external_image_refs(_dockerfile()),
    ids=lambda ref: ref.split("@")[0],
)
def test_every_external_image_is_pinned_by_digest(ref: str) -> None:
    """A tag is a mutable pointer. Re-aiming `0.8.17` at a trojaned build changes
    what the next deploy ships with nothing to review in the repository, and
    neither immutable ECR tags nor scan-on-push notice a substituted toolchain."""
    assert "@sha256:" in ref, (
        f"{ref} is pinned by tag only — add @sha256:<digest> (keep the tag for readability)"
    )
    digest = ref.split("@sha256:", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{ref} has a malformed digest"


def test_the_uv_binary_comes_from_a_pinned_image() -> None:
    """Named explicitly because this one is not just another base layer: the
    binary copied here runs `uv sync` and installs everything the service imports."""
    refs = [ref for ref in _external_image_refs(_dockerfile()) if "astral-sh/uv" in ref]
    assert refs, "the builder no longer copies uv from an image — re-point this test"
    for ref in refs:
        assert "@sha256:" in ref


def _makefile_recipe(target: str) -> str:
    """The recipe body of a Make target — the tab-indented lines under it."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:.*$", text, re.MULTILINE)
    assert match, f"no `{target}` target in the Makefile"
    body: list[str] = []
    for line in text[match.end() :].splitlines()[1:]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


def test_a_lockfile_vulnerability_scan_target_exists() -> None:
    """The gate a workflow can call without building an image first."""
    recipe = _makefile_recipe("scan-deps")
    assert recipe.strip(), "`scan-deps` has an empty recipe"


@pytest.mark.parametrize(
    "lockfile",
    [
        pytest.param("uv.lock", id="python"),
        pytest.param("pnpm-lock.yaml", id="node"),
    ],
)
def test_the_dependency_scan_covers_every_lockfile(lockfile: str) -> None:
    """Every third-party version that reaches an image is resolved in one of
    these; a scan that reads only one half leaves the other unwatched."""
    assert (REPO_ROOT / lockfile).is_file()
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^DEP_LOCKFILES\s*:?=\s*(?P<value>.+)$", text, re.MULTILINE)
    assert match, "the scan no longer declares which lockfiles it reads"
    assert lockfile in match.group("value").split()


@pytest.mark.parametrize(
    "target",
    [pytest.param("scan-deps", id="lockfiles"), pytest.param("scan", id="images")],
)
def test_a_scan_failure_fails_the_build(target: str) -> None:
    """A gate that swallows its scanner's exit status is decoration. Neither a
    leading `-` (make's ignore-errors prefix) nor a trailing `|| true` may appear
    on the line that runs the scanner."""
    for line in _makefile_recipe(target).splitlines():
        body = line.lstrip("\t")
        if body.startswith("@command -v") or not body:
            continue  # the tool-installed precondition, which exits 1 on its own
        assert "|| true" not in body, f"`{target}` swallows a failure: {body.strip()}"
        assert not body.lstrip("@").startswith("-"), (
            f"`{target}` ignores the scanner's exit status: {body.strip()}"
        )


def test_the_image_scan_still_fails_on_high_severity() -> None:
    assert "--fail-on $(GRYPE_FAIL_ON)" in _makefile_recipe("scan")
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^GRYPE_FAIL_ON\s*\?=\s*(high|critical)\s*$", text, re.MULTILINE)


def test_one_target_runs_both_halves_of_the_gate() -> None:
    """So a workflow (or a human before a release) has a single entry point that
    cannot be satisfied by running the cheaper half."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^scan-all:\s*(?P<deps>.+?)(?:##.*)?$", text, re.MULTILINE)
    assert match, "no `scan-all` target"
    prerequisites = match.group("deps").split()
    assert "scan-deps" in prerequisites
    assert "scan" in prerequisites
