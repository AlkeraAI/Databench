# The realtime wire contract

The realtime wire contract sits beside the OpenAPI schema and the daemon
JSON-RPC protocol, and is maintained by hand. OpenAPI can describe the JSON bodies (and does: the models
in this package are exposed through the routes), but not the event-stream text
lines, the socket handshake, the close codes or the resume rules. Those live
here. Every model in this package is pinned by
`packages/api-core/tests/schemas/realtime/`; the envelope and the frames are
persisted shapes (`VersionedModel`) with a fixture corpus under
`packages/api-core/tests/fixtures/realtime/`.

## 1. The event stream (`GET /api/v1/events`)

Server-sent invalidation events. A frame says **what** changed, never the
change: the client refetches through the REST surface it is authorized for.

| Step | Wire |
| --- | --- |
| Authentication | session cookie or `Authorization: Bearer`. Never `?token=`. |
| Capacity | `429` + `Retry-After: 5` past the per-user / per-process cap. |
| Not running | `503` + `Retry-After: 5` in a process whose lifespan did not start the runtime. |
| Cursor | `Last-Event-ID` header wins; else `?after=<outbox id>`. Non-numeric / negative values are ignored (no replay). |
| Open | `retry: 2000` then `: connected`, then the replay of every row after the cursor that this user may see. |
| Event | `id: <outbox id>` / `event: <RealtimeEventType>` / `data: {"type","entity","entity_id","version","org_id"}` (`SseEventData`). |
| Reset | `event: reset` / `data: {"reason": "overflow" \| "cursor_ahead" \| "cursor_too_old"}`, with no `id:`; the cursor is untouched; refetch everything shown. |
| Straggler | a row that committed after a higher id was already framed is framed late, with its own `id:`; an `id:`-only frame (no `data:`, nothing dispatched) follows it carrying the cursor the client held, so the resume cursor never moves backwards. Each id is framed at most once per stream. |
| Keepalive | `: keepalive` every `REALTIME_SSE_KEEPALIVE_SECONDS` (15); the server re-checks the session **without** sliding the idle window and refreshes the entitlement snapshot. |
| Session ended | `event: error` / `data: {"code": "unauthorized"}`, then close. The reconnect is refused with 401. |
| Deadline | after `REALTIME_SSE_MAX_STREAM_SECONDS` (50 min): `retry: 1000` then close; the browser resumes with its cursor. |

Filtering per frame: the row's `org_id` must equal the user's org; visibility
`org` reaches every member, `platform` only platform staff, `user:<uuid>` only
that user; a row with `payload.team_id` additionally needs a membership on that
team (the chain is materialized, so a sub-team member holds the ancestors) or
org admin. `doc.op` and `authz.decision` rows never reach the stream.

Known limit: a row that commits with a **lower** id than one a client already
saw, while that client is disconnected, is not replayed by the cursor read:
a resume cannot recover a straggler older than the cursor, and the listener's
own straggler window is bounded. Connected clients receive it (the row above).
At-least-once holds for everything else.

## 2. The socket (`WS /api/v1/ws`)

### Handshake

1. `POST /api/v1/ws/tickets` (cookie or Bearer; rate-limited) →
   `WsTicketResponse{ticket, expires_in, path}`. The ticket is a 30-second,
   single-use HS256 JWT bound to the minting session's `jti`.
2. Connect to `path` offering **two** subprotocols, in this order:
   `alkera-v1` and `alkera-ticket.<ticket>`. The ticket therefore travels in the
   `Sec-WebSocket-Protocol` header and never in a URL. The server selects
   `alkera-v1`. **No `?ticket=` or `?token=` is accepted anywhere.**
3. Before accepting, the server checks the `Origin` header against the
   configured CORS origins and the frontend base URL (a missing `Origin`, from a
   non-browser client, is allowed), decodes the ticket, burns it (a replay,
   on any replica, is refused), re-checks the session it was minted from, and
   takes a capacity slot.
4. First frame from the server:
   `welcome{peer_id, server_time, instance, min_client_generation, limits}`.
   `min_client_generation` is the oldest client build the server serves; a tab
   compiled below it reloads into the current build at its next quiet moment
   (`apps/web/src/api/realtime/clientGeneration.ts`). `limits` is the inbound
   budget the socket is held to (`frames_per_window`, `bytes_per_window`,
   `window_seconds`, `max_frame_bytes`), so a client paces itself under it, plus
   the two sizes a publisher sizes its traffic by (`ephemeral_max_bytes`, the
   notify cap a streamed chunk must fit, and `doc_max_bytes`, the largest
   snapshot a hello can be answered with). The box reads both and falls back to
   its own defaults when a server sends neither. The
   `peer_id` is minted by the server and is the id the client puts on every
   envelope it sends. It is not trusted on the way in: the server replaces the
   `peer_id` of every inbound envelope with the socket's own before anything
   reads it (the last-writer-wins tie-break, the rebroadcast, the event-log
   record, the echo suppression), so a client can neither speak as another
   peer nor keep a delivery from it.

`GET /api/v1/ws/protocol` → `RealtimeProtocolDescriptor` (envelope kinds, doc
types, intents, close codes, path, subprotocol names, channel pattern) so a
client can pin itself against the server's vocabulary.

### Frames (discriminated on `t`)

Client → server: `subscribe{channel}`, `unsubscribe{channel}`,
`presence.join{channel}`, `presence.leave{channel}`,
`presence.heartbeat{channel}`, `presence.cursor{channel, cursor}` (where this
peer's caret is in the shared composer draft, `{offset, anchor, before, after}`;
answered to a joined peer only, fanned out as a `presence` delta, never stored),
`ping`, `doc{envelope}`.

Server → client: `welcome{peer_id, server_time, instance, min_client_generation, limits}`,
`subscribed{channel, can_write}`, `presence{channel, event: join|leave|heartbeat|roster|cursor, peers[]}`
(a peer carries `cursor` on a `cursor` delta only),
`reset{reason}`, `error{code, message, channel?}`, `pong`, `doc{envelope}`,
`publisher{channel, state: here|gone, at}` (to a reader of a chat, never to the
box: the box publishing it came onto the channel, or its socket went away; a
drain sends no `gone`, since the box reconnects to another replica).

An unknown `t` is answered with `error{unknown_frame}` and the socket stays
open. A frame over `MAX_FRAME_BYTES` closes the socket with `4413`; more than
200 frames in 10 seconds closes it with `4429`. The backend is launched with
uvicorn's `--ws-max-size` set to that same constant, so
in a deployed process the transport refuses a larger message with the
protocol close `1009` before the gateway reads it, and never buffers more than
the cap; the gateway's own `4413` answer stands behind that for a process
launched without the flag.

Channel grammar: `doc:<type>:<id>`, where `<type>` is one of the document
types (`DocType` in `schemas/realtime/envelope.py`: `chat`, `artifact`,
`chat_draft`, `file`, `notebook`) and `<id>` matches `[A-Za-z0-9._:-]{1,255}`
(`CHANNEL_PATTERN`). `chat_draft`, `file` and `notebook` are the Loro CRDT
lane's types (`schemas/realtime/crdt.py`). `chat_workspace` is the older
spelling of `chat_draft`, still accepted on the wire and mapped on the way in
and out. Two channels have no document behind them: `doc:workspace:<uuid>`
carries presence only, and `machine:<id>` is a box's control channel
(`schemas/realtime/machine.py`). A document type that no domain serves (see
`artifact` below) is refused as `not_found`. The whole channel name must also fit the outbox's 255-character `entity_id`, so an id
near the grammar's limit is refused as `bad_channel`. In-band error codes on
frames: `bad_channel`, `not_found`, `forbidden`, `unknown_frame`, `bad_frame`,
`frame_too_large`, `not_subscribed`, `too_many_channels`; on doc envelopes
(`kind: error`): `not_subscribed`, `stale_epoch`, `unsupported_kind`,
`unknown_field`, `invalid_field`, `bad_op`, `doc_too_large`, `op_too_large`,
`chunk_too_large`, `blocked`, `forbidden`, `not_found`, `quota_exceeded`.

### Close codes

| Code | Name | When |
| --- | --- | --- |
| 4401 | UNAUTHORIZED | no ticket, a bad / expired / already-used ticket, a revoked session at handshake |
| 4403 | ORIGIN_FORBIDDEN | a browser `Origin` outside the allowed set |
| 4404 | NOT_FOUND | reserved for a channel the socket may not learn exists (in-band `not_found` is the normal answer) |
| 4408 | SESSION_EXPIRED | the session was revoked, expired or the account blocked mid-socket; the socket outlived `REALTIME_WS_MAX_SESSION_SECONDS` |
| 4413 | FRAME_TOO_LARGE | a frame over `MAX_FRAME_BYTES` |
| 4429 | TOO_MANY | the per-user / per-process cap, or the frame rate limit |
| 4500 | SERVER_RESET | the server is going away; reconnect |
| 4503 | UNAVAILABLE | this process cannot deliver: it runs without the outbox listener, or that listener holds no connection; refused at the handshake, before the ticket is read |

Client handling: `4401` → mint a new ticket once, then back off; `4403` →
stop (misconfiguration, log it); `4408` → mint a new ticket and reconnect;
`4413` (or the transport's `1009`) → log and reconnect; everything else,
`4503` included → back off and reconnect (a fresh ticket every time), which
lands the client on a replica that can deliver. After any reconnect the client
re-`hello`s every channel.

`4503` is the socket's spelling of the `503` the event stream answers for the
same process: both surfaces ask the runtime one question before they admit a
connection, so a replica is never healthy on one and dead on the other.

### Session upkeep

Every `REALTIME_SSE_KEEPALIVE_SECONDS` (15) the server reloads the user,
re-checks the session (revocation, deactivation, the verification gate,
the session's own expiry) **without** sliding the idle window, refreshes the
entitlement snapshot, and heartbeats presence on every joined channel. A
failure closes the socket with `4408`.

## 3. The doc-sync envelope

```
DocEnvelope {doc_id, doc_type: chat|artifact|chat_draft|file|notebook, epoch >= 0,
             peer_id, seq >= 0,
             kind: hello|snapshot|op|ack|presence|reload|error|crdt, payload}
```

* `epoch` is owned by the server, numbered from 1; a client sends `0` only on
  a `hello` ("I have no state yet"). `seq` is the server-assigned position of a
  durable op within its epoch; a client sends `0`.
* `hello{since_seq?}` (peer_id from `welcome`) → `snapshot{state, seq}` from
  peer `srv:0` with the current epoch. `since_seq` is accepted and ignored:
  resume is a full snapshot today.
* `op{op_id, intent, events?, fields?, meta?}` at the current epoch → the
  server applies it, stamps `seq`, rebroadcasts the op to every subscriber
  (durable: an outbox `doc.op` row, `entity_id = "<type>:<id>"`,
  `version = seq`, so every replica delivers it) and answers the sender with
  `ack{op_id, seq, changed}`. `changed = false` is a no-op (a stale field, a
  relay).
* A stale `epoch` → `error{stale_epoch}` then `reload{epoch, reason}`; the
  client re-`hello`s. Never silent divergence.
* **Op size.** A durable op travels as an event-log `doc.op` row, and the log
  caps a payload at `MAX_PAYLOAD_BYTES`, smaller than the socket's
  `MAX_FRAME_BYTES`, which is derived from it so a row the log accepts always
  fits one frame. An op whose envelope, as that row would carry it,
  exceeds the cap is refused in band as `error{op_too_large}` before anything
  is applied; the socket stays open. A body that large is not synced live: the
  client says so and the writer saves it through the REST path, which
  rebuilds the document for every viewer.
* A `can_write` peer may send `snapshot{state}` to **replace** the state: the
  server rebuilds (`epoch + 1`, `seq 0`) and broadcasts
  `reload{epoch, reason: "publisher_snapshot"}`.
* Ephemeral lane: `op{intent: "chunk", events}` from a `can_write` peer, where
  every event type is one the chat log never persists, is fanned out through
  `pg_notify('alkera_rt', …)` (≤ 4 KiB) and rebroadcast as an `op` with
  `seq 0`; no ack, never persisted, never replayed. Presence rides the same
  lane.
* `crdt` carries the Loro lane (`schemas/realtime/crdt.py`), on CRDT document
  channels only; on a `chat` or `artifact` channel it is `error{unsupported_kind}`.
* A subscriber that fell behind gets a socket-level `reset{overflow}`: it
  re-`hello`s every channel.

### The Loro CRDT lane (`doc:chat_draft:<chat>`, `doc:file:<node>`, `doc:notebook:<id>`)

The wire is:

* `hello{proto, loro, doc_schema, vv_b64?, loro_peer?, epoch_seen?}` →
  `snapshot{mode: updates|snapshot, vv_b64, loro_peer, doc_schema, limits,
  data_b64 | chunk}`: what the peer's vector lacks (or the whole document),
  the server's vector after it, and the Loro peer this socket writes as. A
  peer offers `loro_peer` back only while it holds the document it wrote with
  it (it sends its vector); a fresh copy writes as a fresh peer.
* `crdt{t: update, update_id, data_b64 | chunk}` → `ack{update_id, changed,
  vv_b64}` (the server's vector after the durable commit) or
  `error{code, update_id, reason?, retry_after_ms?}`. Every other subscriber
  receives `crdt{t: update, update_id, loro_peer, user_id, vv_b64, data_b64 |
  chunk}`: the canonical delta the server committed, never the sender's bytes.
  The sender's own update is never echoed back.
* `crdt{t: ephemeral, data_b64}`: a Loro `EphemeralStore` update setting only
  the sender's own caret; relayed stamped with `loro_peer`, `user_id`,
  `display_name`, `email`, and stored nowhere.
* `seq` is always 0. `epoch` moves on history rotation (`reload{compacted}`)
  and quarantine (`reload{quarantine}`).
* Blobs over 32 KiB travel as ordered `chunk{xfer_id, index, count,
  total_bytes, sha256_b64, data_b64}` pieces; a chunked update's `xfer_id` is
  its `update_id`. One transfer carries at most 16 MiB (a sync of a whole
  file document fits); an update is held to its type's `limits.max_update_bytes`.
* `doc:file:<node_id>` is a text file co-edited live: its content is the root
  text `content`, read by anyone the Files policy lets read the node and
  written by a person it lets write it (and only while the file can be written
  back). `limits.max_text_bytes` is the
  size the client holds typing to (1 MiB). A file that is not editable text is
  refused at the `hello` with `error{not_editable, reason: binary | too_large |
  gone}`; the client shows the file read-only instead.
* Whether a file's edits reach the drive: `crdt{t: saving, state: paused |
  ok, reason}` is sent to every subscriber when a write back is first refused
  and again when one lands after it. The notice is lossy, so every `snapshot`
  and every `reload` of a document that rests somewhere also carries
  `saving{state, reason}` as the document's row records it (absent on a
  document that rests nowhere, and from an older server). A tab that opens or
  reconnects after saving paused is told by the frame that opens it.
* Error codes: `crdt_rejected` (reason names the rule: `peer`, `container`,
  `op_type`, `text_too_large`, `too_many_ops`, `undecodable`,
  `update_too_large`, `poisoned`, `validator_crash`), `crdt_resync`,
  `crdt_busy` (with `retry_after_ms`), `stale_epoch` (then `reload`),
  `forbidden`, `not_synced`, `doc_full`, `crdt_unsupported`,
  `chunk_invalid`, `chunk_timeout`, `chunk_limit`, `internal`. Every chat's
  draft lives on this lane: on the op-log lane, a `set_meta` that writes
  `meta.draft` is always `error{draft_moved}`.
* Python `loro` 1.16.2 and JS `loro-crdt` 1.16.4 are the pinned pair; only the
  backend's sandbox worker processes import Loro.

To add a CRDT document type:

1. A rule set in `backend/services/crdt/sandbox/core.py` (`RULES`): which
   containers, which operation types, text caps, an operation cap, where carets
   may point, which root text is its content, and how to project it (small:
   it is written on every edit).
2. A type in `backend/services/crdt/registry.py` implementing `CrdtDocType`:
   `rules`, `doc_schema`, `limits`, `session_policy`; `lock` (what it
   serializes against before the row); `authorize` (who reads and who writes,
   decided at every write); `seed`, `recover` (what a quarantine reseeds from)
   and `team_of` (who a server frame is addressed to); `source`, for an
   `ephemeral_session` type the `DocSource` it is written back to, `None` for a
   persistent type. The session engine (`backend/services/crdt/sessions.py`)
   does the rest. Register it in `CrdtRegistry`; the gateway's traffic filter,
   the socket's reauthorization and server frame addressing all follow the
   registry.
3. The doc type in `schemas/realtime/envelope.py` (`DocType`,
   `CRDT_DOC_TYPES`; leave it in `RESERVED_DOC_TYPES` until it is served) and
   the `crdt_docs` CHECK constraint.
4. The type in the browser's `api/realtime/crdt/docTypes.ts` (its content
   text) and a binding for its editor.

#### How a peer that is not a browser joins a file document

The wire and the authorization are the browser's; nothing is specific to it.
A VS Code extension host (or any other program acting for a person) joins a
file document like this:

1. Mint a ticket with the person's credential: `POST /api/v1/ws/tickets` with
   `Authorization: Bearer <session JWT>` (the token `alkera login` saves).
   Connect as in "Handshake" above; a missing `Origin` is allowed.
2. `subscribe{channel: "doc:file:<node_id>"}`. The server answers
   `subscribed{can_write}` by the Files policy, as for a tab, or
   `error{not_found}` when the person may not read the file.
3. Load Loro (`loro-crdt` 1.16.4 in Node, `loro` in Python), say `hello`,
   apply the `snapshot` (reassembling chunks), and from then on send local
   edits as `crdt{t: update}` exported from the server's last vector, one
   in flight, settled by the `ack`'s vector. Write only into the root text
   `content`; anything else is `crdt_rejected{container}`.
4. Carets are `crdt{t: ephemeral}` with an `EphemeralStore` value
   `{anchor, focus}` of encoded Loro cursors into `content`, under the key of
   the Loro peer the `snapshot` handed out. The server stamps who sent it.
5. Map the editor's buffer to `content` exactly (UTF-16 offsets, line endings
   as they are: never normalise CRLF), and treat `reload` as "say hello
   again with your vector".

The peer never writes the file itself: the session writes it back to the
drive (and through a chat's box when one holds the folder).

Every persisted op-log state carries `"schema_version"` (the version of its
type's state shape). A row an older writer left without it, or with an older
version, is brought forward on the next `hello` (a chat's transcript is kept
and stamped) with a `reload{reason: "schema_upgrade"}` to every subscriber. A
chat state naming a version the server does not know is refused
(`error{unsupported_kind}`) rather than rewritten; a publisher `snapshot` is
stamped with the server's version when it names none and refused when it
names another.

### Registered kinds (`artifact`)

A document type the socket does not serve itself is served only when a domain
registers it (`backend/services/realtime/doc_kinds.py`), with both how a
socket is granted the channel and the strategy that applies, seeds and
persists its operations. `artifact` is such a type: the wire names it, and the
op intent `set_fields` (last writer wins per field by `(ts, peer_id)`, a
deterministic tie-break, so two peers always converge) is its vocabulary. This
repository registers no domain for it, so its channels are refused as
`not_found`.

### `chat` (`doc:chat:<chat_id>`)

State `{"schema_version", "meta": {...}, "events": [...], "ids": {event_id: index}}`.

- **Read.** A chat is private until its Files node is shared. It is readable
  by its owner, by the machine it is bound to speaking as itself, and by
  anyone holding a rung on the chat's node ("Can view" and up, granted on the
  node, inherited from a folder above it, or made to a team the caller is on).
  Nobody else in the organisation, organisation admins included
  (`alkera_core.authz.chat_visible`).
- **Write.** The publishing peer, decided by `alkera_core.authz.chat_writable`:
  the document's owner, the machine the chat is bound to when the socket was
  opened as that machine's agent, or anyone the chat's node was shared with at
  `writer` or above. The publisher sends `append` (deduplicated by `event_id`;
  event types the chat log never persists are refused), `set_meta` (a partial
  merge that emits `chat.updated`) and `chunk`. Any reader may send
  `user_message`, which is relayed (`changed = false`) so a publisher on any
  replica receives it.
- **Declaration.** A chat must be declared before it is spoken to
  (`backend/services/realtime/chat_lookup.py`). An id nothing declared is
  `not_found`; being first to send a `hello` does not make a caller its owner.
- Whole-state cap: 4 MiB (`doc_too_large`).

A document belongs to one organisation: the organisation is part of its key.
Two organisations naming the same id hold two unrelated documents, and the
answer to a subscribe never depends on what another organisation holds.
Creation is bounded: an organisation holds at most `REALTIME_DOCS_MAX_PER_ORG`
documents of a kind (counted per kind, so one busy kind cannot starve
another), and a `hello` that would create one too many is refused in band as
`quota_exceeded`, on an open socket, with every existing document still
working.


## 4. Resume rules, in one place

| Transport | Cursor | Gap handling |
| --- | --- | --- |
| Event stream | `Last-Event-ID` / `?after=` | replayed from the outbox; `reset` if ahead / too old / overflowed |
| Socket, durable ops | none; `hello` again | full `snapshot`; every replica saw the same `doc.op` rows |
| Socket, ephemeral | none | lost by design (typing chunks, presence) |
