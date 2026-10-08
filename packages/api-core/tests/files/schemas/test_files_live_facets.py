"""The in-flight plane's wire shapes: what a node is doing on the box RIGHT NOW.

A leased folder's bytes land in the drive on a checkpoint, so between two
checkpoints the drive's copy is the PREVIOUS one and a reader is looking at
stale content. These facets are how the difference is told: the lease facet
grows the four counters that say the lease is streaming, and a live facet is
attached to exactly those rows a holder has reported an in-flight state for.

The cases below pin the additive contract (a reader of the older facet keeps
working), the state vocabulary (the wire Literal and the column's tuple are one
vocabulary, refusing anything outside it) and the purge order that keeps a live
entry from outliving the lease it hangs off.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alkera_core.config import Settings
from alkera_core.files.trash import PURGE_TABLES
from alkera_core.models.files.leases import LIVE_ENTRY_STATES
from alkera_core.schemas.files.item import Item, LeaseFacet, LiveFacet
from alkera_core.schemas.files.lease import LIVE_ENTRY_STATE_VALUES
from pydantic import ValidationError

_T = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: What a writer that shipped before the live plane wrote. Kept as a literal
#: rather than built from the model, so a field quietly becoming required is a
#: failure here rather than a silently-adjusted expectation.
_LEASE_FACET_1_0_0 = {
    "schema_version": "1.0.0",
    "holder": "6b1f0b1e-0000-4000-8000-000000000002",
    "machine": "machine-a",
    "purpose": "mount",
    "since": "2026-01-01T12:00:00Z",
    "expires_at": None,
    "last_sync_at": None,
    "mine": True,
    "metadata": {},
}


def test_the_older_lease_facet_still_reads_and_comes_back_stamped_current() -> None:
    """The four counters are additive: a payload written before they existed
    loads, answers the quiet defaults, and is re-serialised at today's version
    so a stale stamp is never written back."""
    facet = LeaseFacet.model_validate(_LEASE_FACET_1_0_0)
    assert facet.holder == "6b1f0b1e-0000-4000-8000-000000000002"
    assert facet.mine is True
    assert (facet.live, facet.inbound, facet.pending, facet.live_seq) == (False, False, 0, 0)
    # The names are additive too, and a writer that predates them says nothing
    # rather than falling back to the ids they exist to replace.
    assert (facet.machine_name, facet.holder_name) == ("", "")
    # So are the serving state, the landing count and the lease's own node: a
    # payload that predates them reads as a folder nobody is serving, with
    # nothing on its way, whose lease the reader cannot match a frame to.
    assert (facet.served, facet.landing_count, facet.node_id) == ("offline", 0, None)
    # And whether the holder acts for the reader: an older payload says it does
    # not, so a refusal falls back to naming the holder rather than "you".
    assert facet.yours == "none"
    # And the server's status: an older payload carries none, and a reader
    # draws no status rather than inventing one from the raw fields.
    assert facet.status is None
    assert facet.model_dump(mode="json")["schema_version"] == LeaseFacet.SCHEMA_VERSION
    assert LeaseFacet.SCHEMA_VERSION == "1.6.0"


def test_the_resolved_names_ride_beside_the_ids_they_replace() -> None:
    """A box holds its own lease, so the raw facet names the machine and the
    holder with one uuid. The names are a separate pair of fields: the ids the
    server fences on are unchanged, and a reader that has a name knows it has
    one rather than guessing at the string in ``holder``."""
    machine_id = "807bf89a-464c-4867-8b08-6e020a9bd8a3"
    facet = LeaseFacet(holder=machine_id, machine=machine_id, machine_name="alkera-demo-box")
    dumped = facet.model_dump(mode="json")
    assert dumped["machine"] == machine_id and dumped["holder"] == machine_id
    assert dumped["machine_name"] == "alkera-demo-box"
    assert dumped["holder_name"] == ""


def test_a_streaming_lease_reports_its_counters() -> None:
    facet = LeaseFacet(live=True, inbound=True, pending=3, live_seq=91)
    dumped = facet.model_dump(mode="json")
    assert dumped["live"] is True and dumped["inbound"] is True
    assert dumped["pending"] == 3 and dumped["live_seq"] == 91


@pytest.mark.parametrize("state", LIVE_ENTRY_STATE_VALUES)
def test_every_state_the_column_admits_round_trips_on_the_wire(state: str) -> None:
    facet = LiveFacet(state=state, box_size=4096, box_mtime=_T, updated_at=_T)
    reread = LiveFacet.model_validate(facet.model_dump(mode="json"))
    assert reread.state == state
    assert reread.box_size == 4096
    assert reread.box_mtime == _T


@pytest.mark.parametrize(
    "state",
    ["", "WRITING", "written", "on-box", "inbound_move", "deleted"],
    ids=["empty", "shouted", "near-miss", "hyphenated", "invented", "unrelated"],
)
def test_a_state_outside_the_vocabulary_is_refused(state: str) -> None:
    """The Literal is the guard, not decoration: a holder cannot invent a state
    the column's CHECK would then reject at the database."""
    with pytest.raises(ValidationError):
        LiveFacet(state=state)


def test_the_column_and_the_wire_name_the_same_states_in_the_same_order() -> None:
    """Two spellings — the tuple the CHECK constraint is built from and the
    Literal the payload is validated against — are one vocabulary. Editing
    either alone would let a row exist that no client can read, or a payload
    validate that no row can hold."""
    assert LIVE_ENTRY_STATES == LIVE_ENTRY_STATE_VALUES
    assert LIVE_ENTRY_STATES == (
        "writing",
        "uploading",
        "on_box",
        "deferred",
        "inbound",
        "inbound_delete",
        "inbound_rename",
    )


def test_an_item_carries_the_live_facet_beside_its_lease_and_omits_it_otherwise() -> None:
    plain = Item(id="6b1f0b1e-0000-4000-8000-000000000001")
    assert plain.live is None
    live = Item(
        id="6b1f0b1e-0000-4000-8000-000000000001",
        lease=LeaseFacet(live=True, pending=1, live_seq=4),
        live=LiveFacet(state="uploading", box_size=17, updated_at=_T),
    )
    payload = live.model_dump(mode="json", by_alias=True)
    # The item camelCases its own keys; a facet is a versioned document and
    # keeps the field names it is persisted under.
    assert payload["live"]["state"] == "uploading"
    assert payload["live"]["box_size"] == 17
    assert payload["lease"]["live_seq"] == 4


def test_a_live_entry_is_purged_before_the_lease_it_hangs_off() -> None:
    """The entry's foreign key points at ``file_leases.node_id``; deleting the
    lease first would leave the purge deleting a row that a cascade has already
    taken, or — on a cascade that is ever dropped — a key violation."""
    assert "file_lease_live_entries" in PURGE_TABLES
    assert PURGE_TABLES.index("file_lease_live_entries") < PURGE_TABLES.index("file_leases")


def test_the_live_cadence_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The served cadence is an operator's dial, not a constant compiled in."""
    monkeypatch.setenv("FILES_LIVE_DEBOUNCE_MS", "75")
    monkeypatch.setenv("FILES_LIVE_MAX_BATCH_ENTRIES", "8")
    tuned = Settings(_env_file=None)  # type: ignore[call-arg]
    assert tuned.files_live_debounce_ms == 75
    assert tuned.files_live_max_batch_entries == 8


def test_one_batch_can_never_exceed_the_entries_a_lease_may_hold_in_flight() -> None:
    """A batch bigger than the ceiling would be refused in full on arrival, so
    the holder could never make progress at the defaults it is handed."""
    shipped = Settings(_env_file=None)  # type: ignore[call-arg]
    assert 0 < shipped.files_live_max_batch_entries <= shipped.files_live_max_pending_entries
    assert shipped.files_live_debounce_ms <= shipped.files_live_batch_ms
    assert shipped.files_live_max_file_bytes <= shipped.files_live_bandwidth_bytes_per_minute


#: A live facet as the writer before the holder's report wrote it.
_LIVE_FACET_1_0_0 = {
    "schema_version": "1.0.0",
    "state": "uploading",
    "box_size": 17,
    "box_mtime": None,
    "updated_at": None,
    "metadata": {},
}


def test_the_older_live_facet_reads_as_a_node_with_no_report() -> None:
    facet = LiveFacet.model_validate(_LIVE_FACET_1_0_0)
    assert (facet.state, facet.box_size) == ("uploading", 17)
    assert (facet.content, facet.holder_size, facet.holder_mtime) == ("none", None, None)
    assert facet.model_dump(mode="json")["schema_version"] == LiveFacet.SCHEMA_VERSION == "1.1.0"


def test_a_reported_node_that_is_not_moving_carries_no_state() -> None:
    """``writing`` was the old default; a file the holder merely reported must
    not read as being written."""
    assert LiveFacet(content="unlanded", holder_size=3).state is None


@pytest.mark.parametrize(
    "content",
    ["", "ON_DRIVE", "landed", "pending"],
    ids=["empty", "shouted", "invented", "a-state-word"],
)
def test_a_content_word_outside_the_vocabulary_is_refused(content: str) -> None:
    with pytest.raises(ValidationError):
        LiveFacet(content=content)


def test_the_state_a_row_was_rendered_with_survives_a_copy_and_never_reaches_the_wire() -> None:
    """A feed renders a row before it learns the lease; the report's state
    before the lease is consulted rides the facet to that later step, and no
    further."""
    facet = LiveFacet(content="unlanded", holder_size=3)
    facet._landed = "unlanded"
    assert facet.model_copy()._landed == "unlanded"
    assert "landed" not in facet.model_dump(mode="json")
    assert "_landed" not in facet.model_dump_json()
