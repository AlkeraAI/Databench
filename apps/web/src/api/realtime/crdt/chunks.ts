// Splitting a Loro blob into frames and putting one back together — the
// browser's half of the server's `crdt/chunks.py`, with the same rules: pieces
// in order, every piece agreeing with the first, the whole adding up and
// hashing right. A transfer that breaks a rule is dropped; the next sync
// brings the bytes again.

import { concat, fromBase64, sha256Base64, toBase64 } from "./bytes";

/** The largest raw blob one frame carries (the server's `CRDT_CHUNK_BYTES`). */
export const CHUNK_BYTES = 32 * 1024;

export interface Chunk {
  xfer_id: string;
  index: number;
  count: number;
  total_bytes: number;
  sha256_b64: string;
  data_b64: string;
}

/** `data` as ordered pieces; only for a blob over one piece. */
export async function split(data: Uint8Array, xferId: string): Promise<Chunk[]> {
  if (data.length <= CHUNK_BYTES) throw new Error("a blob that fits one frame is sent inline");
  const digest = await sha256Base64(data);
  const count = Math.ceil(data.length / CHUNK_BYTES);
  const pieces: Chunk[] = [];
  for (let index = 0; index < count; index += 1) {
    pieces.push({
      xfer_id: xferId,
      index,
      count,
      total_bytes: data.length,
      sha256_b64: digest,
      data_b64: toBase64(data.subarray(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES)),
    });
  }
  return pieces;
}

/** The size a piece adds to a frame (for the send budget). */
export function chunkFrameBytes(chunk: Chunk): number {
  return chunk.data_b64.length + 300;
}


export function isChunk(value: unknown): value is Chunk {
  if (typeof value !== "object" || value === null) return false;
  const c = value as Record<string, unknown>;
  return (
    typeof c.xfer_id === "string" &&
    Number.isInteger(c.index) &&
    Number.isInteger(c.count) &&
    Number.isInteger(c.total_bytes) &&
    typeof c.sha256_b64 === "string" &&
    typeof c.data_b64 === "string"
  );
}

interface Open {
  count: number;
  total: number;
  sha: string;
  parts: Uint8Array[];
}

/** One channel's incoming transfers. */
export class ChunkAssembler {
  private readonly open = new Map<string, Open>();

  /** The whole blob once its last piece arrives; null before it, and null
   *  (with the transfer dropped) for a piece that breaks a rule. */
  async add(chunk: Chunk): Promise<Uint8Array | null> {
    let transfer = this.open.get(chunk.xfer_id);
    if (transfer === undefined) {
      if (chunk.index !== 0) return null;
      transfer = { count: chunk.count, total: chunk.total_bytes, sha: chunk.sha256_b64, parts: [] };
      this.open.set(chunk.xfer_id, transfer);
    }
    if (
      chunk.index !== transfer.parts.length ||
      chunk.count !== transfer.count ||
      chunk.total_bytes !== transfer.total ||
      chunk.sha256_b64 !== transfer.sha
    ) {
      this.open.delete(chunk.xfer_id);
      return null;
    }
    let piece: Uint8Array;
    try {
      piece = fromBase64(chunk.data_b64);
    } catch {
      this.open.delete(chunk.xfer_id);
      return null;
    }
    transfer.parts.push(piece);
    if (chunk.index !== chunk.count - 1) return null;
    this.open.delete(chunk.xfer_id);
    const whole = concat(transfer.parts);
    if (whole.length !== transfer.total || (await sha256Base64(whole)) !== transfer.sha) return null;
    return whole;
  }

  clear(): void {
    this.open.clear();
  }
}
