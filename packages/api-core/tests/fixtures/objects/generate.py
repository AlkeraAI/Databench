"""Regenerate the workspace-object lineage fixtures at the CURRENT writer version.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/objects/generate.py

Writes one dump per persisted model into
``packages/api-core/tests/fixtures/objects/v<SCHEMA_VERSION>/specs/``. Commit the diff alongside
the schema change that prompted the regeneration.

NEVER edit an old fixture by hand. Migrations go in the model's ``MIGRATIONS``
dict; fixtures stay frozen as the evidence of what a past writer emitted.
"""

from __future__ import annotations

import json
from pathlib import Path

from alkera_core.schemas.objects import (
    AnswerRelay,
    BlobHandle,
    ChatModelPin,
    ChatPromptRecord,
    ChatSpec,
    ChatTemplateSpec,
    ChatTranscriptEntry,
    ModelRelay,
    ModeRelay,
    PromoteRelay,
    PromptRelay,
    QueryParam,
    QuerySpec,
    RawRelay,
    Receipt,
    ReportConnection,
    ReportRendering,
    ReportSpec,
    ReportStep,
    ResultBlobEnvelope,
    ResultColumn,
    ResultSpec,
    RunQueryRelay,
    StopRelay,
    WorkspaceSpec,
)
from alkera_core.versioning import VersionedModel, corpus_version, resolve_corpus_dir

# Fixed values so a regenerate is byte-identical until a shape changes.
CHAT_ID = "5f1f0a3c-2f0e-4a2b-9d5c-71a3c9f0b111"
MACHINE_ID = "6a2c1b4d-3e1f-4b3c-8e6d-82b4d0a1c222"
QUERY_ID = "7b3d2c5e-4f20-4c4d-9f7e-93c5e1b2d333"
CONNECTION_ID = "8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444"
USER_ID = "00000000-0000-4000-8000-000000000001"
EXECUTED_AT = "2026-09-07T12:00:00+00:00"
EVENT_ID = "9d4e3f60-6142-4e5f-b190-b5e703d4f555"
MESSAGE_ID = "ae5f4071-7253-4f60-a2a1-c6f814e5f666"
CLIENT_ID = "bf605182-8364-4071-b3b2-d70925f60777"
RUN_ID = "c0716293-9475-4182-84c3-e81a36071888"
WORKSPACE_ID = "d1827304-a586-4293-95d4-f92b47182999"
ORG_MACHINE_ID = "e2938415-b697-43a4-a6e5-0a3c58293aaa"

#: The model a picked chat is pinned to, as the gateway catalog described it.
MODEL_PIN = ChatModelPin(
    id="claude-opus-4.5",
    display_name="Claude Opus 4.5",
    wire="anthropic",
    efforts=["low", "medium", "high"],
    effort="high",
    context_window=200_000,
    max_output_tokens=64_000,
    reasoning_format="anthropic:claude-opus-4-5",
    reads_reasoning_formats=["anthropic:claude-sonnet-4-5"],
)


def specs() -> dict[str, VersionedModel]:
    """One instance of every model persisted in a ``workspace_objects.spec``."""
    receipt = Receipt(
        sql="SELECT day, orders FROM daily WHERE customer = %(customer)s",
        connection_id=CONNECTION_ID,
        connection_name="Tideline Postgres",
        role="analytics_readonly",
        engine="postgres",
        principal_chain={
            "schema_version": "1.0.0",
            "acting": {"kind": "agent", "id": "sess-01J7Q3M8", "label": "sess-01J7Q3M8"},
            "chain": [
                {"kind": "user", "id": USER_ID, "label": "ops@example.com"},
                {"kind": "agent", "id": "sess-01J7Q3M8", "label": "sess-01J7Q3M8"},
            ],
        },
        executed_at=EXECUTED_AT,
        row_count=3,
        duration_ms=412,
        params={"customer": "acme"},
        definitions_referenced=["daily_orders"],
    )
    return {
        "chat": ChatSpec(machine_id=MACHINE_ID, machine_status="ready", last_seq=17),
        "chat_unbound": ChatSpec(),
        # A chat whose running turn the server ended when its machine was
        # stopped for credit.
        "chat_turn_ended": ChatSpec(
            machine_id=MACHINE_ID,
            mirror_state="asleep",
            end_seq=1,
            ended_reason="credits_exhausted",
            turn_end_reason="credits_exhausted",
            turn_end_at="2026-10-06T19:00:00+00:00",
        ),
        # A chat the reader picked a model for, in a stance they chose. The
        # pinned pair is what the BOX reads to open the session, so a reader
        # that can no longer load it would open the chat on the wrong model.
        "chat_pinned": ChatSpec(
            machine_id=MACHINE_ID,
            machine_status="ready",
            last_seq=4,
            model=MODEL_PIN,
            permission_mode="default",
        ),
        # A chat in a workspace: the reference every chat carries since
        # workspaces, which a box on an older build ignores.
        "chat_in_workspace": ChatSpec(
            machine_id=MACHINE_ID, machine_status="ready", last_seq=2, workspace_id=WORKSPACE_ID
        ),
        # A member's main workspace: native, its own folder, binding derived.
        "workspace_main": WorkspaceSpec(kind="main", layout="native"),
        # The workspace of one every existing chat was adopted into.
        "workspace_adopted": WorkspaceSpec(
            kind="project", layout="adopted", adopted_chat_id=CHAT_ID
        ),
        # What a box that runs workspaces reported: the sandbox is up, and
        # what it holds in memory.
        "workspace_reported": WorkspaceSpec(
            kind="project",
            layout="native",
            binding_authority="workspace",
            machine_id=MACHINE_ID,
            sandbox_state="awake",
            sandbox_memory_used_mb=1536,
            sandbox_reported_at=EXECUTED_AT,
        ),
        # A workspace pinned to one of its org's machines by a move.
        "workspace_pinned": WorkspaceSpec(
            kind="project", layout="native", machine_pin=ORG_MACHINE_ID
        ),
        # A workspace whose machine was deleted, moved to the default placement
        # by a waker that is not a person.
        "workspace_fell_back": WorkspaceSpec(
            kind="project",
            layout="native",
            lost_machine_id=ORG_MACHINE_ID,
            fell_back_at=EXECUTED_AT,
        ),
        # A template saved with nothing chosen: every field at its default, so
        # the corpus holds the row a reader gets when the writer said nothing.
        "chat_template": ChatTemplateSpec(),
        # And one saved out of a real chat: a brief, the model that chat ran
        # on, and the point in the transcript it was saved at.
        "chat_template_pinned": ChatTemplateSpec(
            brief=(
                "Pull the weekly mentions for the region the reader names, chart them, "
                "and write the two paragraphs of commentary underneath."
            ),
            model=MODEL_PIN,
            permission_mode="plan",
            source_chat_id=CHAT_ID,
            saved_from_seq=17,
        ),
        "model_pin": MODEL_PIN,
        "query_param_enum": QueryParam(
            name="region",
            type="enum",
            label="Region",
            enum_values=["emea", "amer"],
            prompt="Which region should this cover?",
        ),
        "query_param_daterange": QueryParam(
            name="period", type="daterange", label="Period", required=False
        ),
        "query_param_number": QueryParam(
            name="threshold", type="number", label="Threshold", required=False
        ),
        "query_param_datetime": QueryParam(
            name="observed_at", type="datetime", label="Observed at", required=False
        ),
        "query_sqlite": QuerySpec(
            sql_template="SELECT * FROM prompts WHERE customer = {customer}",
            params=[QueryParam(name="customer", type="string", label="Customer")],
            engine="sqlite",
        ),
        "query": QuerySpec(
            sql_template=(
                "SELECT day, count(*) FROM prompts "
                "WHERE customer = {customer} AND day BETWEEN {period.start} AND {period.end}"
            ),
            params=[
                QueryParam(name="customer", type="string", label="Customer"),
                QueryParam(name="period", type="daterange", label="Period"),
            ],
            connection_id=CONNECTION_ID,
            engine="postgres",
            source_chat_id=CHAT_ID,
        ),
        "query_typed_slots": QuerySpec(
            # The spelling a real agent writes for Tinybird: the type is in the
            # statement, and the values it was saved with open the re-run form.
            sql_template=(
                "SELECT toStartOfWeek(run_day, 1) AS week_start, count() AS n FROM mentions "
                "WHERE customer_id = {{String(customer_id)}} "
                "AND run_day >= {{Date(start_date)}} AND run_day <= {{Date(end_date)}} "
                "GROUP BY week_start ORDER BY week_start"
            ),
            params=[
                QueryParam(name="customer_id", type="string", label="Customer id"),
                QueryParam(name="start_date", type="date", label="Start date"),
                QueryParam(name="end_date", type="date", label="End date"),
            ],
            defaults={
                "customer_id": "Enterprise",
                "start_date": "2026-08-01",
                "end_date": "2026-09-06",
            },
            connection_id=CONNECTION_ID,
            engine="tinybird",
            source_chat_id=CHAT_ID,
        ),
        "report_connection": ReportConnection(plugin="postgres", handle="tideline-warehouse"),
        "report_step": ReportStep(
            kind="query",
            text="SELECT week_start, mentions FROM weekly WHERE region = {region}",
            connection="tideline-warehouse",
        ),
        "report_rendering": ReportRendering(
            format="pdf", template="Summary, then one section per region, then the methodology."
        ),
        "report": ReportSpec(
            title="Weekly mentions",
            questions=[
                QueryParam(
                    name="region",
                    type="enum",
                    label="Region",
                    enum_values=["emea", "amer"],
                    prompt="Which region should this report cover?",
                ),
                QueryParam(
                    name="period",
                    type="daterange",
                    label="Period",
                    prompt="Which date range should the report cover?",
                ),
            ],
            connections=[ReportConnection(plugin="postgres", handle="tideline-warehouse")],
            steps=[
                ReportStep(
                    kind="query",
                    text=(
                        "SELECT week_start, count(*) AS mentions FROM mentions "
                        "WHERE region = {region} "
                        "AND run_day BETWEEN {period.start} AND {period.end} "
                        "GROUP BY week_start ORDER BY week_start"
                    ),
                    connection="tideline-warehouse",
                ),
                ReportStep(kind="render", text="One line chart, then the table beneath it."),
            ],
            narrative="What moved this period, and what the movement is attributable to.",
            rendering=ReportRendering(
                format="pdf",
                template="Summary, then one section per region, then the methodology.",
            ),
            source_chat_id=CHAT_ID,
        ),
        "result_column": ResultColumn(name="day", label="Day"),
        "blob_handle": BlobHandle(sha256="c" * 64, size=4096),
        "receipt": receipt,
        "result_inline": ResultSpec(
            source_query_id=QUERY_ID,
            columns=[ResultColumn(name="day", label="Day"), ResultColumn(name="orders")],
            receipt=receipt,
            chart_spec={
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "mark": "line",
                "encoding": {
                    "x": {"field": "day", "type": "temporal", "timeUnit": "yearmonthdate"},
                    "y": {"field": "orders", "type": "quantitative", "aggregate": "sum"},
                },
            },
            payload=BlobHandle(sha256="a" * 64, size=128),
            inline={
                "schema_version": "1.0.0",
                "kind": "rows",
                "columns": ["day", "orders"],
                "rows": [["2026-09-01", 3], ["2026-09-02", 5]],
                "text": "",
                "total": 2,
            },
            total_rows=2,
        ),
        "result_spilled": ResultSpec(
            columns=[ResultColumn(name="day")],
            receipt=receipt,
            payload=BlobHandle(sha256="b" * 64, size=2_000_000),
            inline=None,
            total_rows=50_000,
        ),
        "envelope_rows": ResultBlobEnvelope(
            kind="rows",
            columns=["day", "orders"],
            rows=[["2026-09-01", 3], ["2026-09-02", 5]],
            total=2,
        ),
        "envelope_text": ResultBlobEnvelope(kind="text", text="no rows", total=7),
    }


def transcript() -> dict[str, VersionedModel]:
    """One instance of every shape written to ``chat_messages.payload`` or
    carried as a relay body on the chat document — the other side of the same
    boundary, persisted in a different column."""
    return {
        "transcript_entry": ChatTranscriptEntry(
            event_id=EVENT_ID,
            role="assistant",
            kind="message.created",
            payload={
                "schema_version": "1.0.0",
                "event_type": "message.created",
                "event_id": EVENT_ID,
                "session_id": "sess-01J7Q3M8",
                "time": EXECUTED_AT,
                "message_id": MESSAGE_ID,
                "role": "assistant",
            },
        ),
        # The same entry as a READER is told it: the sequence the durable write
        # assigned, which the document's state and the append's rebroadcast
        # both carry and which a stored row leaves to its own column.
        "transcript_entry_stamped": ChatTranscriptEntry(
            event_id=EVENT_ID,
            role="assistant",
            kind="message.created",
            seq=17,
            payload={
                "schema_version": "1.0.0",
                "event_type": "message.created",
                "event_id": EVENT_ID,
                "session_id": "sess-01J7Q3M8",
                "time": EXECUTED_AT,
                "message_id": MESSAGE_ID,
                "role": "assistant",
            },
        ),
        "prompt_record": ChatPromptRecord(
            text="which prompts moved this week?", client_id=CLIENT_ID, user_id=USER_ID
        ),
        "relay_prompt": PromptRelay(
            message_id=MESSAGE_ID,
            seq=17,
            text="which prompts moved this week?",
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ),
        "relay_answer": AnswerRelay(
            interrupt_id="ask-01J7Q3M8", user_id=USER_ID, option_id="allow_once"
        ),
        "relay_answer_note": AnswerRelay(
            interrupt_id="ask-01J7Q3M9",
            user_id=USER_ID,
            answers=[["Accept — run normally (ask before each change)"]],
            note="Skip the migration for now.",
        ),
        "relay_run_query": RunQueryRelay(
            object_id=QUERY_ID, params={"customer": "acme"}, run_id=RUN_ID, user_id=USER_ID
        ),
        "relay_promote": PromoteRelay(object_id=QUERY_ID, event_id=EVENT_ID),
        "relay_mode": ModeRelay(mode="default", user_id=USER_ID),
        "relay_model": ModelRelay(
            pin=MODEL_PIN.model_dump(mode="json"),
            user_id=USER_ID,
            previous_model_id="gpt-5.2",
            decided_via="web",
            ledger=["anthropic:claude-sonnet-4-5"],
        ),
        "relay_stop": StopRelay(user_id=USER_ID),
        "relay_unknown": RawRelay(kind="a_kind_this_reader_does_not_know"),
    }


def corpus() -> dict[str, dict[str, VersionedModel]]:
    return {"specs": specs(), "transcript": transcript()}


def _corpus_version() -> str:
    """The corpus is stamped with the MAX ``SCHEMA_VERSION`` across the models
    it holds — the floor the corpus can carry. A regenerate whose output
    differs from what is committed at that stamp lands in a NEW directory
    (``resolve_corpus_dir``); a past writer's evidence is never rewritten."""
    return corpus_version([model for group in corpus().values() for model in group.values()])


def regenerate(root: Path | None = None) -> Path:
    """Write every fixture under ``<root>/v<X_Y_Z>/<group>/<name>.json``;
    returns the version directory."""
    root = root or Path(__file__).parent
    payload = {
        f"{group}/{name}.json": (
            json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        ).encode()
        for group, models in corpus().items()
        for name, model in models.items()
    }
    version_dir = resolve_corpus_dir(root, _corpus_version(), payload)
    for relative, blob in payload.items():
        target = version_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return version_dir


if __name__ == "__main__":
    print(f"wrote fixtures under {regenerate()}")
