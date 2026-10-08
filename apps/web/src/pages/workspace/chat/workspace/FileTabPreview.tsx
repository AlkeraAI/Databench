/**
 * The file's bytes, drawn without flinching when they change.
 *
 * An agent rewrites the file it is working on many times a minute. Each rewrite
 * buys a new grant and re-reads the bytes, and the honest report of that read is
 * `loading` — which the preview surface draws as a notice where the file was.
 * Left alone that is a blank frame and a lost scroll position several times a
 * minute, on a pane whose whole purpose is watching a file being written.
 *
 * So a version that is on its way is not a version that is missing: the last
 * copy this tab actually drew stands in until the next one lands, reported at
 * the version it really belongs to so a renderer that reloads on the version
 * does not reload to bytes nobody has yet. The reader is told about the change
 * by the tab's own live line, not by the file vanishing.
 *
 * Three things bound it, and each is a correctness claim rather than a nicety:
 * only `loading` is covered — a file that has gone, is too large, or was refused
 * says so rather than showing a copy that is no longer true; the kept copy is
 * stamped with the node it came from, so switching tabs can never paint one
 * file's bytes under another's name; and the copy is recorded in an effect,
 * after the render that used it, which is what lets the very render that turns
 * `loading` still find it.
 */

import { useEffect, useRef } from "react";

import type { PreviewContent } from "@alkera/ui";

import type { PreviewBytes } from "@/pages/workspace/files/preview/usePreviewContent";

interface Kept {
  nodeId: string;
  content: PreviewContent;
  version: string;
  /** What was said over that copy: an older copy stays called one. */
  notice?: string;
}

/**
 * The bytes to draw: the ones that arrived, or — while the next version is being
 * bought — the last ones this node actually showed.
 */
export function useSteadyPreview(bytes: PreviewBytes, nodeId: string | undefined): PreviewBytes {
  const kept = useRef<Kept | null>(null);

  useEffect(() => {
    if (nodeId === undefined || bytes.status !== "ready") return;
    kept.current = {
      nodeId,
      content: bytes.content,
      version: bytes.version,
      notice: bytes.notice,
    };
  }, [bytes.content, bytes.notice, bytes.status, bytes.version, nodeId]);

  const held = kept.current;
  if (
    bytes.status !== "loading" ||
    nodeId === undefined ||
    held === null ||
    held.nodeId !== nodeId
  ) {
    return bytes;
  }
  return {
    ...bytes,
    content: held.content,
    version: held.version,
    notice: held.notice,
    status: "ready",
  };
}
