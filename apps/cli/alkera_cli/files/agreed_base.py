"""The bytes a caller last agreed with the drive a file held, and the etag
they were agreed at: what a holder's write is fenced on."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgreedBase:
    """What a caller last agreed with the drive a file held, and at which etag.

    A live holder's disk is a copy of one version of the node plus whatever the
    agent wrote since. When the drive's head is still that version, the write
    fences on the head as any push does. When the head is some other content,
    somebody else's write landed after the holder's last agreement, and the
    precondition is the holder's own older etag: the drive sees the moved head
    and settles the two writes -- the holder's bytes keep the name and the
    displaced head becomes a conflicted copy -- instead of taking the upload as
    a plain new version over bytes the holder never saw.
    """

    etag: str
    content_hash: str

    def behind(self, content_hash: str) -> bool:
        """Whether a file holding ``content_hash``, whose head on the drive is
        something else, is a copy behind the drive rather than an edit.

        A file still holding exactly the agreed bytes has no change of its
        own: the head moved because somebody else wrote it (a person's save, a
        version restore). Sent, those bytes would be filed as a write made
        after theirs and push their version aside as a conflicted copy, so a
        push sends nothing and the drive's bytes come down to the file
        instead. Only bytes that moved away from the agreed ones are an edit.
        """
        return content_hash == self.content_hash


__all__ = ["AgreedBase"]
