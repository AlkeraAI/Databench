// Raster images: base64 data from the bundle, drawn as a data URL `<img>`.

import type { OutputRenderer, OutputRendererProps } from "./types";

export const IMAGE_MIMES = ["image/png", "image/jpeg", "image/gif", "image/webp"] as const;
export type ImageMime = (typeof IMAGE_MIMES)[number];

const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

/** The image's base64 payload with whitespace removed, or `null` when the
 *  value is not base64 (so it never becomes part of a URL). */
export function imageBase64(value: unknown): string | null {
  const raw = Array.isArray(value) ? value.join("") : value;
  if (typeof raw !== "string") return null;
  const compact = raw.replace(/\s+/g, "");
  if (compact === "" || !BASE64.test(compact)) return null;
  return compact;
}

export function imageDataUrl(mime: string, value: unknown): string | null {
  if (!(IMAGE_MIMES as readonly string[]).includes(mime)) return null;
  const base64 = imageBase64(value);
  return base64 === null ? null : `data:${mime};base64,${base64}`;
}

function decodeBase64(base64: string): Uint8Array<ArrayBuffer> {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

const EXTENSIONS: Record<ImageMime, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
};

/** The image as a file blob and the name it downloads under. */
export function imageFile(mime: string, value: unknown, baseName = "output"): { blob: Blob; filename: string } | null {
  if (!(IMAGE_MIMES as readonly string[]).includes(mime)) return null;
  const base64 = imageBase64(value);
  if (base64 === null) return null;
  const blob = new Blob([decodeBase64(base64)], { type: mime });
  return { blob, filename: `${baseName}.${EXTENSIONS[mime as ImageMime]}` };
}

/** Saves an image output as a file through a temporary download link.
 *  Returns false when the value is not an image this package draws. */
export function downloadImage(mime: string, value: unknown, baseName = "output"): boolean {
  const file = imageFile(mime, value, baseName);
  if (!file) return false;
  const url = URL.createObjectURL(file.blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = file.filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Let the click start the download before the URL goes away.
  setTimeout(() => URL.revokeObjectURL(url), 0);
  return true;
}

function dimension(metadata: Record<string, unknown> | undefined, mime: string, key: "width" | "height"): number | undefined {
  const perMime = metadata?.[mime];
  const source = perMime && typeof perMime === "object" ? (perMime as Record<string, unknown>) : metadata;
  const value = source?.[key];
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;
}

export function ImageOutput({ mime, data, metadata }: OutputRendererProps) {
  const src = imageDataUrl(mime, data);
  if (!src) return <p className="nb-output-note">This image could not be read.</p>;
  const alt = typeof metadata?.alt === "string" && metadata.alt ? metadata.alt : "Output image";
  return (
    <img
      className="nb-output-image"
      src={src}
      alt={alt}
      width={dimension(metadata, mime, "width")}
      height={dimension(metadata, mime, "height")}
    />
  );
}

export const imageRenderer: OutputRenderer = {
  id: "alkera.image",
  mimes: IMAGE_MIMES,
  rank: 0,
  place: "app",
  Component: ImageOutput,
};
