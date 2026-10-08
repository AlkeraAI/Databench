"""The home-folder rename against the real schema.

Revision 0171 renames every member's home from the spelling the old derivation
took from their address — the local part, the local part with its domain when
a namesake shared the org, the whole address escaped when the local part was
not a valid name — to their user id, and stamps it ``subtype = 'home'``.
``alembic check`` sees none of it (no column changes), so this module seeds a
tree at the parent revision, runs the revision up and down, and pins which rows
moved: every home, and nothing that only looks like one — a folder the member
made beside their home, a folder named after an address somewhere else, a
folder under ``home/`` that no longer carries any of the member's spellings.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alkera_core.authz.principal import ActingContext
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.files import drives
from alkera_core.files.acl_intern import ace_body, body_hash
from alkera_core.files.authz.defaults import ORG_POLICY_RESTRICTED, DriveFolder, default_acl
from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch, seed_org_admin

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0171_home_folders_named_by_id.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0170"

_INSERT_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "name, name_display, name_key, flags_names, path_ids, depth, mode, uid, gid, nlink, size, "
    "rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, traversal_only, "
    "metadata, created_by) VALUES (:id, :ino, :drive, :org, :parent, :kind, NULL, :name, "
    ":display, :key, '{}'::jsonb, CAST(:path AS ltree), :depth, 493, 0, 0, 1, 0, 0, 0, 0, 0, "
    "0, '{}'::jsonb, 1, 0, :traversal, '{}'::jsonb, :by)"
)


async def _drive(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    store_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', :bucket, '{}'::jsonb, ARRAY['proxied'])"
        ),
        {"id": store_id, "bucket": f"homes-seed-{store_id.hex}"},
    )
    domain_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dedup_domains (id, org_team_id, store_id, chunker_seed) "
            "VALUES (:id, :org, :store, '\\x00'::bytea)"
        ),
        {"id": domain_id, "org": org_id, "store": store_id},
    )
    drive_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_drives "
            "(id, org_team_id, store_id, dedup_domain_id, quota_bytes, quota_nodes, next_ino) "
            "VALUES (:id, :org, :store, :domain, 0, 0, 1)"
        ),
        {"id": drive_id, "org": org_id, "store": store_id, "domain": domain_id},
    )
    return drive_id


@dataclass
class _Tree:
    session: AsyncSession
    drive: uuid.UUID
    org: uuid.UUID
    ino: int = 1

    async def node(
        self,
        name: bytes,
        *,
        parent: tuple[uuid.UUID, str, int] | None,
        by: uuid.UUID | None = None,
        traversal: bool = False,
    ) -> tuple[uuid.UUID, str, int]:
        node_id = uuid.uuid4()
        label = str(node_id).replace("-", "_")
        path = label if parent is None else f"{parent[1]}.{label}"
        depth = 0 if parent is None else parent[2] + 1
        display = name.decode()
        await self.session.execute(
            text(_INSERT_NODE),
            {
                "id": node_id,
                "ino": self.ino,
                "drive": self.drive,
                "org": self.org,
                "parent": None if parent is None else parent[0],
                "kind": "folder",
                "name": name,
                "display": display,
                "key": display.casefold(),
                "path": path,
                "depth": depth,
                "traversal": traversal,
                "by": by,
            },
        )
        self.ino += 1
        return node_id, path, depth


async def _member(session: AsyncSession, org_id: uuid.UUID, email: str) -> uuid.UUID:
    # Raw SQL: the copy sits below revisions that added user columns the current
    # model names (the account lifecycle's deleted_at among them).
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, org_team_id, email, email_domain, first_name, last_name) "
            "VALUES (:id, :org, :email, :domain, 'Seeded', 'Member')"
        ),
        {"id": user_id, "org": org_id, "email": email, "domain": email.rpartition("@")[2]},
    )
    # Raw SQL: the copy sits below the revision that gave team memberships an
    # org column, so the current model's insert would name a column it lacks.
    await session.execute(
        text(
            "INSERT INTO team_memberships (id, user_id, team_id, role) "
            "VALUES (:id, :user_id, :team_id, 'member')"
        ),
        {"id": uuid.uuid4(), "user_id": user_id, "team_id": org_id},
    )
    await session.flush()
    return user_id


@dataclass(frozen=True)
class _Seeded:
    owners: dict[str, uuid.UUID]
    homes: dict[str, uuid.UUID]
    legacy: dict[str, bytes]
    untouched: dict[str, tuple[uuid.UUID, bytes]]


async def _seed(session: AsyncSession, org_id: uuid.UUID, admin_id: uuid.UUID) -> _Seeded:
    """One org drive with a home of every spelling, and three lookalikes."""
    token = uuid.uuid4().hex[:8]
    emails = {
        "alice-x": f"alice{token}@x.com",
        "alice-y": f"alice{token}@y.com",
        "bob": f"bob{token}@x.com",
        "slash": f"a/{token}@x.com",
        "renamed": f"renamed{token}@x.com",
    }
    legacy = {
        "alice-x": f"alice{token}.x.com".encode(),
        "alice-y": f"alice{token}.y.com".encode(),
        "bob": f"bob{token}".encode(),
        "slash": f"a%2F{token}@x.com".encode(),
    }
    owners = {key: await _member(session, org_id, email) for key, email in emails.items()}
    tree = _Tree(session, await _drive(session, org_id), org_id)
    root = await tree.node(b"", parent=None, traversal=True)
    container = await tree.node(b"home", parent=root, traversal=True)
    shared = await tree.node(b"Shared", parent=root)
    # Bob's stray is older than his home: a creator-only match would take it.
    stray = await tree.node(b"Test", parent=container, by=owners["bob"])
    homes = {}
    for key, name in legacy.items():
        homes[key] = (await tree.node(name, parent=container, by=owners[key]))[0]
    renamed = await tree.node(b"Projects", parent=container, by=owners["renamed"])
    elsewhere = await tree.node(legacy["bob"], parent=shared, by=owners["bob"])
    admins = await tree.node(b"not-an-address-of-hers", parent=container, by=admin_id)
    await session.commit()
    return _Seeded(
        owners=owners,
        homes=homes,
        legacy=legacy,
        untouched={
            "bob's stray": (stray[0], b"Test"),
            "a home its owner renamed": (renamed[0], b"Projects"),
            "an address-named folder outside home/": (elsewhere[0], legacy["bob"]),
            "the admin's other folder": (admins[0], b"not-an-address-of-hers"),
        },
    )


async def _row(session: AsyncSession, node_id: uuid.UUID) -> dict[str, Any]:
    found = await session.execute(
        text("SELECT name, name_display, name_key, subtype, etag FROM file_nodes WHERE id = :id"),
        {"id": node_id},
    )
    return dict(found.mappings().one())


def test_the_revision_is_at_or_below_the_schema_the_code_expects() -> None:
    assert _REVISION == "0171"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


async def test_every_home_is_renamed_to_its_owners_id_and_nothing_else_moves() -> None:
    async with migration_scratch() as db:
        org = await seed_org_admin(db)
        await db.downgrade(_PARENT)
        async with db.session() as session:
            seeded = await _seed(session, org.org_id, org.admin_id)

        await db.upgrade()
        async with db.session() as session:
            for key, home_id in seeded.homes.items():
                row = await _row(session, home_id)
                owner = str(seeded.owners[key])
                assert bytes(row["name"]) == owner.encode(), key
                assert row["name_display"] == owner, key
                assert row["name_key"] == owner, key
                assert row["subtype"] == "home", key
                assert row["etag"] == 2, "a renamed home reads as changed to a client"
            for what, (node_id, name) in seeded.untouched.items():
                row = await _row(session, node_id)
                assert bytes(row["name"]) == name, what
                assert row["subtype"] is None, what
                assert row["etag"] == 1, what


async def test_the_downgrade_gives_every_home_its_address_spelling_back() -> None:
    """The previous release finds its homes again: the spelling it would derive
    from each owner's address and the org's roster, and no stamp."""
    async with migration_scratch() as db:
        org = await seed_org_admin(db)
        await db.downgrade(_PARENT)
        async with db.session() as session:
            seeded = await _seed(session, org.org_id, org.admin_id)
        await db.upgrade()

        await db.downgrade(_PARENT)
        async with db.session() as session:
            for key, home_id in seeded.homes.items():
                row = await _row(session, home_id)
                assert bytes(row["name"]) == seeded.legacy[key], key
                assert row["name_display"] == seeded.legacy[key].decode(), key
                assert row["subtype"] is None, key
            for what, (node_id, name) in seeded.untouched.items():
                assert bytes((await _row(session, node_id))["name"]) == name, what

        # And up again: the round trip is stable.
        await db.upgrade()
        async with db.session() as session:
            for key, home_id in seeded.homes.items():
                row = await _row(session, home_id)
                assert bytes(row["name"]) == str(seeded.owners[key]).encode(), key


async def _acl(
    session: AsyncSession, org_id: uuid.UUID, node_id: uuid.UUID, grants: tuple[Grant, ...]
) -> None:
    """Give ``node_id`` an interned ACL holding ``grants``, spelled the way the app interns."""
    body = ace_body(grants)
    acl_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_acls (id, org_team_id, body, body_hash) "
            "VALUES (:id, :org, CAST(:body AS jsonb), :hash)"
        ),
        {
            "id": acl_id,
            "org": org_id,
            "body": json.dumps(body),
            "hash": body_hash(body, org_team_id=org_id),
        },
    )
    await session.execute(
        text("UPDATE file_nodes SET acl_id = :acl WHERE id = :id"), {"acl": acl_id, "id": node_id}
    )


async def test_a_home_whose_owner_changed_address_is_found_by_its_owner_grant() -> None:
    """The customer hand-over: the home was ensured under the first address's
    local part, then the address changed. No spelling of the NEW address names
    the folder, but its own ACL carries the owner's drive-default grant — so it
    is renamed to the id, and the member's next drive read finds that one home
    and nests nothing. A folder of theirs beside it that carries only a direct
    grant, and one that is older than the home, are strays and stay put."""
    async with migration_scratch() as db:
        org = await seed_org_admin(db)
        await db.downgrade(_PARENT)
        token = uuid.uuid4().hex[:8]
        async with db.session() as session:
            member = await _member(session, org.org_id, f"customer{token}@new.test")
            drive_id = await _drive(session, org.org_id)
            tree = _Tree(session, drive_id, org.org_id)
            root = await tree.node(b"", parent=None, traversal=True)
            await session.execute(
                text("UPDATE file_drives SET root_node_id = :root WHERE id = :id"),
                {"root": root[0], "id": drive_id},
            )
            container = await tree.node(b"home", parent=root, traversal=True)
            older = await tree.node(b"Test", parent=container, by=member)
            home = await tree.node(f"robin+tideline{token}".encode(), parent=container, by=member)
            shared = await tree.node(b"Handover", parent=container, by=member)
            await _acl(
                session,
                org.org_id,
                home[0],
                default_acl(
                    DriveFolder.HOME,
                    org_policy=ORG_POLICY_RESTRICTED,
                    org_team_id=org.org_id,
                    subject_id=member,
                ),
            )
            await _acl(
                session,
                org.org_id,
                shared[0],
                (Grant(Principal("user", member), "owner", GrantOrigin.direct()),),
            )
            await session.commit()

        await db.upgrade()
        async with db.session() as session:
            renamed = await _row(session, home[0])
            assert bytes(renamed["name"]) == str(member).encode()
            assert renamed["subtype"] == "home"
            for node_id, name in ((older[0], b"Test"), (shared[0], b"Handover")):
                assert bytes((await _row(session, node_id))["name"]) == name

            repo = FilesRepo(session, OrgScope(org_team_id=org.org_id))
            async with repo.transaction():
                lookup = await drives.lookup_home(repo, member)
            assert lookup.home is not None and lookup.home.id == home[0]
            assert sorted(row.id for row in lookup.strays) == sorted([older[0], shared[0]])
            ctx = ActingContext.for_user(user_id=member, org_id=org.org_id, email="x@new.test")
            async with repo.transaction():
                again = await drives.ensure_home_folder(repo, ctx, member)
            assert again.id == home[0], "the next visit ensures no second home to nest the first in"
