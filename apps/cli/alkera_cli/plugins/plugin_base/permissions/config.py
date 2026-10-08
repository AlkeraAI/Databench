"""``.alkera/permissions.yml`` — the project's permission policy.

The project store's first config file. A ``VersionedModel`` carrying the default
mode and the rule list (deny→ask→allow precedence, evaluated collect-all). Both
tier membership and waivability are user-configurable here — the irreducible
floor (`policy.is_floor`) is the only thing this file can't shrink.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from alkera_core.atomic_io import write_text_atomic
from alkera_core.project.locking import FileLock, retrying_lock
from alkera_core.versioning import Migration, VersionedModel
from pydantic import BaseModel, Field, PrivateAttr, ValidationError

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.plugins.plugin_base.permissions.consent_scope import normalize_command
from alkera_cli.plugins.plugin_base.permissions.policy import AutoDecision, grant_reaches

RuleDecision = Literal["allow", "ask", "deny"]

_DECISION_TO_AUTO: dict[RuleDecision, AutoDecision] = {
    "allow": AutoDecision.ALLOW,
    "ask": AutoDecision.PROMPT,
    "deny": AutoDecision.REJECT,
}

PERMISSIONS_FILE = "permissions.yml"
#: Gitignored personal overlay — a developer's own rules on top of the committed
#: project policy (settings hierarchy: project < local).
PERMISSIONS_LOCAL_FILE = "permissions.local.yml"

#: Where a loaded rule came from. ``project`` is the committed ``permissions.yml``;
#: ``local`` is the gitignored overlay, where a chat records a person's card answers.
RuleOrigin = Literal["project", "local"]


class _MatchSpec(BaseModel):
    """The matching predicate shared by a decision rule and an effect-reclassify
    rule. Omitted fields are wildcards; ``match`` is a glob over the raw
    command/SQL (Claude-Code-style)."""

    capability: str | None = None
    effect: Effect | None = None
    operation: str | None = None
    match: str | None = None

    def matches(self, descriptor: ActionDescriptor) -> bool:
        if self.capability is not None and self.capability != descriptor.capability:
            return False
        if self.effect is not None and self.effect != descriptor.effect:
            return False
        if self.operation is not None and self.operation != descriptor.operation:
            return False
        if self.match is None:
            return True
        raw = descriptor.raw or ""
        # Two spellings that differ only in the spaces between words are one
        # command, so a rule recorded for one answers for the other.
        return fnmatch(raw, self.match) or fnmatch(normalize_command(raw), self.match)


class PermissionRule(_MatchSpec):
    """A policy rule: a match predicate → an allow/ask/deny decision."""

    decision: RuleDecision = "ask"
    reason: str | None = None
    mode: str | None = None
    """The stance a person answered "Always allow" under, on a rule a chat
    RECORDED (:func:`add_local_rule` writes it). ``None`` on a hand-authored rule
    in ``permissions.yml``, which carries no stance and binds wherever a rule
    binds; a stance-less rule in the local overlay predates the stamp and the
    loader reads it as :data:`_LEGACY_LOCAL_STANCE`."""
    owner: str | None = None
    """The person whose answer a rule a chat RECORDED on a shared host is (the
    chat owner's user id). On a shared host a rule naming an owner decides that
    owner's chats and nobody else's; ``None`` is a rule nobody's chat recorded
    as their own, which binds there only from the committed ``permissions.yml``.
    On a local machine both files are the one owner's and every rule binds."""

    _origin: RuleOrigin = PrivateAttr(default="project")
    """Which policy file the rule was loaded from. Never persisted: the loader
    stamps it, so a rule cannot claim an origin by spelling one."""

    @property
    def origin(self) -> RuleOrigin:
        return self._origin

    def grants_exec(self, descriptor: ActionDescriptor, *, mode: str, shared_host: bool) -> bool:
        """Whether this rule grants an EXEC action under ``bypass``.

        Only an ``allow`` that NAMES ``effect: exec`` does, matched against the real
        classification: a wildcard, a capability- or operation-only rule, or a rule
        on a tier a reclassify lowered the action to was written for something
        else. On a shared host only the box owner's policy grants it — the
        committed ``permissions.yml``, which sits inside the fence's always-refused
        ``.alkera`` so no member's agent can write it. The ``permissions.local.yml``
        overlay records one member's answers there, so it never grants EXEC on a
        shared host; on a local machine both files are the one owner's."""
        return (
            self.decision == "allow"
            and self.effect == Effect.EXEC
            and descriptor.effect == Effect.EXEC
            and self.matches(descriptor)
            and self.binds_in(mode)
            and (not shared_host or self._origin == "project")
        )

    def matches(self, descriptor: ActionDescriptor) -> bool:
        if not super().matches(descriptor):
            return False
        # A compound command is several actions with no one family, so a
        # standing allow that names a family covers it only when it also pins
        # the exact text. Tightening — a deny, an ask — binds as widely as it
        # is written.
        if self.decision == "allow" and self.match is None and self.operation is not None:
            return descriptor.scope != "command"
        return True

    def binds_for(self, *, shared_host: bool, owner: str) -> bool:
        """Whether this rule may decide an ask in a chat owned by ``owner``.

        A local machine has one owner, so every rule binds. A shared host serves
        many people from one pair of policy files: a rule recorded as someone's
        own binds only their chats, and a rule that names nobody binds only when
        it is the box owner's committed policy, never an answer some chat left in
        the overlay before answers were recorded per person."""
        if not shared_host:
            return True
        if self.owner is not None:
            return bool(owner) and self.owner == owner
        return self._origin == "project"

    def binds_in(self, mode: str) -> bool:
        """Whether this rule may decide an ask under the stance in force.

        Tightening is never scoped — a deny or an ask binds everywhere, in every
        stance — so only a standing ALLOW is held to the stance it was granted
        under: it binds that stance and every looser one, and a stricter stance
        asks again rather than replaying an answer given where less was asked."""
        return self.decision != "allow" or grant_reaches(granted_in=self.mode, mode=mode)


class EffectRule(_MatchSpec):
    """Reclassify a matched action's EFFECT tier.

    Lets a project mark a known-safe operation DOWN a tier (so it auto-allows
    instead of prompting) or a sensitive one UP (so it always prompts). It changes
    only the policy DECISION, not what the connector physically enforces — the
    cap-token gate still binds to the REAL classification (the connector re-checks
    a valid token for the action's actual effect). And reclassification can never
    pull a FLOOR action (destroy/egress + drop/truncate/grant/revoke) below its
    prompt: the floor is evaluated on the stricter of the original and reclassified
    effect, so reclassification is tighten-freely / loosen-only-off-the-floor."""

    to_effect: Effect
    reason: str | None = None


def _strip_environment_scoping(data: dict[str, Any], name: str) -> list[str]:
    """Remove the retired environment scoping from a raw policy mapping IN
    PLACE, tighten-only. Returns the loud warnings to surface.

    An ``environment:``-scoped ALLOW rule is DROPPED (stripping the key would
    silently widen it to every connection); a scoped deny/ask has the key
    stripped and now binds everywhere (widening a restriction is safe); a
    scoped reclassify is DROPPED (the action falls back to its real
    classification, which only tightens); the already-dead ``connections:``
    block is removed silently."""
    warnings: list[str] = []
    rules = data.get("rules")
    if isinstance(rules, list):
        kept_rules: list[Any] = []
        for rule in rules:
            if isinstance(rule, dict) and rule.get("environment") is not None:
                env = rule.pop("environment")
                if rule.get("decision", "ask") == "allow":
                    warnings.append(
                        f"{name}: dropped an allow rule scoped to environment {env!r} — "
                        "environment scoping was removed; re-add the rule without "
                        "'environment:' to allow it on every connection."
                    )
                    continue
                warnings.append(
                    f"{name}: a rule was scoped to environment {env!r}; environment "
                    "scoping was removed, so it now applies to every connection."
                )
            kept_rules.append(rule)
        data["rules"] = kept_rules
    reclassify = data.get("reclassify")
    if isinstance(reclassify, list):
        kept_reclassify: list[Any] = []
        for rule in reclassify:
            if isinstance(rule, dict) and rule.get("environment") is not None:
                warnings.append(
                    f"{name}: dropped an effect-reclassify rule scoped to environment "
                    f"{rule.get('environment')!r} — environment scoping was removed; "
                    "the matched actions keep their real classification."
                )
                continue
            kept_reclassify.append(rule)
        data["reclassify"] = kept_reclassify
    data.pop("connections", None)
    return warnings


def _migrate_permissions_v1_0_0_to_v1_1_0(data: dict[str, Any]) -> dict[str, Any]:
    """1.1.0 added ``reclassify`` (additive, defaulted) — advance the stamp."""
    data["schema_version"] = "1.1.0"
    return data


def _migrate_permissions_v1_1_0_to_v2_0_0(data: dict[str, Any]) -> dict[str, Any]:
    """2.0.0 removed environment scoping (the ``environment`` match key and the
    per-connection ``connections`` block). Same tighten-only transform the
    loader applies to unstamped hand-authored files; a migration has nowhere to
    surface warnings, so they are dropped here."""
    _strip_environment_scoping(data, PERMISSIONS_FILE)
    data["schema_version"] = "2.0.0"
    return data


def _migrate_permissions_v2_0_0_to_v2_1_0(data: dict[str, Any]) -> dict[str, Any]:
    """2.1.0 added ``mode`` on a rule — the stance a recorded standing grant was
    given under (additive, defaulted). A 2.0.0 document's rules carry no stance,
    which is what ``None`` means. Advance the stamp, and nothing else: WHERE a
    stance-less rule binds depends on which file it came from, which a pure
    dict-in/dict-out migration cannot see — the loader answers it
    (:func:`_as_recorded_grants`)."""
    data["schema_version"] = "2.1.0"
    return data


def _migrate_permissions_v2_1_0_to_v2_2_0(data: dict[str, Any]) -> dict[str, Any]:
    """2.2.0 admits ``exec`` as a rule's ``effect`` and a reclassify's
    ``to_effect`` (additive: a 2.1.0 document cannot carry it). Advance the stamp."""
    data["schema_version"] = "2.2.0"
    return data


def _migrate_permissions_v2_2_0_to_v2_3_0(data: dict[str, Any]) -> dict[str, Any]:
    """2.3.0 added ``owner`` on a rule: whose answer a rule recorded on a shared
    host is (additive, defaulted). A 2.2.0 rule names nobody, which is what
    ``None`` means. Advance the stamp."""
    data["schema_version"] = "2.3.0"
    return data


class PermissionsConfig(VersionedModel):
    # 1.1.0: + reclassify (effect-tier reclassification) — additive/optional.
    # 2.0.0: removed environment scoping (the `environment` match key on rules and the
    # per-connection `connections` block) — connections no longer carry a
    # policy-relevant environment.
    # 2.1.0: + rule `mode` — the stance a recorded "Always allow" was granted under,
    # so a stricter stance re-asks instead of replaying it. Additive/optional.
    # 2.2.0: the effect vocabulary gained ``exec`` (a program or a file on the data
    # server), which a rule may name and only a rule naming it grants. Additive.
    # 2.3.0: + rule `owner`, the person a rule recorded on a shared host answers for,
    # so one person's standing answer never decides another's ask. Additive/optional.
    SCHEMA_VERSION = "2.3.0"
    MIGRATIONS: ClassVar[dict[str, Migration]] = {
        "1.0.0": _migrate_permissions_v1_0_0_to_v1_1_0,
        "1.1.0": _migrate_permissions_v1_1_0_to_v2_0_0,
        "2.0.0": _migrate_permissions_v2_0_0_to_v2_1_0,
        "2.1.0": _migrate_permissions_v2_1_0_to_v2_2_0,
        "2.2.0": _migrate_permissions_v2_2_0_to_v2_3_0,
    }

    default_mode: str = "default"
    rules: list[PermissionRule] = Field(default_factory=list)
    reclassify: list[EffectRule] = Field(default_factory=list)
    """Effect-tier overrides; first match wins."""
    cost: dict[str, Any] = Field(default_factory=dict)
    """Per-query/chat/day/week caps."""

    _load_warnings: list[str] = PrivateAttr(default_factory=list)
    #: Where this policy was loaded from and the policy files' change key at the
    #: time, so :meth:`current` can tell whether it is still what the files say.
    #: ``None`` for a policy built in memory. Not persisted (PrivateAttr).
    _source: tuple[Path, tuple[tuple[int, int, int], tuple[int, int, int]]] | None = PrivateAttr(
        default=None
    )
    """Loud, human-facing warnings from a malformed file the loader repaired or
    fell back on. Surfaced at chat-open so a typo never *silently*
    drops a stricter policy. Empty on a clean load. Not persisted (PrivateAttr)."""

    @property
    def load_warnings(self) -> list[str]:
        return list(self._load_warnings)

    def rule_decision(self, descriptor: ActionDescriptor, *, mode: str) -> AutoDecision | None:
        """Collect-all precedence: any matching deny → reject; else any ask →
        prompt; else any allow → allow; else None (fall back to the mode).

        ``mode`` is the stance in force. A rule that does not reach it
        (:meth:`PermissionRule.binds_in` — a standing allow granted under a looser
        stance) is not consulted at all, so the mode decides as if it were never
        recorded, which is a prompt in ``default``."""
        decisions = [
            _DECISION_TO_AUTO[r.decision]
            for r in self.rules
            if r.matches(descriptor) and r.binds_in(mode)
        ]
        if not decisions:
            return None
        return max(decisions)  # REJECT > PROMPT > ALLOW

    def current(self) -> PermissionsConfig:
        """This policy as the files now say it: the same object while neither
        policy file has changed since it was loaded, a fresh load once one has.
        A session loads its policy once, and an answer a person records during
        it ("Always allow") lands in the local file; without this, the next
        decision in the same session would never see it. A policy that was not
        loaded from files is itself."""
        if self._source is None:
            return self
        alkera_dir, key = self._source
        if _policy_files_key(alkera_dir) == key:
            return self
        return load_permissions_cached(alkera_dir)

    def exact_text_decision(
        self, descriptor: ActionDescriptor, *, mode: str
    ) -> AutoDecision | None:
        """:meth:`rule_decision` over the rules that pin a command's exact text
        (``match``) alone: the answer a person gave for THIS text, which is the
        only standing answer that reaches a command whose reach the fence could
        not prove."""
        decisions = [
            _DECISION_TO_AUTO[r.decision]
            for r in self.rules
            if r.match is not None and r.matches(descriptor) and r.binds_in(mode)
        ]
        return max(decisions) if decisions else None

    def exact_allow(self, descriptor: ActionDescriptor, *, mode: str) -> bool:
        """Whether a standing allow pins THIS command's exact text (a ``match``
        with no wildcard in it) and binds the stance in force. That is the one
        standing answer that reaches a destructive command: a person consented
        to this line, not to its family (:mod:`.consent_scope`)."""
        return any(
            r.decision == "allow"
            and r.match is not None
            and pins_one_text(r.match)
            and r.matches(descriptor)
            and r.binds_in(mode)
            for r in self.rules
        )

    def for_host(self, *, shared_host: bool, owner: str = "") -> PermissionsConfig:
        """The policy a decision in ``owner``'s chat on this host may consult.

        On a local machine both policy files are the one owner's, so this is the
        policy itself. A shared host (a cloud box, which serves every chat placed
        on it, of every org) has one ``permissions.local.yml`` for all of them, so
        each recorded answer names the person who gave it, and only that
        person's answers decide their chat (:meth:`PermissionRule.binds_for`):
        the committed ``permissions.yml`` binds everyone, another person's
        "Always allow" or "Always reject" binds nobody here, and neither does an
        overlay rule that names no one. A chat whose owner the box was not told
        (``owner`` empty) consults the committed policy alone. The result is
        detached from the files: asking it for :meth:`current` returns it, never
        a fresh load that would bring every answer back."""
        if not shared_host:
            return self
        owned = self.model_copy(
            update={"rules": [r for r in self.rules if r.binds_for(shared_host=True, owner=owner)]}
        )
        owned._source = None
        return owned

    def grants_exec(self, descriptor: ActionDescriptor, *, mode: str, shared_host: bool) -> bool:
        """Whether some rule grants this EXEC action under ``bypass``
        (:meth:`PermissionRule.grants_exec`). A deny or an ask is not consulted
        here; the caller still takes the strictest matching rule first."""
        return any(
            r.grants_exec(descriptor, mode=mode, shared_host=shared_host) for r in self.rules
        )

    def reclassified_effect(self, descriptor: ActionDescriptor) -> Effect | None:
        """The effect tier the FIRST matching reclassify rule assigns this action,
        or ``None`` when none matches. The gate uses this for the policy
        decision only; the floor + connector still bind the real classification."""
        for rule in self.reclassify:
            if rule.matches(descriptor):
                return rule.to_effect
        return None


def _read_raw(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read + YAML-parse a policy file. Returns ``(mapping_or_None, error)``.

    A missing file → ``(None, None)``. A syntax error / non-mapping / unreadable
    file → ``(None, "<loud reason>")`` so the caller can fall back to defaults
    WITHOUT raising — a typo must never block even pure reads."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not read {path.name}: {exc}"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"{path.name} is not valid YAML ({exc}); using built-in defaults"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"{path.name} must be a YAML mapping; using built-in defaults"
    return data, None


def _validate_or_salvage(raw: dict[str, Any], name: str) -> tuple[PermissionsConfig, list[str]]:
    """Validate a parsed policy mapping, or — when it's invalid — salvage
    TIGHTEN-ONLY: keep only the ``deny``/``ask`` rules that individually validate
    and drop everything else (allow rules, reclassify, connections, cost,
    default_mode). The built-in defaults PROMPT on every mutation, so a human
    stays in the loop even when a stricter config failed to load; a salvaged
    fragment can only make the policy STRICTER, never recover an ``allow``."""
    try:
        return PermissionsConfig.model_validate(raw), []
    except ValidationError:
        kept: list[PermissionRule] = []
        for item in raw.get("rules") or []:
            try:
                rule = PermissionRule.model_validate(item)
            except ValidationError:
                continue
            if rule.decision in ("deny", "ask"):
                kept.append(rule)
        warning = (
            f"{name} is invalid and could not be fully loaded; fell back to safe "
            f"defaults (kept {len(kept)} deny/ask rule(s), dropped everything else). "
            "Mutations will PROMPT — fix the file to restore your policy."
        )
        return PermissionsConfig(rules=kept), [warning]


def _validate_stripped(raw: dict[str, Any], name: str) -> tuple[PermissionsConfig, list[str]]:
    """:func:`_strip_environment_scoping` (retired-key cleanup, with its loud
    warnings) then :func:`_validate_or_salvage` — the shared per-file load path."""
    raw = dict(raw)
    warnings = _strip_environment_scoping(raw, name)
    config, more = _validate_or_salvage(raw, name)
    return config, [*warnings, *more]


#: How a stance-LESS rule in the gitignored local overlay is read. Nothing but
#: the engine writes a grant there (:func:`add_local_rule`, on a person's "Always
#: allow"), so a rule with no stance on it is one recorded before the stance was —
#: not a policy statement. Reading it as unscoped would leave every install that
#: predates the stamp with the hole the stance clamp closes, so it is read as the
#: LOOSEST stance that records a grant at all: it keeps binding where it was
#: plausibly given, and a stance that asks more asks once and widens it. A rule in
#: the committed ``permissions.yml`` is a person's policy and stays unscoped.
_LEGACY_LOCAL_STANCE = "auto"


def _recorded_stance(raw: Any) -> str:
    """The stance a local-overlay rule was recorded under, reading a missing or
    non-string stamp as :data:`_LEGACY_LOCAL_STANCE`."""
    return raw if isinstance(raw, str) else _LEGACY_LOCAL_STANCE


def _as_recorded_grants(local: PermissionsConfig) -> PermissionsConfig:
    """``local`` with every stance-less ALLOW read as a recorded grant.

    Only an allow is touched: a deny or an ask binds every stance anyway, and
    stamping one would say something about it that nobody recorded. Every rule is
    marked as the overlay's, whatever its decision (:meth:`PermissionRule.grants_exec`)."""
    for rule in local.rules:
        rule._origin = "local"
        if rule.decision == "allow" and rule.mode is None:
            rule.mode = _LEGACY_LOCAL_STANCE
    return local


def _overlay_local(
    base: PermissionsConfig, local: PermissionsConfig, local_raw: dict[str, Any]
) -> PermissionsConfig:
    """Overlay the gitignored ``permissions.local.yml`` onto the project policy.

    Rules CONCATENATE (collect-all ``max`` precedence means a local ``deny`` still
    wins and a local ``allow`` can't loosen a project ``deny`` — tighten-only, the
    safe default). Local ``reclassify`` rules shadow project ones (first match
    wins, so local goes first); ``default_mode`` / ``cost`` are overridden when
    the local file provides them."""
    merged = base.model_copy(deep=True)
    merged.rules = [*base.rules, *local.rules]
    merged.reclassify = [*local.reclassify, *base.reclassify]
    if "default_mode" in local_raw:
        merged.default_mode = local.default_mode
    if local.cost:
        merged.cost = {**base.cost, **local.cost}
    return merged


def load_permissions(alkera_dir: Path) -> PermissionsConfig:
    """Load the effective project policy: ``.alkera/permissions.yml`` (default on
    first run) overlaid with the gitignored ``.alkera/permissions.local.yml``.

    NEVER raises: a malformed file fails closed to built-in defaults
    (tighten-only salvage) and records a loud ``load_warnings`` entry instead. The
    two files are handled INDEPENDENTLY so a broken overlay can't poison a valid
    project file (and vice versa)."""
    warnings: list[str] = []

    project_raw, perr = _read_raw(alkera_dir / PERMISSIONS_FILE)
    if perr:
        warnings.append(perr)
    if project_raw is None:
        base = PermissionsConfig()
    else:
        base, pw = _validate_stripped(project_raw, PERMISSIONS_FILE)
        warnings.extend(pw)

    local_raw, lerr = _read_raw(alkera_dir / PERMISSIONS_LOCAL_FILE)
    if lerr:
        warnings.append(lerr)
    if local_raw is not None:
        local, lw = _validate_stripped(local_raw, PERMISSIONS_LOCAL_FILE)
        warnings.extend(lw)
        base = _overlay_local(base, _as_recorded_grants(local), local_raw)

    base._load_warnings = warnings
    base._source = (alkera_dir, _policy_files_key(alkera_dir))
    return base


def _policy_files_key(
    alkera_dir: Path,
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """A change-detection key for the two policy files: ``(mtime_ns, size,
    inode)`` of each, with a sentinel for a missing file. Any edit changes the
    key: content ⇒ size, a same-size rewrite ⇒ mtime — and the inode closes the
    residual gap on coarse-timestamp filesystems (exFAT/NFS), because every
    atomic write lands via ``os.replace`` of a NEW temp file, minting a fresh
    inode even when mtime granularity can't tell two rewrites apart."""
    out: list[tuple[int, int, int]] = []
    for name in (PERMISSIONS_FILE, PERMISSIONS_LOCAL_FILE):
        try:
            st = (alkera_dir / name).stat()
            out.append((st.st_mtime_ns, st.st_size, st.st_ino))
        except FileNotFoundError:
            out.append((-1, -1, -1))
    return out[0], out[1]


# Bounded mtime-keyed cache for the standalone `tool.call` path, which would
# otherwise re-read + re-parse the policy YAML on every invocation. Keyed by
# (abs dir, files key) so an edit busts it and different projects never collide.
_PERMISSIONS_CACHE: dict[tuple[str, Any], PermissionsConfig] = {}
_PERMISSIONS_CACHE_MAX = 64
# The lock guards the multi-step LRU update against a caller on a thread pool: an
# unguarded race could raise, and a swallowed KeyError upstream would drop the
# project's deny rules for that one decision.
_PERMISSIONS_CACHE_LOCK = threading.Lock()


def load_permissions_cached(alkera_dir: Path) -> PermissionsConfig:
    """:func:`load_permissions` with an ``(mtime, size, inode)``-keyed cache —
    picks up live edits (the key changes) without re-parsing the YAML on every
    call. The returned config is treated read-only by callers (the gate never
    mutates it)."""
    key = (str(alkera_dir.resolve()), _policy_files_key(alkera_dir))
    with _PERMISSIONS_CACHE_LOCK:
        cached = _PERMISSIONS_CACHE.pop(key, None)
        if cached is not None:
            # Re-insert to refresh recency (a dict preserves insertion order),
            # making the bound a true LRU.
            _PERMISSIONS_CACHE[key] = cached
            return cached
    config = load_permissions(alkera_dir)
    with _PERMISSIONS_CACHE_LOCK:
        if len(_PERMISSIONS_CACHE) >= _PERMISSIONS_CACHE_MAX:
            # Evict only the single oldest entry, not the whole cache — a full
            # clear would make a daemon serving many concurrent sessions re-read
            # + re-parse every project's YAML in a burst right after eviction.
            _PERMISSIONS_CACHE.pop(next(iter(_PERMISSIONS_CACHE)), None)
        _PERMISSIONS_CACHE[key] = config
    return config


def save_permissions(alkera_dir: Path, config: PermissionsConfig) -> None:
    text = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    write_text_atomic(alkera_dir / PERMISSIONS_FILE, text)


#: ``/cost set`` window name → the ``cost:`` block cap key it writes.
COST_CAP_KEYS: dict[str, str] = {
    "per-query": "per_query_usd_cap",
    "per_query": "per_query_usd_cap",
    "chat": "chat_usd_cap",
    "day": "day_usd_cap",
    "week": "week_usd_cap",
}


# In-process serialization in FRONT of the cross-process FileLock for the
# permissions.local.yml read-modify-write — the same layering CostLedger and
# DecisionSink use. The write itself is atomic, but two unserialized writers
# (two sessions persisting "always" rules, or /cost set racing the daemon RPC)
# would read the same base and the second's atomic write erases the first's
# rule — including a persisted always-DENY.
_LOCAL_WRITE_TLOCK = threading.Lock()


@contextmanager
def _local_write_lock(alkera_dir: Path) -> Iterator[None]:
    lock = FileLock(alkera_dir / ".permissions.local.lock")
    with _LOCAL_WRITE_TLOCK, retrying_lock(lock, timeout_seconds=2.0):
        yield


def exact_match_glob(text: str) -> str:
    """A ``match`` glob that matches ``text`` and nothing else: every glob
    metacharacter is bracketed so it reads as itself."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def pins_one_text(match: str) -> bool:
    """Whether the ``match`` glob matches one text only: every metacharacter in
    it is bracketed to read as itself (what :func:`exact_match_glob` writes)."""
    return not re.search(r"[*?\[]", re.sub(r"\[[*?\[]\]", "", match))


def add_local_rule(
    alkera_dir: Path,
    descriptor: ActionDescriptor,
    *,
    decision: RuleDecision = "allow",
    mode: str,
    exact: bool = False,
    owner: str | None = None,
) -> bool:
    """Persist a human's ``always-allow`` / ``always-reject`` for this action's
    ``(capability, operation)`` as a rule in the gitignored ``permissions.local.yml``,
    written ATOMICALLY. Returns whether the file changed (``False`` when an
    equivalent rule already reached at least as far — idempotent).

    ``exact`` records the command line itself rather than its family — what a
    destructive command's "Always allow" means (:mod:`.consent_scope`).

    ``mode`` is the stance the person answered the card in, stamped on the rule so a
    stricter stance later asks again instead of replaying an answer given where less
    was asked (:meth:`PermissionRule.binds_in`). It is required rather than defaulted:
    a grant whose stance nobody named would silently reach everywhere.

    ``owner`` names the person the answer is for, on a shared host, where one
    file holds the answers of every person whose chats run there: the rule then
    decides that person's chats alone (:meth:`PermissionRule.binds_for`). ``None``
    on a local machine, whose one owner every rule already belongs to.

    Answering "Always allow" AGAIN under a stricter stance WIDENS the rule in place
    rather than adding a second one — the person has now granted it where more is
    asked, and two rules for one action would be a policy file that grows per stance.

    Carries across sessions, and other chats sharing this ``.alkera`` pick it up
    on their next decision via the ``(mtime, size)``-keyed cache (no restart). The
    floor stays unwaivable: callers only persist an ``allow`` for a NON-floor,
    KNOWN-confidence action (an ``allow_always`` on a floor/unknown action is
    clamped to ``once`` upstream), so this never records an allow for a
    destroy/egress. A ``deny`` is always safe to persist (tighten-only)."""
    rule: dict[str, Any] = {"capability": descriptor.capability, "decision": decision}
    if owner:
        rule["owner"] = owner
    if descriptor.operation:
        rule["operation"] = descriptor.operation
    if (exact or descriptor.scope == "command") and descriptor.raw:
        # A compound command has no family, and a destructive one was consented
        # to as written (``exact``): the answer covers this text only.
        rule["match"] = exact_match_glob(normalize_command(descriptor.raw))

    with _local_write_lock(alkera_dir):
        raw, _err = _read_raw(alkera_dir / PERMISSIONS_LOCAL_FILE)
        data = raw or {}
        rules = list(data.get("rules") or [])
        for existing in rules:
            if not (
                isinstance(existing, dict)
                and existing.get("decision") == decision
                and existing.get("capability") == rule["capability"]
                and existing.get("operation") == rule.get("operation")
                and existing.get("match") == rule.get("match")
                and existing.get("owner") == rule.get("owner")
            ):
                continue
            recorded = _recorded_stance(existing.get("mode"))
            if grant_reaches(granted_in=recorded, mode=mode):
                return False  # already reaches this stance — don't duplicate
            existing["mode"] = mode  # granted again where more is asked: widen it
            break
        else:
            verb = "always-allowed" if decision == "allow" else "always-rejected"
            rule["reason"] = f"{verb} from a chat prompt"
            rule["mode"] = mode
            rules.append(rule)
        data["rules"] = rules
        write_text_atomic(
            alkera_dir / PERMISSIONS_LOCAL_FILE, yaml.safe_dump(data, sort_keys=False)
        )
        return True


def update_local_cost_caps(alkera_dir: Path, caps: dict[str, float]) -> None:
    """Merge ``{window: usd}`` cap overrides into the gitignored
    ``permissions.local.yml`` ``cost:`` block (personal, not committed), written
    atomically. Unknown window names raise ``ValueError``."""
    with _local_write_lock(alkera_dir):
        raw, _err = _read_raw(alkera_dir / PERMISSIONS_LOCAL_FILE)
        data = raw or {}
        cost = dict(data.get("cost") or {})
        for window, usd in caps.items():
            key = COST_CAP_KEYS.get(window)
            if key is None:
                raise ValueError(
                    f"unknown cost window {window!r}; expected one of per-query/chat/day/week"
                )
            value = float(usd)
            # A negative or non-finite cap is a footgun: a NaN cap makes every
            # `projected > cap` comparison False → the window is SILENTLY uncapped.
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"cost cap for {window!r} must be a finite, non-negative number")
            cost[key] = value
        data["cost"] = cost
        write_text_atomic(
            alkera_dir / PERMISSIONS_LOCAL_FILE, yaml.safe_dump(data, sort_keys=False)
        )


__all__ = [
    "COST_CAP_KEYS",
    "EffectRule",
    "PermissionRule",
    "PermissionsConfig",
    "add_local_rule",
    "load_permissions",
    "load_permissions_cached",
    "save_permissions",
    "update_local_cost_caps",
]
