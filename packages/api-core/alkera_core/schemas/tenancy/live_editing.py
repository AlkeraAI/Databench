"""Whether an org's files open live, on the wire (the platform admin's switch)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, StrictBool


class OrgLiveEditingUpdate(BaseModel):
    """The org's own setting: ``true`` or ``false`` wins over the deployment's
    in either direction; ``null`` clears it, so the org follows the
    deployment again. The key is required, so an empty body changes
    nothing by accident; only a JSON boolean is a setting (``"off"`` or
    ``0`` is a 422, never read as false)."""

    enabled: StrictBool | None


class OrgLiveEditingRead(BaseModel):
    """Where an org stands on live editing, as the platform sees it."""

    org_id: UUID
    enabled: bool
    """Whether the org's files open live now: its own setting, else the
    deployment's."""
    override: bool | None
    """The org's own setting; ``null`` follows the deployment."""
    deployment_default: bool
    """The deployment's ``LIVE_EDITING_ENABLED``."""
    sessions_written: int | None = None
    """On a change that turned live editing off: the sessions holding edits
    not yet on the drive that were written back before answering."""
    sessions_left_unsaved: int | None = None
    """On that change: the sessions the unsaved sweep is left to write back
    (a refused write back, or one not reached in time)."""
