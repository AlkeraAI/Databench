"""Where a node's bootstrap gets the daemon it installs: one seam, two sources.

A :class:`NodeDaemonSource` renders the bootstrap lines that leave the daemon's
tarball at ``$work/release.tar.gz`` and its digest in ``$expected``; the lines
after them (verify, unpack ``alkera.dist``, hand it to root) are the
bootstrap's own and the same for every source.

- :data:`SERVED_BUNDLE`, the default: the bundle this deployment built from
  source (``scripts/build-node-bundle.sh``). It uses a bundle a provider
  already staged on the host, else fetches it from the backend with the node's
  machine credential, which never goes on argv.
- :data:`RELEASE_DOWNLOAD`: the release host's build
  (``ALKERA_NODE_RELEASE_BASE_URL``), at the version the backend resolved
  before rendering (``$DAEMON_VERSION``). A distribution that ships releases
  registers it at composition through :data:`DAEMON_SOURCES`.

The point admits one source: two would leave the choice to registration order.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Final, Protocol

from alkera_core.auth.machine_token import MACHINE_CREDENTIAL_HEADER
from alkera_core.extensions import Extension, ExtensionError, ExtensionPoint

#: Where a provider stages a bundle on the host before the bootstrap runs, by
#: target, with ``.sha256`` beside it (``$BOX_ROOT`` is ``/opt/alkera``).
STAGED_BUNDLE = "$BOX_ROOT/node-bundle-$TARGET.tar.gz"
#: The backend route a node fetches its bundle from, by target.
BUNDLE_ROUTE = "/api/v1/machines/node-bundle"

_TARGET_LINE = (
    'case "$(uname -m)" in aarch64|arm64) TARGET=linux-arm64 ;; *) TARGET=linux-x64 ;; esac'
)
_EXPECTED_LINE = 'expected="$(grep -oE \'[0-9a-f]{64}\' "$work/release.sha256" | head -n1)"'


class NodeDaemonSource(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def needs_release_base(self) -> bool:
        """Whether the bootstrap needs a release base URL to render."""
        ...

    def release_base(self, configured: str) -> str:
        """The release base the bootstrap fetches from: ``configured`` (the
        deployment's ``ALKERA_NODE_RELEASE_BASE_URL``), else the source's own
        default. Empty for a source with neither."""
        ...

    def fetch_lines(self, *, api_url: str) -> list[str]:
        """Lines that leave the tarball at ``$work/release.tar.gz`` and its
        digest in ``$expected``."""
        ...

    def after_install_lines(self) -> list[str]:
        """Lines run once ``$BOX_ROOT/alkera.dist`` is installed: they leave
        the installed build's version in ``$DAEMON_VERSION``."""
        ...


@dataclass(frozen=True)
class ReleaseDownload:
    name: str = "release"
    needs_release_base: bool = True
    #: The release host a distribution ships its builds from, used when the
    #: deployment configures none. Empty, the deployment must name one.
    default_base: str = ""

    def release_base(self, configured: str) -> str:
        return configured or self.default_base

    def fetch_lines(self, *, api_url: str) -> list[str]:
        return [
            "# The released daemon the backend resolved, verified before it is installed.",
            'case "$DAEMON_VERSION" in *[!0-9A-Za-z.-]*|"")'
            ' echo "refusing daemon version $DAEMON_VERSION"; exit 1 ;; esac',
            _TARGET_LINE,
            'RELEASE_URL="$RELEASE_BASE/cli/v$DAEMON_VERSION/$TARGET/alkera-$TARGET.tar.gz"',
            'work="$(mktemp -d)"',
            'curl -fsSL --retry 5 "$RELEASE_URL.sha256" -o "$work/release.sha256"',
            _EXPECTED_LINE,
            'curl -fsSL --retry 5 "$RELEASE_URL" -o "$work/release.tar.gz"',
        ]

    def after_install_lines(self) -> list[str]:
        # The backend resolved the version before it rendered the script.
        return []


@dataclass(frozen=True)
class ServedBundle:
    name: str = "served"
    needs_release_base: bool = False

    def release_base(self, configured: str) -> str:
        return configured

    def fetch_lines(self, *, api_url: str) -> list[str]:
        return [
            "# The node bundle this deployment built from source: staged on the host by",
            "# its provider, else fetched from the backend with the machine credential",
            "# (read from a 0600 header file, never on argv).",
            _TARGET_LINE,
            f"API_BASE={shlex.quote(api_url.rstrip('/'))}",
            f'STAGED_BUNDLE="{STAGED_BUNDLE}"',
            'work="$(mktemp -d)"',
            'if [ -f "$STAGED_BUNDLE" ] && [ -f "$STAGED_BUNDLE.sha256" ]; then',
            '  mv "$STAGED_BUNDLE" "$work/release.tar.gz"',
            '  mv "$STAGED_BUNDLE.sha256" "$work/release.sha256"',
            "else",
            '  header="$(mktemp)"',
            f"  printf '%s: %s\\n' '{MACHINE_CREDENTIAL_HEADER}'"
            ' "$MACHINE_CREDENTIAL" >"$header"',
            f'  BUNDLE_URL="$API_BASE{BUNDLE_ROUTE}/$TARGET"',
            '  curl -fsSL --retry 5 -H "@$header" "$BUNDLE_URL.sha256" -o "$work/release.sha256"'
            ' || { rm -f "$header"; echo "the backend served no node bundle"; exit 1; }',
            '  curl -fsSL --retry 5 -H "@$header" "$BUNDLE_URL" -o "$work/release.tar.gz"'
            ' || { rm -f "$header"; echo "the backend served no node bundle"; exit 1; }',
            '  rm -f "$header"',
            "fi",
            _EXPECTED_LINE,
        ]

    def after_install_lines(self) -> list[str]:
        return [
            'DAEMON_VERSION="$(cat "$BOX_ROOT/alkera.dist/VERSION" 2>/dev/null || true)"',
            'case "$DAEMON_VERSION" in *[!0-9A-Za-z.+-]*|"")'
            ' echo "refusing bundle version $DAEMON_VERSION"; exit 1 ;; esac',
        ]


RELEASE_DOWNLOAD: Final[NodeDaemonSource] = ReleaseDownload()
SERVED_BUNDLE: Final[NodeDaemonSource] = ServedBundle()

#: The source a distribution registers; empty means :data:`SERVED_BUNDLE`.
DAEMON_SOURCES: ExtensionPoint[NodeDaemonSource] = ExtensionPoint("node_daemon_source")


def daemon_source() -> NodeDaemonSource:
    """The source this process's bootstraps render with."""
    registered = DAEMON_SOURCES.items()
    if len(registered) > 1:
        raise ExtensionError("more than one node daemon source is registered")
    return registered[0] if registered else SERVED_BUNDLE


def _install_release_download() -> None:
    DAEMON_SOURCES.register(RELEASE_DOWNLOAD)


#: A distribution that ships its node daemon from a release host installs this.
RELEASE_DOWNLOAD_EXTENSION = Extension(
    name="alkera.node-daemon-release", install=_install_release_download
)


__all__ = [
    "BUNDLE_ROUTE",
    "DAEMON_SOURCES",
    "RELEASE_DOWNLOAD",
    "RELEASE_DOWNLOAD_EXTENSION",
    "SERVED_BUNDLE",
    "STAGED_BUNDLE",
    "NodeDaemonSource",
    "ReleaseDownload",
    "ServedBundle",
    "daemon_source",
]
