// The notebook harness's own dev-server entry (dev-notebook.html): two people
// and an agent editing one notebook in one page. Not a build input in
// vite.config.ts, so no production bundle carries it.
//
// The page runs a stand-in for the server's half of the Loro lane, in the
// browser and on the app's own Loro: it grants every subscribe, answers a
// hello with a snapshot (or only what the vector lacks), imports an update,
// acks it with the vector and broadcasts it, and relays carets stamped with
// who sent them. Every frame crosses it after a short delay, as over a
// network. Ana and Ben are each the real notebook tab on a socket of their
// own; the agent is a third copy of the document on a third socket, writing
// notebook operations as an agent does. The notebook routes the tab calls are
// answered here and logged for the spec.

import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";

import "@alkera/ui/fonts";
import "@alkera/ui/styles";
import "../styles/portal.css";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { fromBase64, toBase64 } from "@/api/realtime/crdt/bytes";
import { LiveDocChannel, type LiveSocket } from "@/api/realtime/crdt/channel";
import { ChunkAssembler, isChunk } from "@/api/realtime/crdt/chunks";
import { loadLoro, type LoroApi } from "@/api/realtime/crdt/loro";
import { NotebookDocument } from "@/api/realtime/crdt/notebookDoc";
import type { ClientFrame, EnvelopeKind, ServerFrame } from "@/api/realtime/wsClient";
import NotebookTab from "@/pages/workspace/chat/workspace/notebook/NotebookTab";
import type { WorkspaceCtx, WorkspaceTab } from "@/pages/workspace/chat/workspace/tabKinds";
import type { NotebookOp } from "@alkera/notebook-ui";

const NODE = "9b1c2d3e-4f50-4a6b-8c7d-0e1f2a3b4c5d";
const CHANNEL = `doc:notebook:${NODE}`;
const DRIVE = "drive-1";
/** One hop across the stand-in network, each way. */
const HOP_MS = 25;
/** The seeded cells' ids, so the spec can name them. */
const SEEDED = ["aaaaaaaaa1", "bbbbbbbbb2", "ccccccccc3"] as const;

type LoroDoc = InstanceType<LoroApi["LoroDoc"]>;

interface Logged {
  url: string;
  method: string;
  body: unknown;
}

interface Person {
  name: string;
  email: string;
}

class HarnessSocket implements LiveSocket {
  readonly peerId: string;
  readonly limits = null;
  readonly held = new Set<string>();
  private readonly frameListeners = new Set<(frame: ServerFrame) => void>();

  constructor(
    private readonly server: HarnessServer,
    readonly person: Person,
  ) {
    this.peerId = `p:${person.name.toLowerCase()}`;
  }

  subscribe(channel: string): () => void {
    this.held.add(channel);
    this.server.post(this, { t: "subscribe", channel } as ClientFrame);
    return () => {
      this.held.delete(channel);
      this.server.post(this, { t: "unsubscribe", channel } as ClientFrame);
    };
  }

  send(frame: ClientFrame): boolean {
    this.server.post(this, frame);
    return true;
  }

  onFrame(listener: (frame: ServerFrame) => void): () => void {
    this.frameListeners.add(listener);
    return () => void this.frameListeners.delete(listener);
  }

  onOpen(): () => void {
    return () => {};
  }

  /** A frame from the server, one hop later. Equal delays keep the order. */
  deliver(frame: ServerFrame): void {
    setTimeout(() => {
      for (const listener of [...this.frameListeners]) listener(frame);
    }, HOP_MS);
  }
}

class HarnessServer {
  readonly doc: LoroDoc;
  readonly sockets: HarnessSocket[] = [];
  private readonly peers = new Map<HarnessSocket, number>();
  private nextPeer = 5000;
  private readonly assembler = new ChunkAssembler();
  private readonly epoch = 1;

  constructor(private readonly loro: LoroApi) {
    this.doc = new loro.LoroDoc();
    this.doc.setPeerId(1n);
  }

  socket(person: Person): HarnessSocket {
    const socket = new HarnessSocket(this, person);
    this.sockets.push(socket);
    return socket;
  }

  /** A frame from a socket, one hop later. */
  post(socket: HarnessSocket, frame: ClientFrame): void {
    setTimeout(() => this.receive(socket, frame), HOP_MS);
  }

  private envelope(kind: EnvelopeKind, payload: Record<string, unknown>, peer = "srv:0"): ServerFrame {
    return {
      t: "doc",
      envelope: { doc_id: NODE, doc_type: "notebook", epoch: this.epoch, peer_id: peer, seq: 0, kind, payload },
    };
  }

  private others(socket: HarnessSocket): HarnessSocket[] {
    return this.sockets.filter((other) => other !== socket && other.held.has(CHANNEL));
  }

  /** An engine event on the notebook channel, to every socket that holds it. */
  engine(event: Record<string, unknown> & { type: string }): void {
    const channel = `nb:${NODE}`;
    for (const socket of this.sockets) if (socket.held.has(channel)) socket.deliver({ t: "nb", channel, event });
  }

  private receive(socket: HarnessSocket, frame: ClientFrame): void {
    if (frame.t === "subscribe") {
      socket.deliver({ t: "subscribed", channel: frame.channel, can_write: true });
      return;
    }
    if (frame.t !== "doc") return;
    const env = frame.envelope;
    const payload = env.payload;
    if (env.kind === "hello") {
      let peer = this.peers.get(socket);
      if (peer === undefined) {
        peer = this.nextPeer++;
        this.peers.set(socket, peer);
      }
      const delta = typeof payload.vv_b64 === "string" && payload.epoch_seen === this.epoch;
      const data = delta
        ? this.doc.export({ mode: "update", from: this.loro.VersionVector.decode(fromBase64(payload.vv_b64 as string)) })
        : this.doc.export({ mode: "snapshot" });
      socket.deliver(
        this.envelope("snapshot", {
          mode: delta ? "updates" : "snapshot",
          vv_b64: toBase64(this.doc.oplogVersion().encode()),
          loro_peer: peer,
          doc_schema: 1,
          limits: { chunk_bytes: 32 * 1024, max_update_bytes: 512 * 1024, max_doc_bytes: 2 * 1024 * 1024, max_text_bytes: 16 * 1024 },
          data_b64: toBase64(data),
        }),
      );
      return;
    }
    if (env.kind !== "crdt") return;
    if (payload.t === "update") {
      void this.update(socket, payload);
    } else if (payload.t === "ephemeral") {
      const stamped = {
        t: "ephemeral",
        data_b64: payload.data_b64,
        loro_peer: this.peers.get(socket),
        user_id: `user-${socket.person.name.toLowerCase()}`,
        display_name: socket.person.name,
        email: socket.person.email,
      };
      for (const other of this.others(socket)) other.deliver(this.envelope("crdt", stamped, socket.peerId));
    }
  }

  private async update(socket: HarnessSocket, payload: Record<string, unknown>): Promise<void> {
    const id = String(payload.update_id);
    let data: Uint8Array;
    if (isChunk(payload.chunk)) {
      const whole = await this.assembler.add(payload.chunk);
      if (whole === null) return;
      data = whole;
    } else {
      data = fromBase64(String(payload.data_b64 ?? ""));
    }
    const before = this.doc.oplogVersion();
    const status = this.doc.import(data);
    if (status.pending !== null && status.pending.size > 0) {
      socket.deliver(this.envelope("error", { code: "crdt_resync", update_id: id }));
      return;
    }
    const delta = this.doc.export({ mode: "update", from: before });
    const vv = toBase64(this.doc.oplogVersion().encode());
    socket.deliver(this.envelope("ack", { update_id: id, changed: delta.length > 0, vv_b64: vv }));
    for (const other of this.others(socket)) {
      other.deliver(
        this.envelope("crdt", { t: "update", update_id: id, data_b64: toBase64(delta), vv_b64: vv, loro_peer: this.peers.get(socket) }, socket.peerId),
      );
    }
  }
}

/** What the server holds: the live cells in order, with their text. */
interface ServerCells {
  order: string[];
  sources: Record<string, string>;
}

declare global {
  interface Window {
    /** The agent writes a batch of notebook operations; the ids it created. */
    agent(ops: NotebookOp[]): Promise<string[]>;
    /** An engine event on the notebook channel, to both tabs. */
    engine(event: Record<string, unknown> & { type: string }): void;
    /** The server's copy of the notebook. */
    serverCells(): ServerCells;
    /** The agent's copy of the notebook. */
    agentCells(): ServerCells;
    /** Every notebook request either tab made. */
    requests: Logged[];
    seeded: readonly string[];
    /** Set once the agent's copy is live. */
    agentReady: boolean;
  }
}

window.requests = [];
window.seeded = SEEDED;
window.agentReady = false;

/** The output frame page a spec serves, named in the query (`?frame=<url>`):
 *  with it the view offers framed outputs, as a deployment with a content
 *  origin does. */
const FRAME_URL = new URLSearchParams(window.location.search).get("frame");
/** The comm opens the engine has announced, replayed to a frame that attaches. */
const commOpens: Record<string, unknown>[] = [];
let frames = 0;

const BOX_ROOT = "/opt/alkera-work/orgs/0/work/.alkera/chats/chat-1/scratch";
const ENVS = [
  { env_id: "uv_project:.", kind: "uv_project", spec_root: BOX_ROOT, python: "", state: "ready", recorded_in_file: false },
  { env_id: "default:.alkera/envs/default", kind: "default", spec_root: `${BOX_ROOT}/.alkera/envs/default`, python: "3.12.13", state: "ready", recorded_in_file: true },
];

const realFetch = window.fetch.bind(window);
window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (!url.includes("/api/")) return realFetch(input, init);
  const method = init.method ?? "GET";
  window.requests.push({ url, method, body: typeof init.body === "string" ? JSON.parse(init.body) : null });
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  if (url.endsWith("/runs")) return json({ run_id: `r${window.requests.length}`, status: "queued", frontier_included: true, repeat: false });
  // The listing a box answers: its spec roots are the box's own paths, and a
  // version it has not read yet is empty.
  if (url.endsWith("/envs")) return json({ current: ENVS[0], envs: ENVS });
  if (url.endsWith("/frames") && method === "POST") return json({ frame_id: `frame-${++frames}`, opens: commOpens });
  if (url.includes(`/api/v1/notebooks/${DRIVE}/${NODE}`) && method === "GET") {
    return json({
      token: "t",
      format: "1.0",
      read_only_reason: null,
      settings: {},
      cells: [],
      kernel: null,
      presence: [],
      ...(FRAME_URL ? { output_frame_url: FRAME_URL } : {}),
    });
  }
  return json({});
};

function cellsOf(doc: NotebookDocument): ServerCells {
  const cells = doc.snapshot().cells;
  return { order: cells.map((c) => c.id), sources: Object.fromEntries(cells.map((c) => [c.id, c.source])) };
}

/** A notebook document over a Loro document held directly (the server's). */
function heldDocument(loro: LoroApi, doc: LoroDoc, newId?: () => string): NotebookDocument {
  return new NotebookDocument({
    loro,
    source: { doc, canWrite: true, listen: () => () => {}, localCommitted: () => {} },
    ...(newId ? { newId } : {}),
  });
}

function Pane({ socket, person }: { socket: HarnessSocket; person: Person }) {
  const ctx: WorkspaceCtx = { chatId: "chat-1", driveId: DRIVE, rootNodeId: "root", liveSocket: socket };
  const tab: WorkspaceTab = { id: `t-${person.name}`, kind: "file", node_id: NODE, name: "analysis.alknb.py", view: "notebook" };
  const item = { id: NODE, name: "analysis.alknb.py" } as unknown as Item;
  const [client] = useState(() => createQueryClient({ retry: false }));
  return (
    <section className="nb-harness__pane" data-person={person.name.toLowerCase()} aria-label={person.name}>
      <h2 className="nb-harness__who">{person.name}</h2>
      <div className="nb-harness__tab">
        <QueryClientProvider client={client}>
          <NotebookTab tab={tab} ctx={ctx} item={item} onEdit={() => {}} loadLoro={loadLoro} />
        </QueryClientProvider>
      </div>
    </section>
  );
}

const STYLE = `
html, body { margin: 0; background: var(--alkPageBg, #fff); color: var(--alkPrimaryText, #111); }
.nb-harness { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 8px; box-sizing: border-box; }
.nb-harness__pane { min-width: 0; display: flex; flex-direction: column; border: 1px solid var(--alkBorder, #ccc); }
.nb-harness__who { margin: 0; padding: 4px 8px; font: 600 13px var(--alkFontUi, sans-serif); }
.nb-harness__tab { height: calc(100vh - 60px); min-height: 0; overflow: auto; }
@media (max-width: 800px) { .nb-harness { grid-template-columns: 1fr; } }
`;

async function main(): Promise<void> {
  const loro = await loadLoro();
  const server = new HarnessServer(loro);
  let n = 0;
  heldDocument(loro, server.doc, () => SEEDED[n++]!).apply([
    { op: "insert", source: "x = 1", name: "load" },
    { op: "insert", source: "y = x + 1" },
    { op: "insert", source: "print(y)" },
  ]);
  const serverView = heldDocument(loro, server.doc);
  window.serverCells = () => cellsOf(serverView);
  window.engine = (event) => {
    if (event.type === "comm.open") commOpens.push(event);
    server.engine(event);
  };

  const ana = server.socket({ name: "Ana", email: "ana@acme.test" });
  const ben = server.socket({ name: "Ben", email: "ben@acme.test" });

  // The agent: its own copy of the document on its own socket.
  const agentSocket = server.socket({ name: "Agent", email: "agent@acme.test" });
  // The harness signs nobody in: its channels keep no stash for a next page.
  const agentChannel = new LiveDocChannel({ socket: agentSocket, loadLoro: () => Promise.resolve(loro), docType: "notebook", docId: NODE, account: null });
  let agentDoc: NotebookDocument | null = null;
  const live = new Promise<NotebookDocument>((resolve) => {
    agentChannel.listen({
      phase: (phase) => {
        if (phase !== "live" || agentDoc !== null) return;
        agentDoc = new NotebookDocument({ loro, source: agentChannel });
        window.agentReady = true;
        resolve(agentDoc);
      },
    });
  });
  agentChannel.start();
  window.agent = async (ops) => (await live).apply(ops).created;
  window.agentCells = () => (agentDoc === null ? { order: [], sources: {} } : cellsOf(agentDoc));

  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.appendChild(style);
  const root = document.getElementById("root");
  if (!root) throw new Error("#root element not found");
  createRoot(root).render(
    <StrictMode>
      <main className="nb-harness">
        <Pane socket={ana} person={ana.person} />
        <Pane socket={ben} person={ben.person} />
      </main>
    </StrictMode>,
  );
}

void main();
