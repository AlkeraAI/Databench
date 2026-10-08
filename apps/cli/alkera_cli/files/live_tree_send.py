"""How a live sync lands its tree rows when the route asks for less.

The tree route takes a batch of rows for a held folder. It can refuse one as
too large (halved and sent again, down to one row) or as not this lease's: a
path it will not file under the folder, or a lease that is not live in this
holder's hands. The search below tells the two apart by asking again with
less, so a row is dropped for a bad path and never for a lease that comes back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from alkera_cli.files.live_paths import refused_paths, tree_safe
from alkera_cli.files.mount import LeaseSupersededError
from alkera_cli.files.refusals import status_of
from alkera_cli.files.wire import conflict_code

if TYPE_CHECKING:
    from alkera_cli.files.live_sync import TreeAnswer, TreeEntry

#: The code the tree route answers a batch with when it will not take it for
#: this lease: a path it will not file under the folder, or a lease that is
#: not live in this holder's hands. Which of the two is found by asking again
#: with less (see :meth:`TreeSend._search_mismatch`).
MISMATCH_CODE: Final = "files.lease_mismatch"


@dataclass
class _Search:
    """Where a halving search for a batch's refused paths has got to."""

    refused: list[TreeEntry] = field(default_factory=list)
    accepted: bool = False


@dataclass(frozen=True, slots=True)
class TreeSend:
    """One send of tree rows for a live sync: ``post`` makes one ``tree`` call
    (journaled), ``landed`` takes in what a call filed, and the log lines name
    the sync by ``who`` and ``log_ids`` on its ``logger``."""

    post: Callable[[Sequence[TreeEntry]], TreeAnswer]
    landed: Callable[[Sequence[TreeEntry], TreeAnswer, set[str]], None]
    who: str
    log_ids: Mapping[str, str]
    logger: logging.Logger

    def send(self, chunk: Sequence[TreeEntry], settled: set[str]) -> None:
        """Land ``chunk``, splitting it where the route asks for less.

        A batch too large for the route is halved and each half sent, down to
        a single entry — which, refused as too large on its own, is the one
        row no split can land and is left off the drive. A batch the route
        will not take as this lease's is searched for the paths it objects to
        (:meth:`_search_mismatch`). Every other refusal is the caller's.
        """
        try:
            answer = self.post(chunk)
        except LeaseSupersededError:
            raise
        except Exception as refusal:
            if status_of(refusal) == 413:
                if len(chunk) > 1:
                    middle = len(chunk) // 2
                    self.send(chunk[:middle], settled)
                    self.send(chunk[middle:], settled)
                    return
                self.drop(chunk, settled, why="is larger than one batch may be")
                return
            if conflict_code(refusal) == MISMATCH_CODE:
                self._search_mismatch(chunk, refusal, settled)
                return
            raise
        self.landed(chunk, answer, settled)

    def _search_mismatch(
        self, chunk: Sequence[TreeEntry], refusal: Exception, settled: set[str]
    ) -> None:
        """Find what the route refused as not this lease's, and send the rest.

        The refusal means one of two things: a path the route will not file
        under this folder, or a lease that is not live in this holder's hands.
        A path the answer names, or one this holder's own copy of the route's
        rule refuses, is dropped and the rest resent. Otherwise the batch is
        halved until the refused entries are found — and the search concludes
        it is the LEASE, not a path, once two single entries are refused with
        nothing accepted beside them: the batch is then kept whole and the
        refusal raised, so no row is dropped for a lease that comes back.
        """
        self.logger.warning(
            "live sync of %s: a tree batch of %d entries was refused as not this lease's",
            self.who,
            len(chunk),
            extra=self.log_ids,
        )
        named = refused_paths(refusal, chunk) or {
            entry.path
            for entry in chunk
            if not tree_safe(entry.path) or (entry.from_ is not None and not tree_safe(entry.from_))
        }
        if named:
            self.drop(
                [entry for entry in chunk if entry.path in named],
                settled,
                why="is not a path the drive files under this folder",
            )
            rest = [entry for entry in chunk if entry.path not in named]
            if rest:
                self.send(rest, settled)
            return
        search = _Search()
        self._bisect(list(chunk), refusal, settled, search)
        if not search.accepted:
            raise refusal
        self.drop(search.refused, settled, why="is not a path the drive files under this folder")

    def _bisect(
        self,
        part: list[TreeEntry],
        refusal: Exception,
        settled: set[str],
        search: _Search,
    ) -> None:
        if len(part) == 1:
            search.refused.append(part[0])
            if len(search.refused) >= 2 and not search.accepted:
                raise refusal
            return
        middle = len(part) // 2
        for half in (part[:middle], part[middle:]):
            try:
                answer = self.post(half)
            except LeaseSupersededError:
                raise
            except Exception as again:
                if conflict_code(again) != MISMATCH_CODE:
                    raise
                self._bisect(half, again, settled, search)
                continue
            search.accepted = True
            self.landed(half, answer, settled)

    def drop(self, entries: Sequence[TreeEntry], settled: set[str], *, why: str) -> None:
        """Leave these rows off the drive, on the record."""
        for entry in entries:
            settled.add(entry.path)
            self.logger.warning(
                "live sync of %s: the row for %s %s; left off the drive",
                self.who,
                entry.path,
                why,
                extra=self.log_ids,
            )
