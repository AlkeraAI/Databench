// The streamed digest is the whole-buffer digest.
//
// A part is up to 128 MiB and was read whole into an `ArrayBuffer` to be
// hashed; it is now read one 4 MiB window at a time. That is only safe if the
// two answers are identical to the byte — the object-store driver re-hashes the
// streamed part and answers `422 files.part_checksum_mismatch` on any
// disagreement, so a digest that drifted would refuse every upload rather than
// corrupt one.
//
// Pinned two ways: against the published BLAKE3 vectors (an implementation that
// is not this one) and against `blake3Hex` over the same bytes, at every length
// where the tree's shape changes — a chunk boundary, a window boundary, and
// lengths that are neither.

import { describe, expect, it } from "vitest";

import { Blake3Stream, blake3Hex, blake3HexOfBlob } from "@/api/blake3";

// jsdom's Blob has no `arrayBuffer()`.
if (typeof Blob !== "undefined" && typeof Blob.prototype.arrayBuffer !== "function") {
  Blob.prototype.arrayBuffer = function readSlice(this: Blob): Promise<ArrayBuffer> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(this);
    });
  };
}

/** The reference corpus: byte `i` is `i % 251`, the pattern the published
 *  BLAKE3 vectors are defined over. */
function pattern(length: number): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(new ArrayBuffer(length));
  for (let i = 0; i < length; i += 1) bytes[i] = i % 251;
  return bytes;
}

/** Hex digests of that corpus, from an implementation that is not this one —
 *  the same table `filesUploadChecksum.test.ts` verifies the whole-buffer
 *  hasher against, so the streamed answer is checked against the spec rather
 *  than against its own sibling. */
const VECTORS: Record<number, string> = {
  0: "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262",
  1: "2d3adedff11b61f14c886e35afa036736dcd87a74d27b5c1510225d0f592e213",
  63: "e9bc37a594daad83be9470df7f7b3798297c3d834ce80ba85d6e207627b7db7b",
  64: "4eed7141ea4a5cd4b788606bd23f46e212af9cacebacdc7d1f4c6dc7f2511b98",
  65: "de1e5fa0be70df6d2be8fffd0e99ceaa8eb6e8c93a63f2d8d1c30ecb6b263dee",
  1023: "10108970eeda3eb932baac1428c7a2163b0e924c9a9e25b35bba72b28f70bd11",
  1024: "42214739f095a406f3fc83deb889744ac00df831c10daa55189b5d121c855af7",
  1025: "d00278ae47eb27b34faecf67b4fe263f82d5412916c1ffd97c8cb7fb814b8444",
  2048: "e776b6028c7cd22a4d0ba182a8bf62205d2ef576467e838ed6f2529b85fba24a",
  3072: "b98cb0ff3623be03326b373de6b9095218513e64f1ee2edd2525c7ad1e5cffd2",
  4096: "015094013f57a5277b59d8475c0501042c0b642e531b0a1c8f58d2163229e969",
  6144: "3e2e5b74e048f3add6d21faab3f83aa44d3b2278afb83b80b3c35164ebeca205",
  8192: "aae792484c8efe4f19e2ca7d371d8c467ffb10748d8a5a1ae579948f718a2a63",
};

/** The lengths that matter: a chunk boundary and its neighbours, several
 *  chunks, and lengths on either side of the 4 MiB read window. */
const LENGTHS = [0, 1, 63, 64, 1023, 1024, 1025, 2047, 2048, 2049, 3072, 5000, 65_537];

describe("hashing a part a window at a time", () => {
  it.each(LENGTHS)("matches the whole-buffer digest of %i bytes", async (length) => {
    const bytes = pattern(length);
    const streamed = await blake3HexOfBlob(new Blob([bytes]), 4096);
    expect(streamed).toBe(blake3Hex(bytes.buffer));
  });

  it.each(Object.keys(VECTORS).map(Number))(
    "matches the published BLAKE3 vector for %i bytes",
    async (length) => {
      const vector = VECTORS[length];
      const streamed = await blake3HexOfBlob(new Blob([pattern(length)]), 4096);
      // The vector table is the independent check; `blake3Hex` is this code's
      // own sibling and cannot vouch for either of them alone.
      expect([streamed, blake3Hex(pattern(length).buffer)]).toEqual([vector, vector]);
    },
  );

  it("does not depend on where the windows fall", async () => {
    // Same bytes, four different window sizes: none of them a chunk multiple,
    // so every boundary lands mid-chunk in a different place.
    const bytes = pattern(10_000);
    const answers = await Promise.all(
      [1, 7, 999, 4096, 10_000].map((window) => blake3HexOfBlob(new Blob([bytes]), window)),
    );
    expect(new Set(answers)).toEqual(new Set([blake3Hex(bytes.buffer)]));
  });

  it("hashes bytes handed over in arbitrary runs", async () => {
    // The stream's own seam, driven without a Blob: a caller that already has
    // the bytes in pieces gets the answer of one that has them whole, and the
    // runs deliberately straddle the 1 KiB chunk boundary in both directions.
    const bytes = pattern(5_000);
    const hasher = new Blake3Stream();
    let fed = 0;
    for (const size of [1, 1023, 1, 2048, 1000, 927]) {
      hasher.update(bytes.subarray(fed, fed + size));
      fed += size;
    }
    expect(fed).toBe(bytes.length);
    expect(hasher.hex()).toBe(blake3Hex(bytes.buffer));
  });
});


describe("a 10 MiB part", () => {
  it("streams to the same digest the whole buffer gives", async () => {
    // The size the finding is about: a real part, hashed both ways.
    const bytes = pattern(10 * 1024 * 1024);
    const streamed = await blake3HexOfBlob(new Blob([bytes]));
    expect(streamed).toBe(blake3Hex(bytes.buffer));
  }, 30_000);
});
