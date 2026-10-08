# Cross-seam fixtures

These are not lineage fixtures: they carry no schema version, and the lineage walker ignores them (it reads only `v*/specs/`). Each file is one side of a producer and consumer seam, committed so both sides assert against the same bytes.

| File | Written by | Read by |
| --- | --- | --- |
| `query_spec_from_browser.json` | the saved query the browser's builder emits (`querySpecOf` in `apps/web/src/pages/workspace/chat/saveResult.ts`) | the re-run route and the machine, in `apps/cli/tests/cloud/test_cloud_round3_seams.py` and `test_cloud_round3c_seams.py` |
| `chart_spec_from_browser.json` | `timeSeriesSpec` in `apps/web/src/pages/workspace/chat/chartDerive.ts`, pinned in `apps/web/src/tests/pages/workspace/chat/chartSpecSeam.test.tsx` | `alkera_core.schemas.objects.validate_chart_spec`, in `packages/api-core/tests/schemas/objects/test_chart_spec_browser_seam.py` |
| `chart_spec_persisted.json` | `validate_chart_spec(...).persisted()`, pinned in `test_chart_spec_browser_seam.py` | `readTimeSeriesSpec` in `apps/web/src/pages/workspace/objects/chartSpec.ts`, in `chartSpecSeam.test.tsx` |
| `query_slots.json` | the slot grammar both sides read | `apps/web/src/tests/lib/querySlots.test.ts` and `packages/api-core/tests/schemas/objects/test_query_slot_grammar.py` |
| `chat_messages_page_from_server.json` | `GET /api/v1/chats/{id}/messages` after a real turn, recorded by `test_cloud_round3_seams.py` with `SEAM_FIXTURE_WRITE=1`, which also pins its shape | `CloudDataSource.getChatTurns`, in `apps/web/src/tests/pages/workspace/chat/data/CloudDataSource.serverPage.test.ts` |
| `refusal_note_entries.json` | the three entries a `ChatMirror` publishes for a refusal (`alkera_cli/cloud/refusal.py`), recorded by `test_cloud_round3_seams.py` with `SEAM_FIXTURE_WRITE=1` | the system notice in `harnessEventFold`, in `apps/web/src/tests/pages/workspace/chat/data/refusalNotice.test.ts` (which also reads the sentence from `refusal.py`) |
| `receipt_from_routes.json` | the receipt a promote and an upload leave on an object (`POST /chats/{id}/promote`, then `POST /objects/{id}/payload`), recorded by `apps/backend/tests/test_daemon_rest_seam.py` with `SEAM_FIXTURE_WRITE=1` | the receipt panel, in `apps/web/src/tests/pages/workspace/objects/receiptPanel.test.tsx` |
| `tier2_chain_from_routes.json` | one real run of a saved query re-run through `POST /objects/{id}/rerun`, then promoted through `POST /chats/{id}/promote` and `POST /objects/{id}/payload`, recorded at its browser-facing ends (the transcript entry, the stored query, the promoted object, its rows page, the CSV head) by `apps/cli/tests/cloud/test_cloud_round3c_seams.py` with `SEAM_FIXTURE_WRITE=1` | the SQL card, the object page and the query page, in `apps/web/src/tests/pages/workspace/objects/tier2ChainFromRoutes.test.tsx` |

Without a shared fixture, each side's suite hand-builds the other side's payload and stays green while the two disagree. A browser that sent `{sql, parameters, connection}` to a spec declaring `{sql_template, params, connection_id, source_chat_id}` would store an empty query, because `VersionedModel` accepts extra fields.

Changing a shape here means changing the tests on both sides.
