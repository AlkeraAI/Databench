"""The Files ORM models.

This package holds every Appendix A3 table: the tree, its permissions, its
history and its content chain, plus the operations, uploads, leases, platform
rows and the tables a later phase fills. Both groups are re-exported from
``alkera_core.models`` so Alembic autogenerate sees every one of them.
"""

from alkera_core.models.files._types import LTREE
from alkera_core.models.files.acl import (
    PRINCIPAL_KINDS,
    FileAcl,
    FileAclMember,
    FileShare,
)
from alkera_core.models.files.history import (
    CHURN_AUTOVACUUM_SCALE_FACTOR,
    CHURN_TABLES,
    CONFLICT_STATES,
    HISTORY_KINDS,
    FileConflict,
    FileContentGrant,
    FileDirStats,
    FileDirStatsDelta,
    FileHistory,
    FileRetentionLabel,
    FileStar,
    FileTrashOp,
)
from alkera_core.models.files.later import (
    HOLD_SCOPES,
    LATER_CHURN_TABLES,
    LINK_SCOPES,
    LOCK_ENFORCEMENTS,
    LOCK_KINDS,
    FileErasureLog,
    FileHold,
    FileKeyChunk,
    FileLink,
    FileLock,
    FileManifest,
    FilePack,
    ManifestTerm,
    StorageUsageSnapshot,
)
from alkera_core.models.files.leases import (
    LEASE_PURPOSES,
    LIVE_ENTRY_STATES,
    STAGE_DIRECTIONS,
    STAGE_STATES,
    FileLease,
    FileLeaseEpochHwm,
    FileLeaseLiveEntry,
    FileStageJob,
)
from alkera_core.models.files.ops import (
    IDEMPOTENCY_STATUSES,
    OP_KINDS,
    OP_STATES,
    OPS_CHURN_TABLES,
    FileIdempotencyKey,
    FileOp,
)
from alkera_core.models.files.page_grants import FilePageGrant
from alkera_core.models.files.platform import (
    QUARANTINE_KINDS,
    FilePlatform,
    FileQuarantine,
    FileSweepShard,
)
from alkera_core.models.files.stores import (
    DRIVE_FROZEN_REASONS,
    DRIVE_KINDS,
    STORE_DRIVERS,
    TRANSFER_MODES,
    DedupDomain,
    FileDrive,
    FileStore,
)
from alkera_core.models.files.tree import (
    MIME_CLASSES,
    NODE_KINDS,
    NODE_STATES,
    NODE_TRUST,
    SYMLINK_KINDS,
    FileNode,
)
from alkera_core.models.files.uploads import (
    UPLOAD_CHURN_TABLES,
    UPLOAD_SESSION_LIVE_STATES,
    UPLOAD_SESSION_STATES,
    FileUploadPart,
    FileUploadSession,
)
from alkera_core.models.files.versions import (
    SCAN_STATES,
    VERSION_SOURCES,
    FileVersion,
)

#: Every Files table that carries the churn autovacuum setting, so the
#: migration and its test read one list rather than four.
FILES_CHURN_TABLES = (*CHURN_TABLES, *OPS_CHURN_TABLES, *UPLOAD_CHURN_TABLES, *LATER_CHURN_TABLES)

__all__ = [
    "CHURN_AUTOVACUUM_SCALE_FACTOR",
    "CHURN_TABLES",
    "CONFLICT_STATES",
    "DRIVE_FROZEN_REASONS",
    "DRIVE_KINDS",
    "FILES_CHURN_TABLES",
    "HISTORY_KINDS",
    "HOLD_SCOPES",
    "IDEMPOTENCY_STATUSES",
    "LATER_CHURN_TABLES",
    "LEASE_PURPOSES",
    "LINK_SCOPES",
    "LIVE_ENTRY_STATES",
    "LOCK_ENFORCEMENTS",
    "LOCK_KINDS",
    "LTREE",
    "MIME_CLASSES",
    "NODE_KINDS",
    "NODE_STATES",
    "NODE_TRUST",
    "OPS_CHURN_TABLES",
    "OP_KINDS",
    "OP_STATES",
    "PRINCIPAL_KINDS",
    "QUARANTINE_KINDS",
    "SCAN_STATES",
    "STAGE_DIRECTIONS",
    "STAGE_STATES",
    "STORE_DRIVERS",
    "SYMLINK_KINDS",
    "TRANSFER_MODES",
    "UPLOAD_CHURN_TABLES",
    "UPLOAD_SESSION_LIVE_STATES",
    "UPLOAD_SESSION_STATES",
    "VERSION_SOURCES",
    "DedupDomain",
    "FileAcl",
    "FileAclMember",
    "FileConflict",
    "FileContentGrant",
    "FileDirStats",
    "FileDirStatsDelta",
    "FileDrive",
    "FileErasureLog",
    "FileHistory",
    "FileHold",
    "FileIdempotencyKey",
    "FileKeyChunk",
    "FileLease",
    "FileLeaseEpochHwm",
    "FileLeaseLiveEntry",
    "FileLink",
    "FileLock",
    "FileManifest",
    "FileNode",
    "FileOp",
    "FilePack",
    "FilePageGrant",
    "FilePlatform",
    "FileQuarantine",
    "FileRetentionLabel",
    "FileShare",
    "FileStageJob",
    "FileStar",
    "FileStore",
    "FileSweepShard",
    "FileTrashOp",
    "FileUploadPart",
    "FileUploadSession",
    "FileVersion",
    "ManifestTerm",
    "StorageUsageSnapshot",
]
