// BLAKE3, the digest `X-Part-Checksum` carries — shared by the main thread and
// the hashing worker.
//
// It lives in its own module for exactly that reason: the upload client hashes
// a part off the main thread (`blake3.worker.ts`) and falls back to hashing it
// in-thread when no worker is available, and the two paths MUST agree to the
// byte — the object-store driver re-hashes the streamed part and answers
// `422 files.part_checksum_mismatch` on any disagreement. One implementation,
// two callers.

const HEX = "0123456789abcdef";

function toHex(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (const byte of bytes) out += HEX[byte >> 4] + HEX[byte & 15];
  return out;
}

/* ---------------------------------------------------------------------------
 * A direct port of the reference construction (7 rounds, 1 KiB chunks, binary
 * tree), hash mode only — no keying, no derive-key, and a fixed 32-byte output,
 * because that is the whole of what `X-Part-Checksum` is. It is pure ES2020, so
 * it needs no wasm fetch and no new dependency; a part is hashed once, off the
 * bytes that are already in memory to be sent.
 *
 * It is also SYNCHRONOUS and O(bytes) — ~1 s per 32 MiB part — which is why the
 * upload client runs it inside a worker rather than on the main thread.
 * ------------------------------------------------------------------------- */

const BLAKE3_IV = [
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
] as const;
const BLAKE3_PERMUTATION = [2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8] as const;
const CHUNK_START = 1;
const CHUNK_END = 2;
const PARENT = 4;
const ROOT = 8;
const CHUNK_BYTES = 1024;
const BLOCK_BYTES = 64;

function rotr(word: number, bits: number): number {
  return ((word >>> bits) | (word << (32 - bits))) >>> 0;
}

function mix(
  s: Uint32Array,
  a: number,
  b: number,
  c: number,
  d: number,
  mx: number,
  my: number,
): void {
  s[a] = (s[a]! + s[b]! + mx) >>> 0;
  s[d] = rotr(s[d]! ^ s[a]!, 16);
  s[c] = (s[c]! + s[d]!) >>> 0;
  s[b] = rotr(s[b]! ^ s[c]!, 12);
  s[a] = (s[a]! + s[b]! + my) >>> 0;
  s[d] = rotr(s[d]! ^ s[a]!, 8);
  s[c] = (s[c]! + s[d]!) >>> 0;
  s[b] = rotr(s[b]! ^ s[c]!, 7);
}

/** One compression. Returns the full 16-word state; the caller keeps the first
 *  eight as a chaining value, or as the 32-byte root output. */
function compress(
  cv: Uint32Array,
  block: Uint32Array,
  counter: number,
  blockLen: number,
  flags: number,
): Uint32Array {
  const s = new Uint32Array(16);
  s.set(cv.subarray(0, 8), 0);
  s.set(BLAKE3_IV.slice(0, 4), 8);
  // The counter is a u64; a part is far below 2^32 chunks, but the split is
  // written out so the high word is a real value rather than an assumption.
  s[12] = counter >>> 0;
  s[13] = Math.floor(counter / 0x1_0000_0000) >>> 0;
  s[14] = blockLen >>> 0;
  s[15] = flags >>> 0;

  let m = block;
  for (let round = 0; round < 7; round += 1) {
    mix(s, 0, 4, 8, 12, m[0]!, m[1]!);
    mix(s, 1, 5, 9, 13, m[2]!, m[3]!);
    mix(s, 2, 6, 10, 14, m[4]!, m[5]!);
    mix(s, 3, 7, 11, 15, m[6]!, m[7]!);
    mix(s, 0, 5, 10, 15, m[8]!, m[9]!);
    mix(s, 1, 6, 11, 12, m[10]!, m[11]!);
    mix(s, 2, 7, 8, 13, m[12]!, m[13]!);
    mix(s, 3, 4, 9, 14, m[14]!, m[15]!);
    if (round === 6) break;
    const next = new Uint32Array(16);
    for (let i = 0; i < 16; i += 1) next[i] = m[BLAKE3_PERMUTATION[i]!]!;
    m = next;
  }
  for (let i = 0; i < 8; i += 1) {
    s[i] = (s[i]! ^ s[i + 8]!) >>> 0;
    s[i + 8] = (s[i + 8]! ^ cv[i]!) >>> 0;
  }
  return s;
}

/** The 64-byte block at `offset`, zero-padded, as little-endian words. */
function blockWords(bytes: Uint8Array, offset: number): Uint32Array {
  const words = new Uint32Array(16);
  for (let i = 0; i < 16; i += 1) {
    const at = offset + i * 4;
    words[i] =
      ((bytes[at] ?? 0) |
        ((bytes[at + 1] ?? 0) << 8) |
        ((bytes[at + 2] ?? 0) << 16) |
        ((bytes[at + 3] ?? 0) << 24)) >>>
      0;
  }
  return words;
}

/** One chunk's output words: a chaining value, or the root output when
 *  `extra` carries ROOT. */
function chunkWords(
  bytes: Uint8Array,
  from: number,
  length: number,
  counter: number,
  extra: number,
): Uint32Array {
  let cv = Uint32Array.from(BLAKE3_IV);
  const blocks = Math.max(1, Math.ceil(length / BLOCK_BYTES));
  for (let i = 0; i < blocks; i += 1) {
    const offset = i * BLOCK_BYTES;
    const blockLen = Math.min(BLOCK_BYTES, length - offset);
    let flags = 0;
    if (i === 0) flags |= CHUNK_START;
    if (i === blocks - 1) flags |= CHUNK_END | extra;
    const out = compress(cv, blockWords(bytes, from + offset), counter, Math.max(blockLen, 0), flags);
    cv = out.slice(0, 8);
    if (i === blocks - 1) return out;
  }
  return cv;
}

function parentWords(left: Uint32Array, right: Uint32Array, extra: number): Uint32Array {
  const block = new Uint32Array(16);
  block.set(left.subarray(0, 8), 0);
  block.set(right.subarray(0, 8), 8);
  return compress(Uint32Array.from(BLAKE3_IV), block, 0, BLOCK_BYTES, PARENT | extra);
}

/** The number of chunks in the LEFT subtree: the largest power of two strictly
 *  below the total. This is what makes the tree deterministic. */
function leftChunks(chunks: number): number {
  let half = 1;
  while (half * 2 < chunks) half *= 2;
  return half;
}

/** A non-root subtree's chaining value. */
function subtreeWords(bytes: Uint8Array, from: number, length: number, counter: number): Uint32Array {
  if (length <= CHUNK_BYTES) return chunkWords(bytes, from, length, counter, 0).slice(0, 8);
  const left = leftChunks(Math.ceil(length / CHUNK_BYTES)) * CHUNK_BYTES;
  return parentWords(
    subtreeWords(bytes, from, left, counter),
    subtreeWords(bytes, from + left, length - left, counter + left / CHUNK_BYTES),
    0,
  ).slice(0, 8);
}

/** BLAKE3-256 of `input`, hex — the value `X-Part-Checksum` carries. */
export function blake3Hex(input: ArrayBuffer): string {
  const bytes = new Uint8Array(input);
  const length = bytes.byteLength;
  const words =
    length <= CHUNK_BYTES
      ? chunkWords(bytes, 0, length, 0, ROOT)
      : (() => {
          const left = leftChunks(Math.ceil(length / CHUNK_BYTES)) * CHUNK_BYTES;
          return parentWords(
            subtreeWords(bytes, 0, left, 0),
            subtreeWords(bytes, left, length - left, left / CHUNK_BYTES),
            ROOT,
          );
        })();
  return digestHex(words);
}

/** The first eight output words as the 32-byte digest, hex. */
function digestHex(words: Uint32Array): string {
  const out = new Uint8Array(32);
  for (let i = 0; i < 8; i += 1) {
    const word = words[i]!;
    out[i * 4] = word & 0xff;
    out[i * 4 + 1] = (word >>> 8) & 0xff;
    out[i * 4 + 2] = (word >>> 16) & 0xff;
    out[i * 4 + 3] = (word >>> 24) & 0xff;
  }
  return toHex(out.buffer);
}

/* ---------------------------------------------------------------------------
 * The same hash, one bounded window at a time.
 *
 * {@link blake3Hex} needs the whole input resident, and a part is up to 128
 * MiB — so a tab that read each part into an `ArrayBuffer` to hash it held a
 * part's worth of bytes per upload in flight, on top of the copy the request
 * body holds. BLAKE3's tree is built from 1 KiB chunks over a stack of subtree
 * chaining values, so it never needs more than the chunk being filled plus that
 * stack (54 entries at the format's 16 TiB ceiling).
 *
 * This is the reference incremental construction, so it agrees with
 * {@link blake3Hex} for every input by construction — and a test pins that,
 * because a client whose digest drifts is refused by the store with
 * `422 files.part_checksum_mismatch` and no upload at all.
 * ------------------------------------------------------------------------- */

/** A BLAKE3 hash built from however the bytes happen to arrive. */
export class Blake3Stream {
  /** Chaining values of the completed subtrees, newest last. */
  private readonly stack: Uint32Array[] = [];
  private readonly chunk = new Uint8Array(CHUNK_BYTES);
  private chunkLen = 0;
  /** Index of the chunk being filled — BLAKE3's chunk counter. */
  private chunkCounter = 0;

  update(bytes: Uint8Array): this {
    let at = 0;
    while (at < bytes.length) {
      // Closed only when more bytes follow: the LAST chunk is finalized by
      // `hex()`, which is where the root flag belongs.
      if (this.chunkLen === CHUNK_BYTES) this.closeChunk();
      const take = Math.min(CHUNK_BYTES - this.chunkLen, bytes.length - at);
      this.chunk.set(bytes.subarray(at, at + take), this.chunkLen);
      this.chunkLen += take;
      at += take;
    }
    return this;
  }

  /** The digest of everything given so far, hex. */
  hex(): string {
    // The view, not the buffer: the last block is read as a whole 64 bytes and
    // padded with zeros past the input, and the 1 KiB buffer is REUSED — so
    // handing over the whole of it would pad a partial chunk with the previous
    // chunk's bytes instead.
    const tail = this.chunk.subarray(0, this.chunkLen);
    let words =
      this.stack.length === 0
        ? chunkWords(tail, 0, this.chunkLen, this.chunkCounter, ROOT)
        : chunkWords(tail, 0, this.chunkLen, this.chunkCounter, 0).slice(0, 8);
    for (let i = this.stack.length - 1; i >= 0; i -= 1) {
      const root = i === 0;
      const parent = parentWords(this.stack[i]!, words, root ? ROOT : 0);
      words = root ? parent : parent.slice(0, 8);
    }
    return digestHex(words);
  }

  private closeChunk(): void {
    const cv = chunkWords(this.chunk, 0, CHUNK_BYTES, this.chunkCounter, 0).slice(0, 8);
    this.merge(cv, this.chunkCounter + 1);
    this.chunkCounter += 1;
    this.chunkLen = 0;
  }

  /** Fold into the stack while the number of chunks below is even — the rule
   *  that makes this the same tree the recursive split builds. */
  private merge(cv: Uint32Array, chunksSoFar: number): void {
    let value = cv;
    let remaining = chunksSoFar;
    while ((remaining & 1) === 0) {
      value = parentWords(this.stack.pop()!, value, 0).slice(0, 8);
      remaining >>>= 1;
    }
    this.stack.push(value);
  }
}

/** How much of a part is resident while it is hashed. Large enough that the
 *  per-slice overhead disappears against the arithmetic, small enough that a
 *  128 MiB part costs this instead of itself. */
export const HASH_WINDOW_BYTES = 4 * 1024 * 1024;

/**
 * BLAKE3-256 of a Blob, read one window at a time, so a part never has to be
 * resident to be hashed.
 *
 * The read can fail: a `File` is a handle onto a file that may be moved,
 * renamed, deleted or on a volume that goes away, and `arrayBuffer()` answers
 * that with a `NotReadableError`. It is left to reject rather than swallowed —
 * a checksum invented for bytes nobody could read is worse than a refusal — and
 * the signal lets a cancel stop a multi-second hash between two windows instead
 * of after the last one.
 */
export async function blake3HexOfBlob(
  part: Blob,
  windowBytes: number = HASH_WINDOW_BYTES,
  signal?: AbortSignal,
): Promise<string> {
  const hasher = new Blake3Stream();
  for (let at = 0; at < part.size; at += windowBytes) {
    if (signal?.aborted === true) throw new DOMException("aborted", "AbortError");
    const window = await part.slice(at, Math.min(at + windowBytes, part.size)).arrayBuffer();
    hasher.update(new Uint8Array(window));
  }
  return hasher.hex();
}
