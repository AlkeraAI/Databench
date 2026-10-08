"""The typed failures every Files service raises.

A service never returns a status code: it raises one of these, and the route
layer maps the class to a status and the ``code`` to the body's machine-readable
field. Keeping the vocabulary here — rather than in the routes — is what lets
the library, the CLI and the worker all fail in the same words, and what lets a
test assert the *reason* a call was refused instead of its HTTP shape.

Every failure carries four things:

``code``
    The stable machine-readable reason. Each class has a default; a call
    overrides it with the first positional argument or with ``code=``.
``message``
    Prose for a human. It may change without notice and is never parsed.
``status``
    The HTTP status the route layer answers with, so the mapping lives beside
    the class rather than in a table the routes must keep in step.
``detail``
    Extra machine-readable facts a client can act on (which quota ran out, who
    holds a lease). Only ever ids and enums — never a name, a path or a body,
    because a refusal must not become an oracle for something the caller may
    not read.

Construction is deliberately forgiving, because callers spell the same failure
in several natural ways::

    NotFound()                                    # the class default code
    NotFound(code=some_code)                      # a code, explicitly
    NotFound("no operation 3f2a")                 # prose, by its shape
    NotFound(message="drive 3f2a")                # prose, explicitly
    InvalidRequest("files.bad_limit", "limit must be 1..1000")
    Conflict("files.leased", detail={"holder_principal_id": str(who)})
    QuotaExceeded(kind="bytes")                   # code files.quota_bytes

A lone positional string is read as a code when it *looks* like one — a dotted
lowercase identifier such as ``files.bad_limit`` — and as prose otherwise. That
rule is what lets ``NotFound(f"no node {node_id}")`` and a bare code literal
both be written without a keyword; anything ambiguous is spelled with ``code=``
or ``message=``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

#: A lone positional argument is a code when it matches this: a lowercase
#: dotted identifier with no spaces. Prose never does (it has spaces, capitals
#: or punctuation), so the two forms cannot be confused.
_CODE_SHAPE: Final = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")


def looks_like_code(text: str) -> bool:
    """Whether ``text`` is spelled as a machine code rather than as prose."""
    return bool(_CODE_SHAPE.match(text))


class FilesError(Exception):
    """Base of every typed Files failure."""

    #: The default code for the class; a call overrides it per instance.
    code: str = "files.error"
    #: The status the route layer answers with for this class.
    status: int = 500

    def __init__(
        self,
        code_or_message: str | None = None,
        message: str | None = None,
        *,
        code: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        resolved_code = code
        text: str | None
        if code is not None:
            # The code is explicit, so a lone positional cannot be one.
            text = message if message is not None else code_or_message
        elif message is not None:
            # Two arguments are always (code, message).
            resolved_code = code_or_message
            text = message
        elif code_or_message is not None and looks_like_code(code_or_message):
            resolved_code = code_or_message
            text = None
        else:
            text = code_or_message
        if resolved_code is not None:
            self.code = resolved_code
        self.message = text
        self.detail: dict[str, Any] | None = None if detail is None else dict(detail)
        super().__init__(text if text else self.code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class NotFound(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The addressed row does not exist, or the caller may not know that it does."""

    code = "files.not_found"
    status = 404


class PreconditionFailed(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """An ``If-Match`` / version precondition did not hold (412)."""

    code = "files.precondition_failed"
    status = 412


class Conflict(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The state refuses this change right now (409).

    The code names *which* refusal, because a caller retries some (``files.moving``
    once the move lands) and never others (``files.lease_fenced`` means a newer
    epoch owns the subtree). The set is open at runtime (a caller may add a code
    without editing this module), and :data:`KNOWN_CODES` is the catalogue a
    test keeps honest.
    """

    code = "files.conflict"
    status = 409


class QuotaExceeded(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The drive has no room for this write (507).

    ``kind`` says which limit ran out; it picks the code and is echoed in
    ``detail`` so a client can tell "buy more bytes" from "delete some files".
    """

    code = "files.quota_exceeded"
    status = 507

    def __init__(
        self,
        code_or_message: str | None = None,
        message: str | None = None,
        *,
        code: str | None = None,
        detail: Mapping[str, Any] | None = None,
        kind: str | None = None,
    ) -> None:
        if kind is not None:
            code = code or f"files.quota_{kind}"
            detail = {**(detail or {}), "kind": kind}
        super().__init__(code_or_message, message, code=code, detail=detail)

    @property
    def kind(self) -> str | None:
        """Which limit ran out (``bytes`` / ``nodes``), or ``None`` if unsaid."""
        return None if self.detail is None else self.detail.get("kind")


class InvalidRequest(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The request is well-formed but not a thing Files can do (422)."""

    code = "files.invalid_request"
    status = 422


class ContainerReadOnly(InvalidRequest):
    """A traversal-only container takes no direct write (422).

    ``/``, ``home/`` and ``Teams/`` are signposts: the only rows under them are
    the homes and team folders the drive's own ensure creates, each born with
    the grant that makes it reachable. A node put there by a caller carries no
    grant, so nobody but an org admin can ever open it and its owner's Files
    surface never shows it — which is why the refusal is the container's and
    not the role's, and why an org admin is refused exactly like everybody
    else. Its own class, rather than a bare :class:`InvalidRequest`, because a
    client tells "you may not write *here*" from "that request was malformed"
    by the code alone.
    """

    code = "files.container_readonly"
    status = 422


class TooLarge(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The caller declared more bytes than this deployment accepts (413).

    Its own class so the ceiling is refused where it is known — before a single
    byte moves — rather than as a late failure deep into a transfer that the
    caller cannot tell from an outage. Every message names both numbers: what
    was asked for and what the limit is.
    """

    code = "files.too_large"
    status = 413


class ReadOnlyContent(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The bytes behind this node cannot be replaced in place."""

    code = "files.read_only_content"
    status = 409


class StoreUnavailable(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The object store could not take this call right now (503).

    A throttle or an outage is not the caller's mistake and not an internal
    fault: the request is well-formed and the same call will work again. It has
    its own class so a driver refusal cannot leave as an opaque platform 500,
    which is what a client retries blindly and an operator cannot tell from a
    bug.
    """

    code = "files.store_unavailable"
    status = 503


#: The conflict codes Files distinguishes today. Kept as its own name because
#: the route layer and the CLI both branch on "is this retryable", and because
#: a reader looking for the 409 vocabulary should not have to filter
#: :data:`KNOWN_CODES` by hand.
CONFLICT_CODES: Final[tuple[str, ...]] = (
    "files.leased",
    "files.lease_fenced",
    # A move that would take a chat's folder out of its workspace or into
    # another one; moving chats between workspaces is a later feature.
    "files.chat_workspace_move",
    # A holder resuming a lease the server ended (the chat it served was put to
    # sleep, deleted or moved): not a lapse to re-take, the chat is over here.
    "files.lease_ended",
    "files.held",
    "files.inherited_grant",
    # A share to someone who already owns the item: owning is above every rung
    # a share hands out, so a second row would only list them twice.
    "files.already_owner",
    # A share on one chat in a workspace that holds several chats: the
    # workspace is what gets shared there.
    "files.share_the_workspace",
    # An org grant naming any org but the caller's own: the caller's input error,
    # answered the same for a real org and an id never issued.
    "files.org_principal_not_own",
    "files.cycle",
    "files.moving",
    "files.frozen",
    # The parent (or the node itself) is in the trash, so it cannot take a new
    # child or a new version until it is restored.
    "files.trashed",
    # The name is taken by a live sibling: raised by the partial unique index,
    # never by a pre-check.
    "files.exists",
    # The subtree is too large to move inside one request; the caller starts an
    # Operation instead.
    "files.large_move",
    "files.too_many_sessions",
    # One part disagrees with the row the server accepted for it (a different
    # checksum, or the object is not in the store at the size the row claims).
    "files.part_mismatch",
    # The completing part LIST disagrees with the set the server accepted — a
    # different failure from a single bad part, and the one `complete` reports.
    "files.parts_mismatch",
    "files.session_state",
    # The conflict this resolution names has already been decided; the
    # caller re-reads the node rather than deciding it twice.
    "files.conflict_resolved",
    # The live plane. A node the caller declared in flight is not under the
    # lease it named, so no mount owns the edit being reported; and a conflict
    # submission from anyone but the fenced holder of the folder's lease.
    "files.lease_mismatch",
    # A conflict submission names a displaced file that is not in the folder
    # the session files the copy into.
    "files.conflict_of_elsewhere",
    # One lease may hold only so many nodes in flight at once; the cap is the
    # deployment's, and the caller releases one before opening another.
    "files.live_too_many",
)

#: Every code the Files library can put on the wire. It is a catalogue, not a
#: gate: raising an unlisted code works, and a grep-driven test
#: (``test_files_errors.py``) fails when the source grows one that is not here,
#: so the catalogue cannot quietly rot. The families below are spelled as
#: prefixes because their tails come from another module's vocabulary.
KNOWN_CODES: Final[frozenset[str]] = frozenset(
    (
        *CONFLICT_CODES,
        # bases
        "files.error",
        "files.not_found",
        "files.precondition_failed",
        "files.conflict",
        "files.invalid_request",
        "files.read_only_content",
        # a direct write into `/`, `home/` or `Teams/`: the signposts hold only
        # what the drive's own ensure puts there
        "files.container_readonly",
        # forcing a box's lease off names why
        "files.force_reason_required",
        # a symlink whose target leaves the drive (a host path, a `..` above
        # the root) is never stored
        "files.link_outside_tree",
        # quota
        "files.quota_exceeded",
        "files.quota_bytes",
        "files.quota_nodes",
        # the caller's OWN storage limit (org-wide or under a team folder) is
        # reached — the drive may still have room; the org or team admin who
        # set the limit is who can raise it
        "files.user_quota_bytes",
        # idempotency
        "files.idempotency_key_required",
        "files.idempotency_mismatch",
        # listing, filters and delta
        "files.bad_limit",
        "files.invalid_marker",
        "files.bad_filter",
        "files.unknown_filter",
        # a request naming an id this server never issued (a malformed UUID)
        "files.bad_id",
        # batch: the reasons one submitted batch is not a batch this server
        # will run. Each is its own code because a client shows a different
        # thing for "you sent nothing", "you sent 5,000" and "item 3 names a
        # verb we do not have", and it must not have to parse a message.
        "files.bulk_empty",
        "files.bulk_too_large",
        # the holder's tree report: longer than one report may be (the holder
        # splits it and sends the halves), and a body whose encoding does not
        # decode
        "files.batch_too_large",
        "files.bad_encoding",
        # a file is made by its first version; only the holder of a live lease
        # may name one whose bytes are still on its disk
        "files.file_needs_content",
        "files.bulk_missing_field",
        "files.bulk_duplicate_id",
        "files.bulk_bad_op",
        # a batch item whose verb mutates a node arrived without the ifMatch
        # that verb requires; the batch refuses it rather than writing blind
        "files.if_match_required",
        # the operation row a batch runner picked up carries no plan
        "files.bulk_plan_missing",
        # an operation's body failed with something that is not a typed Files
        # refusal. The generic code is what a poller reads instead of prose
        # that would name rows, paths and driver internals.
        "files.operation_failed",
        # an operation that was queued and then never started: the recovery
        # offered it a runner its whole allowance of times and the runner was
        # gone each time, so the row says so rather than sitting at `queued`
        # for a client that is never told.
        "files.runner_lost",
        # search: the query text is empty or otherwise unusable
        "files.bad_query",
        # create_tree: the request names more folders, or deeper ones, than one
        # call may build
        "files.tree_too_large",
        "files.tree_too_deep",
        # uploads
        "files.invalid_size",
        # A version names an origin outside the version sources.
        "files.invalid_source",
        # The declared size (or the part count it implies) is over the
        # deployment's published ceiling; refused at open, never at commit.
        "files.too_large",
        # The body did not carry the number of bytes the request declared.
        "files.size_mismatch",
        # The streamed bytes disagree with the checksum declared for them.
        "files.checksum_mismatch",
        "files.not_a_folder",
        "files.empty_part",
        "files.part_out_of_range",
        # The bytes of a part disagree with the checksum the client declared
        # for it: the caller's error, told apart from a part the server had
        # already accepted with other bytes (``files.part_mismatch``).
        "files.part_checksum_mismatch",
        # The object store threw a throttle or an outage at a byte path.
        "files.store_unavailable",
        # authorization: a caller who may read the node but not do this to it.
        # A caller who may not read it gets the opaque ``files.not_found``.
        "files.forbidden",
        # a registered refusal: the object type has no renderer yet
        "files.rendering_not_implemented",
        # a registered refusal: the object type is retired and nothing renders
        # its bytes any more
        "files.rendering_retired",
        # the local-only crash hook was asked to die at a point it does not have
        "files.unknown_crash_point",
        # retention
        "files.retention_label_unsupported_kind",
        "files.invalid_batch",
        # a grant naming a principal kind no matcher has been taught: the row
        # could never resolve, and the kind is the caller's own input
        "files.unknown_principal_kind",
        # a refused name: the tail is the ``InvalidName.code`` that refused it
        "files.invalid_name.empty",
        "files.invalid_name.nul",
        "files.invalid_name.separator",
        "files.invalid_name.dot",
        "files.invalid_name.too_long",
        "files.invalid_name.control",
        "files.invalid_name.surrounding_space",
    )
)

#: Code prefixes whose tails are generated, so a scanner reading the source
#: sees only the stem. Each stem must have at least one full member above.
KNOWN_CODE_PREFIXES: Final[tuple[str, ...]] = ("files.invalid_name.", "files.quota_")


__all__ = [
    "CONFLICT_CODES",
    "KNOWN_CODES",
    "KNOWN_CODE_PREFIXES",
    "Conflict",
    "ContainerReadOnly",
    "FilesError",
    "InvalidRequest",
    "NotFound",
    "PreconditionFailed",
    "QuotaExceeded",
    "ReadOnlyContent",
    "StoreUnavailable",
    "TooLarge",
    "looks_like_code",
]
