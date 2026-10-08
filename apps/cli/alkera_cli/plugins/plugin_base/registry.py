"""``PluginRegistry`` — discover · register · activate · typed surface lookup.

One per ``HarnessRuntime`` (per project, per process — verified). It holds the
registered surface map; it is NOT a daemon singleton and does not outlive the
process → persistent plugin state lives in ``.alkera/``, never in this object.

It discovers plugins (built-ins + injected extras), matches activation on
``always`` / ``workspace_contains``, serves the typed ``providers()`` lookup,
and discovers connections on enable. The tool selection funnel lives on
``tool_registry()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from alkera_core.connections import Outcome
from alkera_core.naming import handle_error, is_safe_handle

from alkera_cli.contracts.tool_types import CredentialRef, Effect
from alkera_cli.plugins.plugin_base.connection import (
    Connection,
    ConnectionBuildResult,
    normalize_connection_build,
)
from alkera_cli.plugins.plugin_base.connection_state import outcome_of
from alkera_cli.plugins.plugin_base.connections_store import AddedConnectionsStore
from alkera_cli.plugins.plugin_base.credential_manager import (
    named_credential_refs,
    with_named_credential_refs,
)
from alkera_cli.plugins.plugin_base.derivation import (
    DetectedKey,
    connection_info,
    detected_key,
    keyed_detected,
)
from alkera_cli.plugins.plugin_base.enablement import PluginEnablementStore
from alkera_cli.plugins.plugin_base.fs_discovery import IGNORED_DIRS
from alkera_cli.plugins.plugin_base.oauth_sign_in import oauth_sign_in
from alkera_cli.plugins.plugin_base.plugin import (
    CLI_PLUGINS,
    ActivationSpec,
    Plugin,
    PluginInfo,
    WorkspaceEvent,
)
from alkera_cli.plugins.plugin_base.registration import (
    PLUGIN_REGISTRATION_CHECKS,
    RecordingRegistrar,
)
from alkera_cli.plugins.plugin_base.surfaces import (
    PROBE_RESULT_ATTRIBUTE,
    AgentDefinition,
    SkillDef,
    WorkspaceContext,
    probe_result_payload,
)
from alkera_cli.plugins.plugin_base.team_probe_cache import persist_team_probe, restore_team_probe
from alkera_cli.plugins.plugin_base.tool import Tool, ToolRegistry

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base.capabilities import CapabilitySet
    from alkera_cli.plugins.plugin_base.connection import CredentialManager
    from alkera_cli.plugins.plugin_base.surfaces import (
        ProbeProvider,
    )
    from alkera_cli.plugins.plugin_base.team_connections import (
        TeamConnectionRecord,
        TeamMemberState,
    )

logger = logging.getLogger(__name__)

P = TypeVar("P")

#: Directories never worth scanning for an activation marker — the SHARED prune set
#: (``fs_discovery.IGNORED_DIRS``) the connection-discovery walk also uses, so the two
#: scans can't diverge (a marker found here must be discoverable there too). Pruning
#: these keeps activation fast and stops a marker name from matching a vendored copy
#: (e.g. a ``dbt_project.yml`` buried in ``node_modules``).
_IGNORED_DIRS = IGNORED_DIRS
#: Hard cap so activation can't hang on a pathological/giant tree.
_MAX_WALK_ENTRIES = 50_000


def _validate_connection_handle(handle: str) -> None:
    """Reject a connection handle that isn't a safe filesystem slug (raises
    ``ValueError``). A handle becomes a path component of its credential file, so
    it goes through the shared ``alkera_core.naming`` rule that the backend save
    boundary enforces too: a preconfigured name that saves there is always a name
    this side can materialize."""
    if not is_safe_handle(handle):
        raise ValueError(handle_error(handle))


def _require_filled(plugin: Plugin, auth_method: str, fields: dict[str, str]) -> None:
    """Reject a form submission that left a required input blank (raises
    ``ValueError``). A connector's ``build_connection`` builds the connection's
    SHAPE and is not a presence check, because a team member's
    machine builds the same shape while its secret sits in a credential file.
    So presence is asked once, of the form schema, for every connector alike."""
    from alkera_core.connectors.catalog import (
        form_fields,
        length_error,
        missing_required,
        overlong_values,
        required_error,
    )

    schema = plugin.connection_form_schema()
    if schema is None:
        return
    specs = form_fields(schema, auth_method)
    missing = missing_required(specs, fields)
    if missing:
        raise ValueError(required_error(missing))
    overlong = overlong_values(specs, fields)
    if overlong:
        raise ValueError(length_error(overlong))


def _restore_secret(path: Path, data: bytes) -> None:
    """Rewrite a rolled-back credential file owner-only (0o600), atomically enough for
    a single-writer rollback — the original bytes replace whatever a failed edit wrote."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class _SecretSnapshot:
    """The bytes a credential path held before one registry operation changed it."""

    path: Path
    contents: bytes | None


def _snapshot_secret(path: Path) -> _SecretSnapshot:
    """Capture a credential path so a failed multi-secret operation can roll back."""
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        contents = None
    return _SecretSnapshot(path=path, contents=contents)


def _restore_secret_snapshots(snapshots: Sequence[_SecretSnapshot]) -> None:
    """Restore credential paths in reverse write order after a failed operation."""
    for snapshot in reversed(snapshots):
        if snapshot.contents is None:
            with contextlib.suppress(OSError):
                snapshot.path.unlink()
            continue
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        _restore_secret(snapshot.path, snapshot.contents)


def _iter_workspace_paths(root: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(relative_posix_path, basename)`` for every file under ``root``,
    pruning ``_IGNORED_DIRS`` and NOT descending through symlinked directories
    (loop-safe), bounded to ``_MAX_WALK_ENTRIES`` entries."""
    root = Path(root)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        # Prune in place BEFORE descending: drop ignored + symlinked subdirs.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _IGNORED_DIRS and not os.path.islink(os.path.join(dirpath, d))
        ]
        base = Path(dirpath)
        for name in filenames:
            yield (base / name).relative_to(root).as_posix(), name
            seen += 1
            if seen >= _MAX_WALK_ENTRIES:
                # Surface the cap: beyond here an activation marker would be missed,
                # so a plugin silently not activating is at least diagnosable.
                logger.warning(
                    "plugin activation scan of %s hit the %d-entry cap; a marker "
                    "beyond it won't activate its plugin (prune the workspace or "
                    "raise _MAX_WALK_ENTRIES)",
                    root,
                    _MAX_WALK_ENTRIES,
                )
                return


#: Upper bound (seconds) on the pre-activation connection PROBE. A bad / unreachable / silently-
#: dropping host can hang a driver's connect on OS defaults (tens of seconds), and no per-driver
#: timeout covers EVERY phase (TCP connect, TLS handshake, auth) — which freezes the daemon's
#: ``connection.add`` the UI is awaiting. This bounds every connector's probe uniformly so the form
#: always gets a timely answer; generous enough for a real cloud warehouse's auth + ``SELECT 1``.
_PROBE_TIMEOUT_SECONDS = 15.0


class ConnectionValidationError(ValueError):
    """A connection failed its pre-activation probe. Subclasses ``ValueError`` so the
    existing ``connection.configure`` RPC error path surfaces it unchanged; the message is
    the REAL underlying failure (auth refused, host unreachable, database missing), shown
    verbatim to the user so they can fix it.

    ``outcome`` and ``reauth`` carry what the probe already KNEW across this
    boundary. Flattening a typed refusal into its message left the prose
    classifier to guess from the words, and a genuine "your sign-in expired"
    guessed as a generic error — so the row said "Error" where it should have
    said "Sign in again", and the action a person needed was not offered.
    """

    def __init__(
        self, message: str, *, outcome: str | None = None, reauth: str | None = None
    ) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.reauth = reauth


class PluginRegistry:
    @property
    def project_directory(self) -> ProjectDirectory:
        """This workspace's ``.alkera/`` handle. A surface that badges a row reads
        connection state through it rather than re-deriving the path."""
        return self._project

    def __init__(self, project: ProjectDirectory, workspace_root: Path) -> None:
        self._project = project
        self._workspace_root = workspace_root
        self._plugins: list[Plugin] = []
        self._registrations: dict[str, RecordingRegistrar] = {}
        # Each provider instance tagged with its owning plugin so a DISABLED plugin's
        # providers (context/lineage/classifier) drop off the surface.
        self._provider_instances: list[tuple[str, object]] = []
        self._active: set[str] = set()
        # DETECTED connection candidates (from discovery) — NOT live until added.
        # Keyed by the FULL connection identity (plugin, handle) — NOT handle alone — so two
        # plugins that derive the same bare handle (e.g. data.sqlite + data.duckdb both → "data")
        # don't overwrite each other in the detected/snapshot surface (the allow-list already
        # keys by (plugin, handle); these must agree). Rows are FILED by their parent too
        # (``_found``), so two orgs' accounts naming one warehouse never overwrite each other.
        self._found: dict[DetectedKey, Connection] = {}
        self._detected: dict[tuple[str, str], Connection] = {}
        # The EXPLICITLY-ADDED allow-list (persisted) — the only connections that
        # go live in the tool registry (detect-then-add).
        self._added = AddedConnectionsStore(project.connections_path)
        # The user's explicit plugin on/off toggle (default-on); a disabled plugin's
        # tools / providers / connections vanish from the agent surface.
        self._enablement = PluginEnablementStore(project.plugins_enabled_path)

    # --- discovery -----------------------------------------------------

    async def discover(self, extra_plugins: Sequence[type[Plugin]] | None = None) -> None:
        """Call ``register()`` on every registered plugin (``CLI_PLUGINS``) and
        on any injected extras."""
        classes: list[type[Plugin]] = [*CLI_PLUGINS.items(), *(extra_plugins or [])]
        for cls in classes:
            plugin = cls()
            name = plugin.manifest.name
            if name in self._registrations:
                raise ValueError(f"duplicate plugin name {name!r}")
            rec = RecordingRegistrar()
            plugin.register(rec)
            for check in PLUGIN_REGISTRATION_CHECKS.items():
                check(name, rec)
            self._plugins.append(plugin)
            self._registrations[name] = rec
            for provider_cls in rec.provider_classes():
                self._provider_instances.append((name, provider_cls()))

    # --- activation ----------------------------------------------------

    async def evaluate_activation(self, ev: WorkspaceEvent) -> list[Plugin]:
        """Enable each plugin whose ``ActivationSpec`` matches ``ev``. Idempotent
        — a plugin already active is skipped. Returns the newly-enabled set."""
        newly: list[Plugin] = []
        ctx = WorkspaceContext(project=self._project, workspace_root=ev.workspace_root)
        for plugin in self._plugins:
            name = plugin.manifest.name
            if name in self._active:
                continue
            spec = plugin.activation()
            if self._matches(spec, ev):
                conns = await self._discover_connections(name, ctx)
            elif spec.connection_signals:
                # Self-activate when a connection is discoverable (e.g. an existing
                # Snowflake CLI login / SNOWFLAKE_* env).
                conns = await self._discover_connections(name, ctx)
                if not conns:
                    continue
            else:
                continue
            await plugin.on_enable(ctx)
            auto = plugin.auto_activates()
            for conn in conns:
                # Always record as a DETECTED candidate the user can add. A remote
                # connection stops there (detect-then-add keeps a human in the loop
                # on what the agent can touch — the prod-by-accident guard). It auto-
                # adds (goes live with no manual step) when EITHER the plugin declares
                # all its connections offline-safe (a local DuckDB file, a dbt
                # project's committed manifest) OR this specific connection is an
                # offline file with no network/cost (a Tableau .twb workbook, a static
                # dags/ folder) — Tableau/Airflow are live-by-default plugins whose
                # FILE connections still ride the carve-out. Idempotent: re-detection
                # never dups (identity is (plugin, handle)).
                self._found[detected_key(conn)] = conn
                self._detected = keyed_detected(self._found.values())
                # ``auto`` applies only to a connection OWNED by this plugin — a
                # cross-plugin candidate a provider surfaces (dbt suggesting the Trino/
                # Snowflake warehouse its profile targets) is metered and stays
                # detect-then-add, never riding the discovering plugin's auto flag.
                if (auto and conn.plugin == name) or conn.is_offline_seed():
                    self._added.add(conn)
            self._active.add(name)
            newly.append(plugin)
        # A persisted (form-)added connection MUST keep its plugin active even with no
        # live detection signal — e.g. a Postgres added via the UI form on a host that
        # exposes no PG* env. Without this the connection sits live-in-the-store but its
        # plugin is inactive (no providers/tools), so the agent can't use it and the UI
        # shows the plugin INACTIVE despite the connection. Re-activate any such plugin.
        # The live team/org Preconfigured lane counts the same way: a box holds no
        # allow-list of its own, and the only connections it ever has are the team's.
        for owner in {c.plugin for c in self.active_connections()}:
            activated = await self._activate_plugin(owner)
            if activated is not None:
                newly.append(activated)
        return newly

    async def _activate_plugin(self, name: str) -> Plugin | None:
        """Run a plugin's ``on_enable`` hook + mark it active (idempotent). Returns the
        plugin only if it was NEWLY activated (already-active / unknown → ``None``). The
        single place outside :meth:`evaluate_activation` that flips ``_active`` — used when
        a connection is added without a detection event (the UI form / a persisted add)."""
        if name in self._active:
            return None
        plugin = next((p for p in self._plugins if p.manifest.name == name), None)
        if plugin is None:
            return None
        await plugin.on_enable(
            WorkspaceContext(project=self._project, workspace_root=self._workspace_root)
        )
        self._active.add(name)
        return plugin

    async def rediscover(self, ev: WorkspaceEvent) -> bool:
        """Re-scan the workspace for connections after a file add/remove — so a newly-dropped
        Tableau workbook / ``.duckdb`` / dbt project surfaces WITHOUT an IDE restart. First
        :meth:`evaluate_activation` activates any now-eligible inactive plugin (+ discovers its
        connections); then we re-run discovery for ALREADY-ACTIVE plugins (which that method
        skips), so a SECOND workbook on an active Tableau is found too. ``_detected`` is refreshed
        and offline-safe connections are auto-added; remote ones stay suggested (detect-then-add).
        Returns True if the detected/added set CHANGED (so the caller can resync jobs + the UI)."""
        before = (frozenset(self._detected), frozenset(self._added.handles()))
        await self.evaluate_activation(ev)  # inactive→active: activate + discover + auto-add
        ctx = WorkspaceContext(project=self._project, workspace_root=ev.workspace_root)
        # Built to the side and published in ONE assignment below: discovery
        # awaits per plugin, and a caller listing connections between two of them
        # would otherwise read a set that is half this walk and half the last one.
        candidates = dict(self._found)
        for plugin in self._plugins:
            name = plugin.manifest.name
            if name not in self._active:
                continue  # inactive plugins were just handled by evaluate_activation
            auto = plugin.auto_activates()
            for conn in await self._discover_connections(name, ctx):
                candidates[detected_key(conn)] = conn
                # ``auto`` applies only to a connection OWNED by this plugin — a
                # cross-plugin candidate a provider surfaces (dbt suggesting the Trino/
                # Snowflake warehouse its profile targets) is metered and stays
                # detect-then-add, never riding the discovering plugin's auto flag.
                if (auto and conn.plugin == name) or conn.is_offline_seed():
                    self._added.add(conn)
        self._found, self._detected = candidates, keyed_detected(candidates.values())
        return (frozenset(self._detected), frozenset(self._added.handles())) != before

    def _matches(self, spec: ActivationSpec, ev: WorkspaceEvent) -> bool:
        if spec.always:
            return True
        patterns = spec.workspace_contains
        if not patterns:
            return False
        # ONE bounded walk, testing every pattern per entry (vs the old
        # tree-walk-per-pattern). Prunes heavy/irrelevant dirs + won't follow
        # symlinks + caps the scan, so activation can't hang on a giant or
        # cyclic tree.
        for rel, name in _iter_workspace_paths(ev.workspace_root):
            for pattern in patterns:
                if fnmatch(rel, pattern) or fnmatch(name, pattern):
                    return True
        return False

    async def _discover_connections(
        self, plugin_name: str, ctx: WorkspaceContext
    ) -> list[Connection]:
        rec = self._registrations[plugin_name]
        out: list[Connection] = []
        for cp_cls in rec.connection_providers:
            provider = cp_cls()
            out.extend(await provider.discover_connections(ctx))
        return out

    # --- typed lookup --------------------------------------------------

    def providers(self, surface: type[P]) -> list[P]:
        """Structural, typed surface lookup — every ENABLED plugin's provider
        instance that implements ``surface`` (a ``@runtime_checkable`` Protocol). A
        disabled plugin's providers are excluded so its surfaces vanish."""
        return [
            p
            for (name, p) in self._provider_instances
            if self.is_enabled(name) and isinstance(p, surface)
        ]

    def plugin_providers(self, plugin_name: str, surface: type[P]) -> list[P]:
        """THIS plugin's provider instances implementing ``surface`` — used to
        resolve, e.g., the Snowflake plugin's ContextProvider for a refresh job.
        Empty when the plugin is disabled (its providers are off the surface)."""
        rec = self._registrations.get(plugin_name)
        if rec is None or not self.is_enabled(plugin_name):
            return []
        return [
            instance for cls in rec.provider_classes() if isinstance((instance := cls()), surface)
        ]

    def detected_connections(self) -> list[Connection]:
        """Discovered candidates — what the user *could* add (not necessarily
        live). The daemon exposes these so the editor can offer + notify."""
        return list(self._detected.values())

    def added_connections(self) -> list[Connection]:
        """The explicitly-added, live connections (the allow-list)."""
        return self._added.load()

    def active_connections(self) -> list[Connection]:
        """The live set — what tools can actually target: the member's own
        allow-list PLUS the live team/org Preconfigured Connections lane."""
        return [*self._added.load(), *self.team_live_connections()]

    def team_connection_records(self) -> list[tuple[TeamConnectionRecord, TeamMemberState]]:
        """Every synced team/org Preconfigured Connection with this member's
        state — re-read from the team store on each call (the cloud-sync lane
        writes it). The liveness / suggestion predicates live in
        ``plugin_base.team_connections``."""
        from alkera_cli.plugins.plugin_base.team_connections import (
            TeamConnectionsStore,
            TeamMemberState,
        )

        state = TeamConnectionsStore(self._project.team_connections_path).load()
        pairs = [
            (record, state.member.get(record_id, TeamMemberState()))
            for record_id, record in state.records.items()
        ]
        pairs.sort(key=lambda p: (p[0].plugin, p[0].handle, p[0].id))
        return pairs

    def team_live_connections(self) -> list[Connection]:
        """The team records that materialize as LIVE connections right now
        (added + enabled + credential-satisfied, per the liveness predicate),
        built as ordinary ``Connection`` objects. A member-own ``(plugin,
        handle)`` identity always wins — the reconciler suffixes collisions,
        and this is the belt to that suspender."""
        from alkera_cli.plugins.plugin_base.team_connections import is_live, to_connection

        own = {(c.plugin, c.handle) for c in self._added.load()}
        live: list[Connection] = []
        for record, member in self.team_connection_records():
            if not is_live(record, member) or not self.is_enabled(record.plugin):
                continue
            conn = to_connection(record, member, plugins_root=self._project.plugins_path)
            # A record this client can't build (an unknown connector, or values
            # that don't make a connection) is not a live connection. Skipping
            # keeps every other team row working; the next sync brings a fix.
            if conn is None or (conn.plugin, conn.handle) in own:
                continue
            restore_team_probe(self._project.plugins_path, record, member, conn)
            live.append(conn)
        return live

    def is_added(self, handle: str) -> bool:
        return handle in self._added.handles()

    def add_connection(self, handle: str, *, plugin: str | None = None) -> Connection | None:
        """Promote a DETECTED candidate to live by persisting it to the allow-
        list. Returns the added connection, or ``None`` if no such candidate.
        Takes effect for tool registries built AFTER this call.

        ``plugin`` disambiguates a cross-plugin handle collision (two plugins both detecting
        ``"data"``): with it, the EXACT ``(plugin, handle)`` candidate is promoted — mirroring
        the plugin-scoped ``remove_connection`` so add/remove resolve the same identity. Without
        it (a legacy caller), match the first detected candidate with the handle (the common
        single-plugin case)."""
        # _detected is keyed by (plugin, handle).
        candidate = next(
            (
                c
                for c in self._detected.values()
                if c.handle == handle and (plugin is None or c.plugin == plugin)
            ),
            None,
        )
        if candidate is None:
            return None
        return self._added.add(candidate)

    def add_all_detected(self) -> list[Connection]:
        """Add every detected candidate (convenience for CLI/tests; the editor
        adds individually after notifying)."""
        return [self._added.add(c) for c in self._detected.values()]

    async def remove_connection(self, handle: str, *, plugin: str | None = None) -> str | None:
        """Remove a connection from the live allow-list (un-add). Returns the OWNING PLUGIN
        name when something was removed (so the caller can cancel exactly that connection's
        refresh job — a bare-handle cancel would kill the survivor plugin's job when two plugins
        share a handle), or ``None`` when nothing matched.

        ``plugin`` disambiguates a cross-plugin handle collision: with it, ONLY the exact
        ``(plugin, handle)`` is removable — a mismatch returns ``None`` rather than falling
        back to another plugin's same-handle row (removal deletes the stored credential, so
        a wrong-row fallback would be destructive). Without it (a legacy caller), the first
        match by handle is removed.

        If the removal leaves its owning plugin with no remaining reason to be active — no
        OTHER added connection AND not a DETECTED candidate (a still-suggested env/file signal)
        — deactivate it, so the active state is correct immediately, not just after the next
        workspace re-open. The exact inverse of the "added ⇒ active" invariant; a detected-but-
        removed connection keeps its plugin active (it stays a suggestion, the redshift/
        databricks 'active' state)."""
        if plugin is not None:
            owner = next(
                (c.plugin for c in self._added.load() if c.plugin == plugin and c.handle == handle),
                None,
            )
            if owner is None:
                return None
        else:
            owner = next((c.plugin for c in self._added.load() if c.handle == handle), None)
        # Remove ONLY the owner's (plugin, handle), not every connection sharing the bare handle —
        # else removing one of two same-handle connections (data.sqlite + data.duckdb) would
        # destroy both (and the other's stored credential).
        removed = self._added.remove(handle, plugin=owner)
        if removed and owner is not None:
            # Delete the removed connection's primary and named credentials with its directory:
            # a successfully added-then-removed connection must not leave plaintext secrets on
            # disk forever (the only prior delete path was the rollback on a FAILED validate).
            # Scoped to the EXACT (owner, handle): a sibling plugin sharing the bare handle keeps
            # its own credentials. Registry-owned files all live below this exact directory.
            conn_dir = self._project.plugins_path / owner / "connections" / handle
            with contextlib.suppress(OSError):
                shutil.rmtree(conn_dir, ignore_errors=True)
            from alkera_cli.plugins.plugin_base.connection_state import forget, local_key

            forget(self._project, local_key(owner, handle))
            still_added = any(c.plugin == owner for c in self._added.load())
            still_detected = any(c.plugin == owner for c in self._detected.values())
            if not (still_added or still_detected):
                await self._deactivate_plugin(owner)
        return owner if removed else None

    async def _deactivate_plugin(self, name: str) -> None:
        """Drop a plugin from the active set + run its ``on_disable`` hook (idempotent) —
        the inverse of :meth:`_activate_plugin`, used when removing a connection leaves a
        plugin with no remaining reason to be active."""
        if name not in self._active:
            return
        self._active.discard(name)
        plugin = next((p for p in self._plugins if p.manifest.name == name), None)
        if plugin is not None:
            await plugin.on_disable()

    def is_active(self, plugin_name: str) -> bool:
        return plugin_name in self._active

    # --- enable / disable (the user's explicit on/off, default-on) -----

    def is_enabled(self, plugin_name: str) -> bool:
        """Whether ``plugin_name`` is enabled (default-on; False only if the user
        explicitly disabled it). A disabled plugin's tools/providers/connections are
        filtered out of the tool registry + surface lookups."""
        return self._enablement.is_enabled(plugin_name)

    def enable_plugin(self, plugin_name: str) -> None:
        """Clear an explicit disable (no-op if already enabled). Takes effect for
        tool registries built AFTER this call."""
        self._enablement.enable(plugin_name)

    async def disable_plugin(self, plugin_name: str) -> None:
        """Disable ``plugin_name``: persist the toggle + run its ``on_disable`` hook.
        Its tools/providers/connections drop off the next-built tool registry."""
        self._enablement.disable(plugin_name)
        plugin = next((p for p in self._plugins if p.manifest.name == plugin_name), None)
        if plugin is not None:
            await plugin.on_disable()

    async def configure_connection(
        self,
        plugin_name: str,
        handle: str,
        auth_method: str,
        fields: dict[str, str],
        *,
        credential_manager: CredentialManager,
    ) -> Connection:
        """Build + validate a connection from the UI form for ``plugin_name``: the
        plugin turns ``(auth_method, fields)`` into the canonical connection-build
        result. The credential manager stores its primary and named secrets separately;
        only their references land on the connection. The built connection is added to
        the live allow-list. Raises
        ``ValueError`` on an unknown plugin or an unsafe handle, or (from the plugin)
        on invalid input."""
        # Validate the handle BEFORE building — it becomes a path component for the
        # credential file, so an absolute/`..` handle could write the secret outside
        # .alkera/. The plugin never sees an unsafe handle.
        _validate_connection_handle(handle)
        plugin = next((p for p in self._plugins if p.manifest.name == plugin_name), None)
        if plugin is None:
            raise ValueError(f"unknown plugin {plugin_name!r}")
        _require_filled(plugin, auth_method, fields)
        build = normalize_connection_build(plugin.build_connection(handle, auth_method, fields))
        conn, snapshots = await self._materialize_build_credentials(
            plugin_name, build, credential_manager
        )
        # Probe the connection BEFORE it goes live — one that can't connect must NOT be added
        # (the user sees the REAL error and the form stays open). Roll back the just-stored
        # secret on failure so a rejected connection leaves nothing behind.
        try:
            await self.validate_connection(conn)
        except BaseException:
            _restore_secret_snapshots(snapshots)
            raise
        added = self._added.add(conn)
        await self.record_probe_success(added)
        # A form-added connection has NO detection event, so (unlike a detected candidate,
        # whose plugin self-activated when its signal matched) its plugin would otherwise
        # stay inactive — live-in-the-store but with no providers/tools, unusable by the
        # agent and shown INACTIVE in the UI. Activate it here so "added ⇒ active" holds.
        await self._activate_plugin(plugin_name)
        return added

    def _added_connection(self, plugin_name: str, handle: str) -> Connection | None:
        """The live (added) connection for the exact ``(plugin, handle)``, or None."""
        return next(
            (c for c in self._added.load() if c.plugin == plugin_name and c.handle == handle),
            None,
        )

    def _record_probe_success(self, conn: Connection) -> None:
        """A passed add/update probe is real I/O evidence, so it clears a stale
        observed failure instead of leaving "Sign in again" up for the rest of
        that verdict's hold window.

        Synchronous on purpose, and therefore never called from the daemon's
        event-loop thread — :meth:`record_probe_success` is the awaitable door
        for a caller that is on it.
        """
        from alkera_cli.plugins.plugin_base.connection_state import record_io_outcome

        record_io_outcome(self._project, conn, None)

    async def record_probe_success(self, conn: Connection) -> None:
        """:meth:`_record_probe_success` off the loop. Writing the record takes a
        cross-process lock and resolves a credential, both of which block; on the
        daemon's loop thread that stalls every other RPC in flight."""
        await asyncio.to_thread(self._record_probe_success, conn)

    def _connection_credential_dir(self, plugin_name: str, handle: str) -> Path:
        """Return a validated private credential directory inside this project."""
        _validate_connection_handle(plugin_name)
        _validate_connection_handle(handle)
        directory = self._project.plugins_path / plugin_name / "connections" / handle
        plugins_root = self._project.plugins_path.resolve()
        if not directory.resolve().is_relative_to(plugins_root):
            raise ValueError(f"refusing to write a credential outside {plugins_root}")
        return directory

    def _named_credential_path(self, plugin_name: str, handle: str, name: str) -> Path:
        """Return the contained file path for one safe credential role name."""
        if not is_safe_handle(name):
            raise ValueError(f"unsafe credential name {name!r}")
        directory = self._connection_credential_dir(plugin_name, handle)
        path = directory / "credentials" / name
        if not path.resolve().is_relative_to(self._project.plugins_path.resolve()):
            raise ValueError("refusing to write a named credential outside the project")
        return path

    @staticmethod
    def _remember_secret(snapshots: dict[Path, _SecretSnapshot], path: Path) -> None:
        """Snapshot ``path`` once even when one operation touches it repeatedly."""
        snapshots.setdefault(path, _snapshot_secret(path))

    async def _materialize_primary_credential(
        self,
        plugin_name: str,
        build: ConnectionBuildResult,
        credential_manager: CredentialManager,
        current: Connection | None,
        snapshots: dict[Path, _SecretSnapshot],
    ) -> Connection:
        """Store or preserve the primary credential and return its reference-only connection."""
        conn = build.connection
        if build.credential is None:
            if current is None:
                return conn
            return conn.model_copy(update={"credential_ref": current.credential_ref})
        path = self._credential_path(plugin_name, conn.handle)
        self._remember_secret(snapshots, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        ref = CredentialRef(scheme="file", locator=str(path))
        await credential_manager.store(ref, build.credential)
        return conn.model_copy(update={"credential_ref": ref})

    def _remove_named_credential(
        self,
        plugin_name: str,
        handle: str,
        ref: CredentialRef,
        snapshots: dict[Path, _SecretSnapshot],
    ) -> None:
        """Delete a registry-owned named secret while retaining rollback bytes."""
        if ref.scheme != "file":
            return
        old_path = Path(ref.locator)
        directory = self._connection_credential_dir(plugin_name, handle)
        if not old_path.resolve().is_relative_to(directory.resolve()):
            return
        self._remember_secret(snapshots, old_path)
        with contextlib.suppress(FileNotFoundError):
            old_path.unlink()

    async def _materialize_named_credentials(
        self,
        plugin_name: str,
        build: ConnectionBuildResult,
        conn: Connection,
        credential_manager: CredentialManager,
        current: Connection | None,
        snapshots: dict[Path, _SecretSnapshot],
    ) -> Connection:
        """Apply explicit named-secret replacements/removals and preserve blank entries."""
        refs = named_credential_refs(current) if current is not None else {}
        requested = build.named_credentials
        if requested is None:
            return with_named_credential_refs(conn, refs)
        for name in refs.keys() - requested.keys():
            self._remove_named_credential(plugin_name, conn.handle, refs.pop(name), snapshots)
        for name, credential in requested.items():
            if credential is None:
                continue
            path = self._named_credential_path(plugin_name, conn.handle, name)
            self._remember_secret(snapshots, path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            ref = CredentialRef(scheme="file", locator=str(path))
            await credential_manager.store(ref, credential)
            refs[name] = ref
        return with_named_credential_refs(conn, refs)

    async def _materialize_build_credentials(
        self,
        plugin_name: str,
        build: ConnectionBuildResult,
        credential_manager: CredentialManager,
        *,
        current: Connection | None = None,
    ) -> tuple[Connection, list[_SecretSnapshot]]:
        """Store a build's secrets and return the reference-only connection plus rollback state."""
        conn = build.connection
        _validate_connection_handle(conn.handle)
        if build.named_credentials is not None:
            for name in build.named_credentials:
                if not is_safe_handle(name):
                    raise ValueError(f"unsafe credential name {name!r}")

        snapshots: dict[Path, _SecretSnapshot] = {}
        try:
            conn = await self._materialize_primary_credential(
                plugin_name, build, credential_manager, current, snapshots
            )
            conn = await self._materialize_named_credentials(
                plugin_name, build, conn, credential_manager, current, snapshots
            )
        except BaseException:
            _restore_secret_snapshots(list(snapshots.values()))
            raise
        return conn, list(snapshots.values())

    def _credential_path(self, plugin_name: str, handle: str) -> Path:
        """The on-disk credential file for a form-configured connection (a plain
        secret file today; the OAuth broker writes its bundle here too)."""
        return self._connection_credential_dir(plugin_name, handle) / "credential"

    def _oauth_bundle_path(self, plugin_name: str, handle: str) -> Path:
        """The per-connection OAuth token bundle (distinct from a plain ``credential``
        secret file so both auth styles can coexist under the same conn dir)."""
        return self._connection_credential_dir(plugin_name, handle) / "oauth.json"

    def _oauth_provider_for(self, plugin: Plugin, auth_method: str) -> str:
        """The OAuth provider name a plugin declared for ``auth_method`` (via the
        ``OAuthSpec`` on that auth method). Raises ``ValueError`` if the method isn't
        an OAuth method — so a caller can't drive the broker for a secret method."""
        schema = plugin.connection_form_schema()
        methods = schema.auth_methods if schema is not None else []
        method = next((m for m in methods if m.name == auth_method), None)
        if method is None or method.oauth is None:
            raise ValueError(f"auth method {auth_method!r} is not an OAuth method")
        return str(method.oauth.provider)

    async def configure_oauth_connection(
        self,
        plugin_name: str,
        handle: str,
        auth_method: str,
        fields: dict[str, str],
        *,
        open_browser: Callable[[str], None],
        app: object | None = None,
        timeout: float = 300.0,  # noqa: ASYNC109 - the browser-authorization deadline, caller-tunable
    ) -> Connection:
        """Add a connection whose auth is user-delegated OAuth: run the browser
        handshake (loopback, PKCE), store the resulting token bundle, then build +
        probe + add the connection exactly like the form path — but the plugin stays
        auth-mechanism-agnostic (it just describes the connection; the broker owns the
        redirect/callback/exchange).

        ``open_browser`` is injected so the flow is testable against a mock provider;
        ``app`` is the org/deploy OAuth-app config (a public PKCE client when omitted —
        e.g. Snowflake ``LOCAL_APPLICATION``). Raises ``ValueError`` for a bad method /
        missing attribute, ``LoopbackError`` / ``OAuthError`` on a failed handshake, or
        ``ConnectionValidationError`` if the authorized connection can't connect."""
        _validate_connection_handle(handle)
        plugin = next((p for p in self._plugins if p.manifest.name == plugin_name), None)
        if plugin is None:
            raise ValueError(f"unknown plugin {plugin_name!r}")
        provider = self._oauth_provider_for(plugin, auth_method)

        # The plugin describes the connection (non-secret attributes) for this OAuth
        # method; it must NOT return a secret (the broker mints one).
        build = normalize_connection_build(plugin.build_connection(handle, auth_method, fields))
        if build.credential is not None or build.named_credentials:
            raise ValueError("an OAuth connection builder cannot return credentials")
        conn = build.connection
        _validate_connection_handle(conn.handle)

        signed_in = await oauth_sign_in().sign_in(
            provider,
            conn.attributes,
            app=app,
            reuse_org_client=False,
            open_browser=open_browser,
            timeout=timeout,
        )

        bundle_path = self._oauth_bundle_path(plugin_name, conn.handle)
        plugins_root = self._project.plugins_path.resolve()
        if not bundle_path.resolve().is_relative_to(plugins_root):
            raise ValueError(f"refusing to write a credential outside {plugins_root}")
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            bundle_path.parent.chmod(0o700)
        signed_in.write(bundle_path)
        ref = CredentialRef(scheme="oauth", locator=str(bundle_path))
        conn = conn.model_copy(update={"credential_ref": ref})

        try:
            await self.validate_connection(conn)
        except BaseException:
            with contextlib.suppress(OSError):
                bundle_path.unlink()
            raise
        added = self._added.add(conn)
        await self.record_probe_success(added)
        await self._activate_plugin(plugin_name)
        return added

    def _oauth_method_for_connection(self, plugin: Plugin, conn: Connection) -> str:
        """The auth method a stored OAuth connection signed in through.

        A connection does not record which form method built it, so the bundle
        does: it stamps the provider that minted it. When it cannot (an older
        bundle, an unreadable file), the plugin's single OAuth method is the only
        answer there is; a plugin offering several is refused rather than guessed
        at, because signing in through the wrong one mints a token for the wrong
        scope.
        """
        schema = plugin.connection_form_schema()
        methods = [m for m in (schema.auth_methods if schema is not None else []) if m.oauth]
        if not methods:
            raise ValueError(f"plugin {plugin.manifest.name!r} has no browser sign-in to redo")
        provider = ""
        ref = conn.credential_ref
        if ref is not None and ref.scheme == "oauth":
            with contextlib.suppress(OSError, ValueError):
                provider = oauth_sign_in().provider_of(Path(ref.locator))
        if provider:
            match = next(
                (m for m in methods if m.oauth is not None and str(m.oauth.provider) == provider),
                None,
            )
            if match is not None:
                return str(match.name)
        if len(methods) > 1:
            raise ValueError(
                f"connection {conn.handle!r} does not say which sign-in it used; "
                "edit it and sign in again from the form"
            )
        return str(methods[0].name)

    def _contained_credential_path(self, locator: str) -> Path:
        """``locator`` as a path this project is allowed to write, or a refusal.

        A credential path that resolves outside the plugins root points the write
        at somewhere an attacker chose; the same check guards the add path."""
        path = Path(locator)
        plugins_root = self._project.plugins_path.resolve()
        if not path.resolve().is_relative_to(plugins_root):
            raise ValueError(f"refusing to write a credential outside {plugins_root}")
        return path

    async def reauthorize_oauth_connection(
        self,
        plugin_name: str,
        handle: str,
        *,
        open_browser: Callable[[str], None],
        app: object | None = None,
        timeout: float = 300.0,  # noqa: ASYNC109 - the browser-authorization deadline, caller-tunable
    ) -> Connection:
        """Sign in again for an already-added OAuth connection, without the form.

        The connection keeps everything it has — its attributes, its handle, its
        place in the allow-list — and only the token bundle is replaced. The new
        bundle is written BESIDE the old one and probed there; only a probe that
        passes moves it into place. A failed re-authorization therefore leaves the
        previous bundle byte-identical, where the add path's rollback would unlink
        it and leave ``credential_ref`` pointing at nothing — a connection that
        was merely stale turned into one that cannot resolve a credential at all.

        Raises ``ValueError`` for an unknown or non-OAuth connection, the broker's
        own errors on a failed handshake, and ``ConnectionValidationError`` when
        the new token cannot connect.
        """
        conn = self._added_connection(plugin_name, handle)
        if conn is None:
            raise ValueError(f"no added connection {handle!r} for plugin {plugin_name!r}")
        ref = conn.credential_ref
        if ref is None or ref.scheme != "oauth":
            raise ValueError(
                f"connection {handle!r} does not sign in through a browser — "
                "edit it to change its credential"
            )
        plugin = next((p for p in self._plugins if p.manifest.name == plugin_name), None)
        if plugin is None:
            raise ValueError(f"unknown plugin {plugin_name!r}")

        bundle_path = self._contained_credential_path(ref.locator)

        auth_method = self._oauth_method_for_connection(plugin, conn)
        # A second sign-in uses the OAuth client the first one registered.
        signed_in = await oauth_sign_in().sign_in(
            self._oauth_provider_for(plugin, auth_method),
            conn.attributes,
            app=app,
            reuse_org_client=True,
            open_browser=open_browser,
            timeout=timeout,
        )

        # Beside the old one, in the same 0600 directory: a temp file elsewhere
        # would not be on the same filesystem, and the replace below has to be
        # atomic for the connection never to be seen without a credential.
        staged = bundle_path.with_name(bundle_path.name + ".new")
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            bundle_path.parent.chmod(0o700)
        signed_in.write(staged)
        probed = conn.model_copy(
            update={"credential_ref": CredentialRef(scheme="oauth", locator=str(staged))}
        )
        try:
            await self.validate_connection(probed)
        except BaseException:
            with contextlib.suppress(OSError):
                staged.unlink()
            raise
        os.replace(staged, bundle_path)
        # The sign-in is real I/O that just succeeded, so it clears whatever the
        # row was saying about the credential.
        await self.record_probe_success(conn)
        return conn

    async def set_connection_enabled(
        self, plugin_name: str, handle: str, enabled: bool
    ) -> Connection | None:
        """Mute / unmute a single live connection WITHOUT removing it — its config and
        stored secret stay on disk; a muted connection just drops off the agent's tool
        surface (the build-time + live filters skip ``enabled == False``). Returns the
        updated connection, or None if no such ``(plugin, handle)`` is added. Takes
        effect for tool registries built after this call."""
        conn = self._added_connection(plugin_name, handle)
        if conn is None:
            # A team/org Preconfigured Connection is muted via its member-local
            # state, which the RUNTIME layer routes (set_connection_enabled →
            # _set_team_connection_muted) because it also invalidates the tool
            # view + resyncs jobs. Here we just report "not a local connection".
            return None
        if conn.enabled == enabled:
            return conn
        updated = conn.model_copy(update={"enabled": enabled})
        return self._added.add(updated)  # add() replaces by (plugin, handle) identity

    async def test_connection(self, plugin_name: str, handle: str) -> None:
        """Re-probe an already-added connection on demand (the "Test connection"
        action). Raises :class:`ConnectionValidationError` carrying the REAL error if
        the connection no longer works (token expired, host moved), or ``ValueError`` if
        no such connection is added. Returns ``None`` on success and does not mutate the
        connections store. A successful team probe may refresh its non-secret cache."""
        conn = self._added_connection(plugin_name, handle)
        if conn is None:
            # A configured team/org Preconfigured Connection is just as
            # testable (muted included — it's still listed): it materializes
            # as an ordinary Connection with a local credential.
            from alkera_cli.plugins.plugin_base.team_connections import (
                build_blocker,
                build_refusal,
                is_configured,
                to_connection,
            )

            # Resolve the ROW first, then build it. Yielding the build straight
            # out of the generator made "this row won't assemble here" arrive as
            # "no such connection", which is a listed row being told it does not
            # exist.
            match = next(
                (
                    (record, member)
                    for record, member in self.team_connection_records()
                    if record.plugin == plugin_name
                    and (member.local_handle or record.handle) == handle
                    and is_configured(record, member)
                ),
                None,
            )
            if match is not None:
                blocker = build_blocker(*match)
                if blocker:
                    raise ValueError(build_refusal(blocker))
                conn = to_connection(*match, plugins_root=self._project.plugins_path)
        if conn is None:
            raise ValueError(f"no added connection {handle!r} for plugin {plugin_name!r}")
        await self.validate_connection(conn)

    async def update_connection(
        self,
        plugin_name: str,
        handle: str,
        auth_method: str,
        fields: dict[str, str],
        *,
        credential_manager: CredentialManager,
    ) -> Connection:
        """Edit an existing added connection in place: rebuild it from the form, probe,
        then atomically replace the stored connection. Blank managed secret fields preserve
        their current independent references; filled fields overwrite only their own secret.
        Probe failure restores every changed secret. ``enabled`` and ``credential_mode``
        carry over from the current connection. Raises
        ``ValueError`` on an unknown plugin/handle or invalid input, and
        ``ConnectionValidationError`` if the edited connection can't connect."""
        _validate_connection_handle(handle)
        plugin = next((p for p in self._plugins if p.manifest.name == plugin_name), None)
        if plugin is None:
            raise ValueError(f"unknown plugin {plugin_name!r}")
        current = self._added_connection(plugin_name, handle)
        if current is None:
            raise ValueError(f"no added connection {handle!r} for plugin {plugin_name!r}")

        _require_filled(plugin, auth_method, fields)
        build = normalize_connection_build(plugin.build_connection(handle, auth_method, fields))
        new_conn = build.connection
        if new_conn.handle != handle:
            raise ValueError("an edit cannot change the connection handle (use rename)")
        # Carry over the runtime axes the form doesn't collect.
        new_conn = new_conn.model_copy(
            update={"enabled": current.enabled, "credential_mode": current.credential_mode}
        )

        build = ConnectionBuildResult(
            connection=new_conn,
            credential=build.credential,
            named_credentials=build.named_credentials,
        )
        new_conn, snapshots = await self._materialize_build_credentials(
            plugin_name, build, credential_manager, current=current
        )

        try:
            await self.validate_connection(new_conn)
        except BaseException:
            _restore_secret_snapshots(snapshots)
            raise
        updated = self._added.add(new_conn)
        await self.record_probe_success(updated)
        return updated

    async def clone_connection(self, plugin_name: str, handle: str, new_handle: str) -> Connection:
        """Duplicate a connection's NON-SECRET config under ``new_handle`` (dev→prod
        variants without retyping). The secret is deliberately NOT copied — the clone
        starts disabled with no ``credential_ref`` so it can't run until the user adds
        its own secret via edit. Raises ``ValueError`` on a bad/duplicate handle or an
        unknown source."""
        _validate_connection_handle(new_handle)
        source = self._added_connection(plugin_name, handle)
        if source is None:
            raise ValueError(f"no added connection {handle!r} for plugin {plugin_name!r}")
        if self._added_connection(plugin_name, new_handle) is not None:
            raise ValueError(f"a connection {new_handle!r} already exists for {plugin_name!r}")
        clone_source = with_named_credential_refs(source, {})
        attributes = dict(clone_source.attributes)
        attributes.pop(PROBE_RESULT_ATTRIBUTE, None)
        clone = clone_source.model_copy(
            update={
                "handle": new_handle,
                "credential_ref": None,
                "enabled": False,  # no secret yet — muted until the user configures it
                "attributes": attributes,
            }
        )
        added = self._added.add(clone)
        await self._activate_plugin(plugin_name)
        return added

    async def rename_connection(self, plugin_name: str, handle: str, new_handle: str) -> Connection:
        """Rename a connection's stable handle, migrating its stored credential dir and
        allow-list entry atomically. URNs/lineage survive (they key on the connection's
        authority, not its handle). Raises ``ValueError`` on a bad/duplicate handle or an
        unknown source. The caller cancels the OLD handle's refresh job (the new handle
        reschedules on the next tick), mirroring ``remove_connection``."""
        _validate_connection_handle(new_handle)
        source = self._added_connection(plugin_name, handle)
        if source is None:
            raise ValueError(f"no added connection {handle!r} for plugin {plugin_name!r}")
        if new_handle == handle:
            return source
        if self._added_connection(plugin_name, new_handle) is not None:
            raise ValueError(f"a connection {new_handle!r} already exists for {plugin_name!r}")

        # Move the credential DIR (if any) to the new handle's path, and re-point a
        # file/oauth credential_ref that lived inside it. A driver-native ref (env /
        # snowflake_toml / gcloud_adc) has no dir to move and is carried as-is.
        old_dir = self._project.plugins_path / plugin_name / "connections" / handle
        new_dir = self._project.plugins_path / plugin_name / "connections" / new_handle
        new_ref = source.credential_ref
        if old_dir.is_dir():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_dir, new_dir)
            ref = source.credential_ref
            if ref is not None and ref.scheme in {"file", "oauth"} and str(old_dir) in ref.locator:
                new_ref = ref.model_copy(
                    update={"locator": ref.locator.replace(str(old_dir), str(new_dir), 1)}
                )

        new_named_refs = named_credential_refs(source)
        for name, ref in new_named_refs.items():
            locator = Path(ref.locator)
            if ref.scheme not in {"file", "oauth"} or not locator.is_relative_to(old_dir):
                continue
            new_named_refs[name] = ref.model_copy(
                update={"locator": str(new_dir / locator.relative_to(old_dir))}
            )
        renamed = with_named_credential_refs(
            source.model_copy(update={"handle": new_handle, "credential_ref": new_ref}),
            new_named_refs,
        )
        # Add the new identity first, then drop the old — so a crash between the two
        # leaves BOTH (recoverable), never neither (data loss).
        self._added.add(renamed)
        self._added.remove(handle, plugin=plugin_name)
        # The standing state moves with the connection: nothing happened to the
        # credential or the warehouse, so a renamed row keeps the verdict it
        # earned instead of reading unchecked.
        from alkera_cli.plugins.plugin_base.connection_state import local_key, rename

        rename(
            self._project,
            local_key(plugin_name, handle),
            local_key(plugin_name, new_handle),
        )
        return renamed

    def plugins(self) -> list[Plugin]:
        """The discovered plugins (read-only) — for the ``plugin.*`` daemon RPC's
        list/status. Active state is ``is_active(name)``."""
        return list(self._plugins)

    def plugin_snapshot(self) -> list[PluginInfo]:
        """Every discovered plugin + its connections, for the agent's ``list_plugins``
        tool: active/inactive, description, URN format, and which connections are live
        (added) vs merely detected. Lets the agent tell the user what Alkera supports
        and what's left to configure."""
        # Key by the FULL (plugin, handle) identity so two plugins sharing a bare handle don't
        # collapse — each plugin's connection stays visible under its own plugin.
        # "Added" is the live set — the member's own allow-list AND the team/org
        # Preconfigured lane: the row a box leased from the team is the only kind of
        # connection it has, and a listing that counted only the allow-list told the
        # agent its warehouse was "not active, nothing added" while the box was
        # carding that very warehouse. A plugin with a live connection of either
        # lane is on the tool surface, so it reads as active whichever way it got
        # there.
        active = self.active_connections()
        added_pairs = {(c.plugin, c.handle) for c in active}
        reachable = {c.plugin for c in active}
        by_id: dict[tuple[str, str], Connection] = {
            (c.plugin, c.handle): c for c in self.detected_connections()
        }
        for conn in active:
            by_id.setdefault((conn.plugin, conn.handle), conn)
        out: list[PluginInfo] = []
        for plugin in self._plugins:
            name = plugin.manifest.name
            conns = [
                connection_info(c, added=(c.plugin, c.handle) in added_pairs)
                for c in by_id.values()
                if c.plugin == name
            ]
            out.append(
                PluginInfo(
                    name=name,
                    description=plugin.manifest.description,
                    active=name in self._active or name in reachable,
                    enabled=self.is_enabled(name),
                    surfaces=sorted(s.value for s in plugin.manifest.surfaces),
                    urn_format=plugin.urn_help(),
                    connections=conns,
                )
            )
        return out

    def detected_for_plugin(self, plugin_name: str) -> list[Connection]:
        """The detected (candidate) connections this plugin contributed."""
        return [c for c in self._detected.values() if c.plugin == plugin_name]

    def agent_definitions(self) -> list[AgentDefinition]:
        out: list[AgentDefinition] = []
        for rec in self._registrations.values():
            out.extend(rec.agent_defs)
        return out

    def skill_definitions(self) -> list[SkillDef]:
        out: list[SkillDef] = []
        for rec in self._registrations.values():
            out.extend(rec.skill_defs)
        return out

    def tool_classes(self) -> list[type[Tool[Any, Any]]]:
        """Plugin-contributed tool classes from ENABLED plugins that have a live
        connection to target. A disabled plugin's tools vanish from the agent's
        surface; a plugin with no added/team connection has nothing its tools
        could run against, and advertising them anyway buries the reachable tool
        under unreachable ones in ``search_tools`` ranking. A muted connection
        doesn't count, so visibility matches what the call-time resolver would
        actually hand the tool."""
        active = [connection for connection in self.active_connections() if connection.enabled]
        reachable = {connection.plugin for connection in active}
        out: list[type[Tool[Any, Any]]] = []
        for name, rec in self._registrations.items():
            if self.is_enabled(name) and name in reachable:
                out.extend(rec.tools)
                for connection in active:
                    if connection.plugin != name:
                        continue
                    for provider_class in rec.tool_providers:
                        out.extend(provider_class().tools(connection))
        return list(dict.fromkeys(out))

    def tool_registry(
        self,
        *,
        lineage_jobs_snapshot: Callable[[], Sequence[Any]] | None = None,
        web_search_enabled: bool = False,
        web_fetch_enabled: bool | None = None,
        subagents_enabled: bool = True,
    ) -> ToolRegistry:
        """Build a ``ToolRegistry`` for the active project: the platform's own
        tools, every EXPLICITLY-ADDED connection with its resolved
        ``CapabilitySet``, the tools and stores of every registered tool source
        (``AGENT_TOOLS``), and any plugin-provided tools. Detected-but-
        not-added connections are deliberately absent — the agent can only reach
        what the user added.

        ``lineage_jobs_snapshot`` (when given) is the live scheduler job reader the
        lineage tools use to wait out an in-flight refresh; the runtime wires it to its
        memoized ``scheduler().list_jobs``. ``None`` (the default for non-runtime callers
        and tests) makes that wait a strict no-op.

        ``web_search_enabled`` gates the local web tools (`web.search`/`web.fetch`) —
        the org-level toggle the runtime resolves from the gateway. Default False:
        fail-closed for callers with no gateway (tests, offline commands).
        ``web_fetch_enabled`` subtracts `web.fetch` alone, for a deployment that
        keeps the key-less metasearch but refuses the tool that pulls an
        arbitrary URL; ``None`` means "follow ``web_search_enabled``".

        ``subagents_enabled`` (the ``ALKERA_SUBAGENTS_ENABLED`` setting) gates the
        agent-spawning tools. False keeps them out of the catalog entirely, so no
        backend advertises them — a permission "deny" would not: the opencode
        backend still offers a denied tool to the model."""
        # Late imports — the built-in tools import ``tool``/``capabilities``;
        # importing them here keeps registry load-order simple.
        from alkera_cli.plugins.plugin_base.agent_tools import (
            AGENT_TOOLS,
            ToolBuild,
            combined_services,
        )
        from alkera_cli.plugins.plugin_base.agents import AGENTS_SUBDIR, resolve_agents
        from alkera_cli.plugins.plugin_base.background_tools import register_background_tools
        from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
        from alkera_cli.plugins.plugin_base.blob_inspect_tools import register_blob_inspect_tools
        from alkera_cli.plugins.plugin_base.blob_tool import register_blob_tools
        from alkera_cli.plugins.plugin_base.blob_write_tools import register_blob_write_tools
        from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
        from alkera_cli.plugins.plugin_base.permissions import DecisionSink
        from alkera_cli.plugins.plugin_base.skill_tool import register_skill_tools
        from alkera_cli.plugins.plugin_base.subagent_tool import register_subagent_tools
        from alkera_cli.plugins.plugin_base.task_tools import register_task_tools
        from alkera_cli.plugins.plugin_base.web_tools import register_web_tools

        def _live_added_connections() -> list[Connection]:
            # The ENABLED added connections, re-read from disk on each call (mirrors the
            # build-time loop's filter below). LIVE so a connection added after this
            # registry was built — mid-session, or by another process via the shared
            # connections.json — is reachable without rebuilding the session. Both the
            # plugin AND the individual connection must be enabled (a muted connection
            # keeps its config + secret on disk but drops off the agent's surface).
            # The team/org Preconfigured lane rides the same liveness: a reconcile or
            # member-accept that just landed is queryable without a rebuild.
            own = [c for c in self._added.load() if self.is_enabled(c.plugin) and c.enabled]
            return [*own, *self.team_live_connections()]

        def _live_agents() -> list[AgentDefinition]:
            return list(
                resolve_agents(
                    agents_dir=self._project.path / AGENTS_SUBDIR,
                    programmatic=self.agent_definitions(),
                ).values()
            )

        build_set = [
            *(c for c in self._added.load() if self.is_enabled(c.plugin) and c.enabled),
            # The live team/org Preconfigured lane registers exactly like the
            # member's own connections (already plugin-enablement-filtered and
            # deduped against member-own identity).
            *self.team_live_connections(),
        ]
        # A disabled plugin's connections — and an individually muted connection —
        # drop off the surface too (config + secret stay on disk).
        resolved = [(conn, self._capabilities_for_connection(conn)) for conn in build_set]
        build = ToolBuild(
            project=self._project,
            plugins=self,
            capabilities=tuple(caps for _conn, caps in resolved),
        )
        sources = AGENT_TOOLS.items()
        services = combined_services(sources, build)

        registry = ToolRegistry(
            self._project.blobs(),
            context_store=services.context_store,
            lineage_store=services.lineage_store,
            cost_ledger=services.cost_ledger,
            # Project-level fallback log; an in-chat dispatch overrides it with the
            # chat's own per-chat sink (via the session binding).
            decision_sink=DecisionSink(self._project.path),
            # LIVE providers (bound methods / closures): re-read on each access so a
            # connection / plugin / agent added AFTER this registry was built shows up
            # in list_plugins / list_skills / list_agent_types and is queryable, without
            # rebuilding the session (the registry is captured once per chat at start).
            skills=self.skill_definitions,
            # With no embedder, tool search ranks by keyword alone.
            embedder=services.embedder,
            agents=_live_agents,
            plugin_snapshot=self.plugin_snapshot,
            # The live scheduler job reader (or None) so the lineage tools can wait out
            # an in-flight refresh before querying — wired by the runtime build site.
            lineage_jobs_snapshot=lineage_jobs_snapshot,
            # Live connection resolution: connection_for / capabilities_for fall back to
            # these for a handle added after build (resolving ONLY from the added
            # allow-list — a detected-but-not-added connection stays unreachable).
            connections_provider=_live_added_connections,
            capabilities_resolver=self._capabilities_for_connection,
            knowledge_reader_resolver=services.knowledge_reader,
            subagents_enabled=subagents_enabled,
        )
        for conn, caps in resolved:
            registry.register_connection(conn, capabilities=caps)
        register_meta_tools(registry)
        register_blob_tools(registry)
        register_blob_inspect_tools(registry)
        register_blob_write_tools(registry)
        register_subagent_tools(registry)
        register_task_tools(registry)
        register_background_tools(registry)
        # The parent-hosted bash tool is POSIX-only (process-group lifecycle). On
        # Windows we register nothing here AND leave the vendor's native bash enabled
        # (the adapter disables are likewise POSIX-gated), so a Windows agent always
        # has a shell — until the parent-hosted port is cross-platform.
        if os.name == "posix":
            register_bash_tools(registry)
        register_skill_tools(registry)
        # The local web tools are an ORG-level toggle (default on for SaaS, off
        # for self-hosted) resolved by the runtime from the gateway — not a
        # capability of any connection, so gated here rather than via build_set.
        if web_search_enabled:
            fetch_ok = web_search_enabled if web_fetch_enabled is None else web_fetch_enabled
            register_web_tools(registry, fetch_enabled=fetch_ok)
        # Every other tool family comes from a registered source (AGENT_TOOLS).
        for source in sources:
            source.register(registry, build)

        for tool_cls in self.tool_classes():
            registry.register(tool_cls)
        return registry

    def capabilities_for_connection(self, conn: Connection) -> CapabilitySet | None:
        """Public: the live CapabilitySet for a connection (refresh runners use it
        to set the lineage certainty ceiling)."""
        return self._capabilities_for_connection(conn)

    def _capabilities_for_connection(self, conn: Connection) -> CapabilitySet | None:
        """Resolve a connection's live ``CapabilitySet`` from its plugin's
        ``CapabilityProvider`` (if it declared one)."""
        rec = self._registrations.get(conn.plugin)
        if rec is None or not rec.capability_providers:
            return None
        provider = rec.capability_providers[0]()
        return provider.capabilities(conn)

    def _probe_for_connection(self, conn: Connection) -> ProbeProvider | None:
        """Instantiate the optional authenticated probe registered by ``conn``'s plugin."""
        rec = self._registrations.get(conn.plugin)
        if rec is None or not rec.probe_providers:
            return None
        return rec.probe_providers[0]()

    async def validate_connection(self, conn: Connection) -> None:
        """Probe that ``conn`` actually works BEFORE it is added/activated.

        A registered probe runs first and caches its non-secret evidence on the connection
        before capabilities are assembled. Legacy plugins keep the RunSQL ``SELECT 1`` probe,
        falling back to IntrospectSchema. A connector with neither surface remains a no-op.

        Raises :class:`ConnectionValidationError` carrying the REAL underlying error (auth
        refused, host unreachable, database missing) so callers surface it verbatim. An empty
        result is NOT a failure — only a raised error is (a valid instance may have 0 relations).
        """
        from alkera_cli.plugins.plugin_base.capabilities import (
            IntrospectSchemaCapability,
            RunSQLCapability,
        )

        probe = self._probe_for_connection(conn)
        try:
            async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
                if probe is not None:
                    result = await probe.probe(conn)
                    conn.attributes[PROBE_RESULT_ATTRIBUTE] = probe_result_payload(result)
                    records = self.team_connection_records()
                    persist_team_probe(self._project.plugins_path, records, conn, result)
                    self._capabilities_for_connection(conn)
                    return
                caps = self._capabilities_for_connection(conn)
                if caps is None:
                    return
                # type-abstract: the capability ABCs are lookup KEYS here, never instantiated.
                runsql = caps.get(RunSQLCapability)  # type: ignore[type-abstract]
                introspect = caps.get(IntrospectSchemaCapability)  # type: ignore[type-abstract]
                if runsql is not None:
                    await runsql.run("SELECT 1", effect=Effect.READ, cap_token=None, limit=1)
                elif introspect is not None:
                    # A metadata read against the live remote — a bad URL/token raises here.
                    await introspect.list_relations()
                # else: no live surface to probe (a pure-file connector) — nothing to validate.
        except TimeoutError as exc:
            # The probe outran its bound — an unreachable / silently-dropping host. Surface a clear
            # failure instead of letting the daemon's connection.add hang while the UI waits.
            raise ConnectionValidationError(
                f"connection probe timed out after {_PROBE_TIMEOUT_SECONDS:g}s — the host is "
                "unreachable or too slow to respond",
                outcome=Outcome.timeout.value,
            ) from exc
        except ConnectionValidationError:
            raise
        except Exception as exc:  # the connector's REAL failure — surface it verbatim
            outcome, _detail, reauth = outcome_of(exc)
            raise ConnectionValidationError(
                str(exc) or repr(exc), outcome=outcome, reauth=reauth or None
            ) from exc


__all__ = ["ConnectionValidationError", "PluginRegistry"]
