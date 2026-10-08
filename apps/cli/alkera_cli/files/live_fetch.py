"""The live plane's fence around a call, and its fetch of a drive's bytes.

Kept apart from the live sync so each reads on its own: the fence turns a
refusal that says the folder is not this holder's any more into the one error
a caller must act on, and the fetch lands a download in the inbound spool,
never in the chat's tree.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Protocol

from alkera_cli.files.inbound_backoff import InboundBackoff
from alkera_cli.files.mount import LeaseSupersededError
from alkera_cli.files.push import conflict_code
from alkera_cli.files.spool import InboundSpool

__all__ = ["FENCED_CODE", "fenced", "fetch_inbound"]

#: The code a write refused by the folder's fence carries.
FENCED_CODE: Final = "files.lease_fenced"

#: The live sync's own channel: what the fetch says is the sync's to say.
logger = logging.getLogger("alkera_cli.files.live_sync")


class _Downloads(Protocol):
    def download(self, node_id: str, into: Path, *, deadline: float) -> None: ...


@contextlib.contextmanager
def fenced() -> Iterator[None]:
    """Turn the fence's refusal into the one error a caller must act on.

    Every other failure is left alone: a refused upload is a bug or a hiccup to
    surface, while a fenced one means the folder is not ours any more and the
    only correct next move is to stop writing.
    """
    try:
        yield
    except LeaseSupersededError:
        raise
    except Exception as failure:
        if conflict_code(failure) == FENCED_CODE:
            raise LeaseSupersededError(
                "this folder's lease moved on while the live sync was writing"
            ) from failure
        raise


def fetch_inbound(
    api: _Downloads,
    spool: InboundSpool,
    node_id: str,
    relative: str,
    *,
    deadline: float,
    who: str,
    ids: dict[str, str],
    backoff: InboundBackoff,
    seq: int,
    now: float,
) -> Path | None:
    """Fetch entry ``seq`` of ``node_id`` into the spool; the file it arrived
    in (the caller's to put in place and to remove), or None while it is
    refused or still inside its wait (``InboundBackoff``).

    A download that arrived whole is kept, however long it took: the
    ceiling handed to the API (``deadline``) is what cuts a transfer that has
    stalled, and it is sized so that only a stalled one reaches it. Bytes
    that made it are never thrown away for having been slow: that is how a
    file dropped into a chat over a slow link was fetched, discarded and
    asked for again on every beat without ever landing.
    """
    if backoff.waiting(node_id, seq, now):
        return None
    temporary = spool.new_file(relative)
    try:
        with fenced():
            api.download(node_id, temporary, deadline=deadline)
    except Exception as failure:
        # No half-downloaded file is left behind, whatever happened. A
        # drive that refused these bytes has not lost them: the entry stays
        # owed and the next drain asks again. Failing the batch here would
        # also leave every entry that DID land unsettled, and an unsettled
        # entry is re-downloaded onto a file the agent may since have
        # edited.
        with contextlib.suppress(OSError):
            temporary.unlink()
        if isinstance(failure, LeaseSupersededError):
            raise
        if backoff.refused(node_id, seq, now):
            logger.warning(
                "live sync of %s: inbound download of %s failed (%s: %s); left owed, "
                "asked for again on a widening wait",
                who,
                node_id,
                type(failure).__name__,
                failure,
                extra=ids,
            )
        return None
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    backoff.landed(node_id)
    return temporary
