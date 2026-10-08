"""What the credential doors refuse with, worded for who reads the refusal.

The same refusal reaches two kinds of reader. A browser tab holds the session
cookie, and the person behind it reads the sentence in a toast or a dialog. A
CLI, an editor, a box's machine or worker credential, a personal access token
or a CI token holds a credential no tab ever holds, and its reader is a log line
or a terminal. A sentence about "another window" or an organization's single
sign-on is false for the second reader, so each refusal here carries both
sentences and the door picks by the credential it resolved. The code is the
same for both: clients branch on it.

A refusal never carries exception or parser text. What failed inside a decoder
goes to the log; the caller learns only which refusal it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from fastapi import HTTPException, status

#: The code every credential refusal at the doors carries unless it names its own.
UNAUTHORIZED_CODE: Final = "unauthorized"


class Reader(StrEnum):
    """Who reads a refusal: the browser tab, or any other credential's client."""

    BROWSER = "browser"
    CLIENT = "client"

    @classmethod
    def of(cls, *, browser_session: bool) -> Reader:
        return cls.BROWSER if browser_session else cls.CLIENT


@dataclass(frozen=True, slots=True)
class Refusal:
    """One refusal: its status, its code, and its sentence for each reader."""

    status: int
    code: str
    browser: str
    client: str
    headers: dict[str, str] = field(default_factory=dict)

    def message(self, reader: Reader) -> str:
        return self.browser if reader is Reader.BROWSER else self.client

    def to(self, reader: Reader) -> HTTPException:
        return HTTPException(
            status_code=self.status,
            detail={"code": self.code, "message": self.message(reader)},
            headers=dict(self.headers) or None,
        )


#: The client asserted an org (``X-Alkera-Org``) that is not its credential's.
ORG_CHANGED: Final = Refusal(
    status=status.HTTP_409_CONFLICT,
    code="org_changed",
    browser="You switched organizations in another window.",
    client="The X-Alkera-Org header names an organization this credential does not act in.",
)

#: A browser write that named no org. Only a browser session is held to this.
ORG_ASSERTION_REQUIRED: Final = Refusal(
    status=status.HTTP_428_PRECONDITION_REQUIRED,
    code="org_assertion_required",
    browser="Reload this page to continue.",
    client="Name the organization in the X-Alkera-Org header.",
)

#: An account-wide change from a credential that does not stand for the account.
ACCOUNT_SIGN_IN_REQUIRED: Final = Refusal(
    status=status.HTTP_403_FORBIDDEN,
    code="account_sign_in_required",
    browser="Sign in to your own account, not an organization's single sign-on, to do this.",
    client="Only a browser signed in to your own account can do this.",
)

#: A session token that does not decode.
INVALID_SESSION: Final = Refusal(
    status=status.HTTP_401_UNAUTHORIZED,
    code=UNAUTHORIZED_CODE,
    browser="Your session is not valid. Sign in again.",
    client="The credential is not a valid session token.",
    headers={"WWW-Authenticate": "Cookie"},
)

#: Every refusal worded here, for the sweep that holds each to its readers.
REFUSALS: Final = (ORG_CHANGED, ORG_ASSERTION_REQUIRED, ACCOUNT_SIGN_IN_REQUIRED, INVALID_SESSION)


__all__ = [
    "ACCOUNT_SIGN_IN_REQUIRED",
    "INVALID_SESSION",
    "ORG_ASSERTION_REQUIRED",
    "ORG_CHANGED",
    "REFUSALS",
    "UNAUTHORIZED_CODE",
    "Reader",
    "Refusal",
]
