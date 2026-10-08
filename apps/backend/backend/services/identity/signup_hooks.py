"""What a distribution runs when a person signs up.

Both signup routes (email and password, and an identity provider's first
sign-in) call :func:`run_signup_hooks` once the new user is flushed and before
the session is issued. A hook may write in the request's session; it must not
refuse the signup. With nothing registered a signup runs no hook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from alkera_core.extensions import ExtensionPoint
from alkera_core.models import User
from sqlalchemy.ext.asyncio import AsyncSession

#: One hook: the signup request's cookies, its session and the new user.
SignupHook = Callable[[Mapping[str, str], AsyncSession, User], Awaitable[None]]

SIGNUP_HOOKS: ExtensionPoint[SignupHook] = ExtensionPoint("identity_signup_hooks")


async def run_signup_hooks(cookies: Mapping[str, str], db: AsyncSession, user: User) -> None:
    """Run every registered hook, in registration order."""
    for hook in SIGNUP_HOOKS.items():
        await hook(cookies, db, user)


__all__ = ["SIGNUP_HOOKS", "SignupHook", "run_signup_hooks"]
