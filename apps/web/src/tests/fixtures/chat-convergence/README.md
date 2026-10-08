# Chat convergence corpus

Server event logs of real chats, scrubbed of their text. They drive `src/tests/pages/workspace/chat/data/convergence.corpus.test.ts` through the checks in `src/pages/workspace/chat/data/convergence.ts`.

The test asks one question of every chat: does a tab that opened partway through and then followed the live stream show the same thing as a tab that opens fresh later, whichever of the REST page and the socket snapshot arrives first? Both must also match the reference, the whole log read in one page. Each difference the test finds is sorted into a named class with its cause, and the classes each chat shows are pinned, so a new kind of difference fails the test and so does a fix that closes a class until its entry is removed.

## Files

One folder per chat, named by the chat id.

- `log.json` is `{chat_id, rows}`. Each row is a `ChatMessageRead` (`id, seq, role, kind, event_id, payload, created_at`) exactly as `GET /api/v1/chats/{id}/messages` returns it, ordered by `seq`. This is the server's ordered event log, the source every view is derived from.
- `doc.json` has the shape of the chat's realtime document row: the `turn_state` and `turn_state_at` columns, the merged `meta`, and the bounds of the retained window. `window.first_seq` is where the socket snapshot starts.
- `rest.json`, where present, holds the chat row and the shape of the last page the app opens on. `tail.first_seq` is where the REST page starts.

A chat without a `doc.json` has a window covering the whole log, which is what `OpenOptions.windowFrom` defaults to.

## Scrubbing

Structure and ids are kept; free text is not. `text`, `content`, `output`, `patterns`, `preview`, `title`, `command`, `reason` and every other free-text field is replaced by `[scrubbed N]`, where N is the original length, so code that branches on whether there is text takes the same branch. Ids, event types, statuses, timestamps, workspace paths and URLs (without query strings) are kept, and email addresses are replaced. Add or refresh a chat with the same rules rather than editing a file by hand.

## What the corpus covers

- **An ask announced again after it was resolved.** A permission request is resolved, then the same request is published again with `prompting: true` inside the next turn. A tab whose REST page arrives first can show an answerable ask that the socket snapshot, which includes the resolution, does not. Answering it returns 409 `ask_already_answered`.
- **A knowledge-sharing ask with no tool call** (`permission_kind: knowledge`), published only as its prompting copy, so the treatment of an ask without a tool call is covered.
- **Stops that land around dispatch.** A stop pressed just before or after a message is dispatched, including one where the turn kept streaming until the adapter's cancel returned.
- **Long turns and compaction.** Turns longer than one page, and turns at the compaction threshold.
- **Negative cases.** Chats where every sampled pair agrees, kept so the check stays green on logs that should converge.

## Adding a chat

Turn the convergence detector on in a development build (`localStorage.setItem("alkera.chatConvergenceCheck", "1")`, see `src/pages/workspace/chat/data/convergenceDetector.ts`), export `localStorage.getItem("alkera.chatConvergence.<chatId>")`, scrub it as above, add its folder here, and list it in the test.
