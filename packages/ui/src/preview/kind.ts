import type { PreviewFacts } from "./types";

// What a file IS, decided once, for everyone.
//
// The sniffed mime is the better answer and wins wherever the server produced
// one. But a writer that never set a content type leaves `application/octet-stream`
// on bytes that plainly are a PNG, and keying on the mime alone puts a picture
// behind a "Preview is not available for this type" card. So a mime that carries
// no information yields to the name, which at least says what the writer meant.
//
// `text/plain` is treated as a family rather than a type: a `.csv`, a `.md` and a
// `.py` all arrive under it and each has a better reader than a raw `<pre>`. The
// name refines WITHIN that family only — it never pulls bytes the server really
// did read as text over to the image renderer, which would draw a broken picture.

/** The families a preview surface can draw. One per renderer, plus `office` for
 *  the formats we name but cannot draw yet, and `binary` for the rest. */
export type PreviewKind =
  | "image"
  | "svg"
  | "pdf"
  | "html"
  | "csv"
  | "markdown"
  | "code"
  | "text"
  | "audio"
  | "video"
  | "office"
  | "binary";

/** The mime without its parameters — `text/html; charset=utf-8` is `text/html`. */
export function mimeEssence(mime: string): string {
  return mime.split(";")[0]?.trim().toLowerCase() ?? "";
}

/** The mimes that mean "nobody looked". They are not evidence of anything, so a
 *  file wearing one is classified by its name instead. */
const UNINFORMATIVE_MIMES = new Set([
  "",
  "application/octet-stream",
  "binary/octet-stream",
  "application/binary",
  "application/x-binary",
  "application/unknown",
  "content/unknown",
  "application/force-download",
  "application/download",
  "*/*",
]);

const MIME_KINDS = new Map<string, PreviewKind>([
  ["image/svg+xml", "svg"],
  ["application/pdf", "pdf"],
  ["text/html", "html"],
  ["application/xhtml+xml", "html"],
  ["text/csv", "csv"],
  ["application/csv", "csv"],
  ["text/tab-separated-values", "csv"],
  ["text/markdown", "markdown"],
  ["text/x-markdown", "markdown"],
  ["application/json", "code"],
  ["application/yaml", "code"],
  ["text/yaml", "code"],
  ["application/x-yaml", "code"],
  ["application/toml", "code"],
  ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "office"],
  ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "office"],
  ["application/vnd.openxmlformats-officedocument.presentationml.presentation", "office"],
  ["application/vnd.ms-excel", "office"],
  ["application/msword", "office"],
  ["application/vnd.ms-powerpoint", "office"],
]);

const EXTENSION_KINDS = new Map<string, PreviewKind>([
  ["png", "image"],
  ["jpg", "image"],
  ["jpeg", "image"],
  ["gif", "image"],
  ["webp", "image"],
  ["avif", "image"],
  ["bmp", "image"],
  ["ico", "image"],
  ["svg", "svg"],
  ["pdf", "pdf"],
  ["html", "html"],
  ["htm", "html"],
  ["csv", "csv"],
  ["tsv", "csv"],
  ["md", "markdown"],
  ["markdown", "markdown"],
  ["txt", "text"],
  ["log", "text"],
  ["mp3", "audio"],
  ["wav", "audio"],
  ["m4a", "audio"],
  ["aac", "audio"],
  ["ogg", "audio"],
  ["flac", "audio"],
  ["mp4", "video"],
  ["webm", "video"],
  ["mov", "video"],
  ["m4v", "video"],
  ["xlsx", "office"],
  ["xls", "office"],
  ["docx", "office"],
  ["doc", "office"],
  ["pptx", "office"],
  ["ppt", "office"],
  // Source and config. The list is deliberately long: a file the reader wrote is
  // the one they most want to see, and the fallback for a miss is only `binary`.
  ...(
    [
      "json",
      "yaml",
      "yml",
      "toml",
      "ini",
      "cfg",
      "conf",
      "env",
      "ts",
      "tsx",
      "js",
      "jsx",
      "mjs",
      "cjs",
      "py",
      "pyi",
      "rb",
      "go",
      "rs",
      "java",
      "kt",
      "kts",
      "swift",
      "c",
      "h",
      "cc",
      "cpp",
      "hpp",
      "cs",
      "php",
      "pl",
      "lua",
      "r",
      "scala",
      "sh",
      "bash",
      "zsh",
      "fish",
      "ps1",
      "sql",
      "css",
      "scss",
      "less",
      "xml",
      "graphql",
      "gql",
      "proto",
      "tf",
      "dockerfile",
      "gradle",
      "diff",
      "patch",
    ] as const
  ).map((ext): [string, PreviewKind] => [ext, "code"]),
]);

/** The kinds that live under `text/plain`. A name may move a file between these
 *  and nowhere else, so text the server vouched for stays text. */
const TEXT_FAMILY = new Set<PreviewKind>(["csv", "markdown", "code", "text"]);

/** Whether `kind` is text a person reads and writes as text: what a file tab
 *  offers to edit. A table (`csv`) is text too, but it is drawn as a table. */
export function isEditableText(kind: PreviewKind): boolean {
  return kind !== "csv" && TEXT_FAMILY.has(kind);
}

/** The file's last extension, lowercased, or `""` for a name without one. A
 *  leading dot is the whole name of a dotfile, not an extension. */
export function extensionOf(name: string): string {
  const base = name.slice(Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\")) + 1);
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return "";
  return base.slice(dot + 1).toLowerCase();
}

function kindFromMime(essence: string): PreviewKind | null {
  const named = MIME_KINDS.get(essence);
  if (named) return named;
  if (essence.startsWith("image/")) return "image";
  if (essence.startsWith("audio/")) return "audio";
  if (essence.startsWith("video/")) return "video";
  if (essence === "text/plain") return "text";
  if (essence.startsWith("text/")) return "text";
  return null;
}

function kindFromExtension(name: string): PreviewKind | null {
  return EXTENSION_KINDS.get(extensionOf(name)) ?? null;
}

/** What this file is. Pure: the same facts always answer the same way, so every
 *  surface that shows files agrees about a type and a test can pin the whole table.
 *
 *  The mime decides when the server sniffed one. `text/plain` is refined by the
 *  name within the text family. An uninformative mime — the octet-stream a writer
 *  that set no content type leaves behind — hands the decision to the name. */
export function previewKindFor(facts: PreviewFacts): PreviewKind {
  const essence = mimeEssence(facts.mime);

  if (!UNINFORMATIVE_MIMES.has(essence)) {
    const fromMime = kindFromMime(essence);
    if (fromMime === "text") {
      const refined = kindFromExtension(facts.name);
      return refined && TEXT_FAMILY.has(refined) ? refined : "text";
    }
    // A mime the server DID place is evidence even when we draw no renderer for
    // it: `application/zip` on a file named `.md` is an archive, not a note. Only
    // a mime that says nothing hands the decision to the name.
    return fromMime ?? "binary";
  }

  return kindFromExtension(facts.name) ?? "binary";
}
