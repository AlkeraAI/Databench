// Bytes on a JSON wire: standard base64, and the SHA-256 a chunked blob is
// checked against. Loro hands the lane Uint8Arrays; the socket carries text.

const CHUNK = 0x8000;

/** Standard (padded) base64 of `bytes`. */
export function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let at = 0; at < bytes.length; at += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(at, at + CHUNK));
  }
  return btoa(binary);
}

/** The bytes of standard base64 `text`; throws on anything else. */
export function fromBase64(text: string): Uint8Array {
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

/** The base64 length of `n` raw bytes. */
export function base64Length(n: number): number {
  return 4 * Math.ceil(n / 3);
}

export async function sha256Base64(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new Uint8Array(bytes));
  return toBase64(new Uint8Array(digest));
}

export function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}
