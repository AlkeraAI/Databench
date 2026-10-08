import {
  IconFile,
  IconFileText,
  IconFileTypePdf,
  IconMovie,
  IconMusic,
  IconPhoto,
} from "@tabler/icons-react";
import type { ReactElement, ReactNode } from "react";

import { Button } from "../../primitives/controls/Button";
import { extensionOf, previewKindFor, type PreviewKind } from "../kind";
import type { PreviewProps } from "../types";
import { formatBytes } from "../size";

// The card a file gets when no renderer claims its type, when it is too big to
// fetch, or when the bytes never arrived. It is the one preview that always
// works, so it carries what a person needs in order to decide what to do — the
// name, what the server says the bytes are, how big they are, why there is no
// picture — and the way out, Download.


/** The formats a person will recognise by name but that no browser opens. Saying
 *  which one it is beats a mime string the reader did not ask about. */
const OFFICE_NAMES: Record<string, string> = {
  xlsx: "an Excel workbook",
  xls: "an Excel workbook",
  docx: "a Word document",
  doc: "a Word document",
  pptx: "a PowerPoint presentation",
  ppt: "a PowerPoint presentation",
};

/** A file the machine holding the folder has not written back yet. The host says
 *  so outright, from whether any bytes were ever committed — a genuinely empty
 *  file is synced, and size and timestamps are not evidence either way. */
export function isUnsynced(facts: PreviewProps["facts"]): boolean {
  return facts.synced === false;
}

/** What the card says about a file it cannot draw: the cause, in one line. */
function reasonFor(props: PreviewProps): string {
  const { facts } = props;
  switch (props.status) {
    case "gone":
      return "This file is no longer available";
    case "error":
      return props.error ?? "The preview could not be loaded";
    case "pending":
      break;
    default:
      break;
  }

  // Nothing has arrived yet. Saying the type is not previewable would be a lie
  // about a file whose bytes simply are not here — and it is the machine, not the
  // format, that the reader has to wait on. The host names the machine only
  // while it is bringing the bytes, so a named machine is one being fetched from.
  if (props.status === "pending" || isUnsynced(facts)) {
    if (props.status === "pending" && props.pendingReason) return props.pendingReason;
    return facts.machine ? `Fetching from ${facts.machine}…` : "Not synced yet";
  }

  if (previewKindFor(facts) === "office") {
    const office = OFFICE_NAMES[extensionOf(facts.name)];
    return `No in-browser preview yet for ${office ?? "this format"}`;
  }

  return "Preview is not available for this type";
}

type FileKind = "image" | "video" | "audio" | "pdf" | "text" | "file";

const GLYPHS: Record<FileKind, typeof IconFile> = {
  image: IconPhoto,
  video: IconMovie,
  audio: IconMusic,
  pdf: IconFileTypePdf,
  text: IconFileText,
  file: IconFile,
};

/** The glyph each kind draws. A video too large to play still reads as a video. */
const GLYPH_FOR: Record<PreviewKind, FileKind> = {
  image: "image",
  svg: "image",
  pdf: "pdf",
  html: "text",
  csv: "text",
  markdown: "text",
  code: "text",
  text: "text",
  audio: "audio",
  video: "video",
  office: "file",
  binary: "file",
};

/** A line where a pane would be — the bytes have not arrived, or there are none
 *  to arrive. Announced politely so a reader is not left with blank space. */
export function PreviewNotice({ children }: { children: ReactNode }): ReactElement {
  return (
    <p className="alk-preview__notice" role="status">
      {children}
    </p>
  );
}

export function BinaryFallback(props: PreviewProps): ReactElement {
  const { facts } = props;
  const unsynced = props.status === "pending" || isUnsynced(facts);
  // The glyph follows what the file IS, so a PNG nobody sniffed still reads as a
  // picture rather than as a generic file.
  const kind = GLYPH_FOR[previewKindFor(facts)];
  const Glyph = GLYPHS[kind];
  return (
    <div
      className="alk-preview-fallback"
      data-testid="preview-fallback"
      data-kind={kind}
      data-unsynced={unsynced ? "" : undefined}
    >
      <Glyph className="alk-preview-fallback__glyph" size="var(--alkIconLg)" aria-hidden />
      <p className="alk-preview-fallback__name">{facts.name}</p>
      {/* A mime nobody set and a size of zero are facts about the sync, not about
          the file — repeating them would only tell the reader what is missing. */}
      {!unsynced && (
        <p className="alk-preview-fallback__facts">{`${facts.mime} · ${formatBytes(facts.size)}`}</p>
      )}
      <p className="alk-preview-fallback__reason">{reasonFor(props)}</p>
      <Button variant="secondary" fill="outline" size="sm" onClick={props.onDownload}>
        Download
      </Button>
    </div>
  );
}
