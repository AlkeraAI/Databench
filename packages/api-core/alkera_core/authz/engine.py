"""The one place an authorization decision is made.

:func:`authorize` is pure and synchronous: no database, no clock, no request
object. The caller resolves every fact a policy could need — roles, entitlement,
row state — into ``attrs`` beforehand, and the engine only decides. That keeps
every branch reachable from a unit test with plain data in, plain data out.

The engine denies by default. In order:

* no policy is registered for the resource type → deny ``no_policy``;
* the resource belongs to an org the context does not serve → deny
  ``cross_org`` as an opaque not-found, before any policy runs, so tenancy
  never depends on a policy remembering to check it (a user, a token and an
  agent serve their own org alone; a machine also serves the orgs it is
  assigned to, see :meth:`ActingContext.serves`);
* a policy asks for an attribute the caller did not supply, or supplied with
  the wrong type → deny ``missing_attribute:<key>``;
* a policy returns ``None`` → deny ``no_rule_matched``.

Any doubt resolves to a deny. A policy is registered once per resource type at
import time (see :mod:`alkera_core.authz.policies`) and may not declare an
audited attribute that :data:`~alkera_core.authz.decision.NEVER_AUDITED_KEYS`
forbids, so the redaction rule is enforced at registration rather than trusted
at write time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TypeVar

from alkera_core.authz.decision import NEVER_AUDITED_KEYS, Decision, deny
from alkera_core.authz.enums import Action, ResourceType
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.resource import Resource

T = TypeVar("T")

PolicyFn = Callable[[ActingContext, Action, Resource, Mapping[str, object]], Decision | None]
"""A policy: the acting context, the action, the resource and the caller-supplied
facts in; a :class:`Decision`, or ``None`` when no rule matched, out."""

#: The policy name a decision carries when no policy exists for the resource type.
NO_POLICY = "none"

#: The HTTP detail text of every engine-level deny that is not a tenancy miss.
NOT_ALLOWED = "Not allowed"


#: The key of a refusal message: the action refused and the purpose it was
#: asked with (``""`` when the action carries none).
MessageKey = tuple[Action, str]


@dataclass(frozen=True, slots=True)
class Policy:
    """A registered policy: its name (carried on every decision it makes), the
    resource type it decides for, the decide function, the allowlist of
    attribute names a decision row may record, and the sentence each refusal
    reads as, per action and purpose.

    A refusal message is per action because a reader refused a look and a
    reader refused a change need different sentences: telling someone who
    asked to see a machine's cost that they cannot change the machine answers
    a question they did not ask. :func:`register` refuses a policy that serves
    one sentence to both.
    """

    name: str
    resource_type: ResourceType
    decide: PolicyFn
    audited_attrs: frozenset[str]
    messages: Mapping[MessageKey, str] = field(default_factory=dict)

    def message_for(self, action: Action, purpose: str = "") -> str:
        """The refusal sentence for ``action`` asked with ``purpose``: the one
        declared for that pair, else the action's own, else
        :data:`NOT_ALLOWED`."""
        return self.messages.get((action, purpose), self.messages.get((action, ""), NOT_ALLOWED))


class MissingAttributeError(KeyError):
    """A policy needed an attribute the caller did not supply, or supplied with
    the wrong type. :func:`authorize` turns it into a deny; ``args[0]`` is the
    attribute name."""


def require_attr(attrs: Mapping[str, object], key: str, typ: type[T]) -> T:
    """``attrs[key]`` when present and an instance of ``typ``; otherwise
    :class:`MissingAttributeError`. A wrong type is treated as absent on purpose — a
    flag that arrives as the string ``"false"`` must not read as truthy."""
    try:
        value = attrs[key]
    except KeyError:
        raise MissingAttributeError(key) from None
    if not isinstance(value, typ):
        raise MissingAttributeError(key)
    return value


_REGISTRY: dict[ResourceType, Policy] = {}


def register(policy: Policy) -> None:
    """Register ``policy`` for its resource type.

    Raises ``ValueError`` when the policy declares a never-audited attribute,
    when it declares one refusal sentence for a read and a change, or when a
    policy for that resource type already exists (two policies for one type
    would make the decision depend on import order).
    """
    forbidden = policy.audited_attrs & NEVER_AUDITED_KEYS
    if forbidden:
        raise ValueError(
            f"policy {policy.name!r} declares never-audited attributes: {sorted(forbidden)}"
        )
    reads = {text for (action, _), text in policy.messages.items() if action is Action.READ}
    changes = {text for (action, _), text in policy.messages.items() if action is not Action.READ}
    shared = reads & changes
    if shared:
        raise ValueError(
            f"policy {policy.name!r} refuses a read and a change with the same sentence: "
            f"{sorted(shared)}"
        )
    existing = _REGISTRY.get(policy.resource_type)
    if existing is not None:
        raise ValueError(
            f"a policy for {policy.resource_type.value!r} is already registered "
            f"({existing.name!r}); refusing {policy.name!r}"
        )
    _REGISTRY[policy.resource_type] = policy


def policy_for(resource_type: ResourceType) -> Policy | None:
    return _REGISTRY.get(resource_type)


def registered_policies() -> tuple[Policy, ...]:
    """Every registered policy, ordered by resource type value."""
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY, key=lambda rt: rt.value))


@contextmanager
def temporarily_registered(policy: Policy) -> Iterator[None]:
    """Test seam: register ``policy`` for the duration of the block, then remove
    it. Refuses a resource type that already has a policy, like :func:`register`."""
    register(policy)
    try:
        yield
    finally:
        if _REGISTRY.get(policy.resource_type) is policy:
            del _REGISTRY[policy.resource_type]


def authorize(
    ctx: ActingContext,
    action: Action,
    resource: Resource,
    attrs: Mapping[str, object],
) -> Decision:
    """Decide whether ``ctx`` may perform ``action`` on ``resource`` given the
    facts in ``attrs``. Never raises for a missing or mistyped fact; it denies."""
    policy = policy_for(resource.type)
    if policy is None:
        return deny(NO_POLICY, "no_policy", message=NOT_ALLOWED)
    if resource.org_id is not None and not ctx.serves(resource.org_id):
        return deny(policy.name, "cross_org", message="Not found", as_not_found=True)
    try:
        decision = policy.decide(ctx, action, resource, attrs)
    except MissingAttributeError as exc:
        return deny(policy.name, f"missing_attribute:{exc.args[0]}", message=NOT_ALLOWED)
    if decision is None:
        return deny(policy.name, "no_rule_matched", message=NOT_ALLOWED)
    return decision


__all__ = [
    "NOT_ALLOWED",
    "NO_POLICY",
    "MessageKey",
    "MissingAttributeError",
    "Policy",
    "PolicyFn",
    "authorize",
    "policy_for",
    "register",
    "registered_policies",
    "require_attr",
    "temporarily_registered",
]
