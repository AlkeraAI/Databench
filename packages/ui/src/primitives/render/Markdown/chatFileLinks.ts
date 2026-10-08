// The files a message links to in its own chat folder, as the transcript
// renders them: every link `renderInline` opens as a chat file, in every block
// whose text goes through it, whose target the path rule places inside the
// chat. Pictures on a line of their own are image blocks, not links.
//
// This is `alkera_core.chat_paths.chat_file_links` — what the Slack thread
// attaches beside the pictures — spelled for the browser. Both are pinned
// against `api-core/tests/fixtures/chat_paths/links.json`, so a file the chat
// opens from a link is one the thread carries too.

import { chatRelativePath } from "./chatPaths";
import { parseMarkdownBlocks, type MarkdownBlock, type MarkdownListBlock } from "./markdownBlocks";
import { inlineFileLinks } from "./markdownInline";

export interface ChatFileLinkRef {
  label: string;
  target: string;
  /** The path below the chat's working folder (or its own folder, for a box path). */
  path: string;
}

function listTexts(list: MarkdownListBlock): string[] {
  const out: string[] = [];
  list.items.forEach((item, index) => {
    out.push(item);
    for (const nested of list.nested?.[index] ?? []) out.push(...listTexts(nested));
  });
  return out;
}

function inlineTexts(block: MarkdownBlock): string[] {
  switch (block.kind) {
    case "heading":
    case "paragraph":
    case "quote":
      return [block.text];
    case "list":
      return listTexts(block);
    case "table":
      return [...block.headers, ...block.rows.flat()];
    default:
      return [];
  }
}

export function chatFileLinks(markdown: string, chatId: string | null): ChatFileLinkRef[] {
  const out: ChatFileLinkRef[] = [];
  const seen = new Set<string>();
  for (const block of parseMarkdownBlocks(markdown)) {
    for (const text of inlineTexts(block)) {
      for (const link of inlineFileLinks(text)) {
        const path = chatRelativePath(link.target, chatId);
        if (path === null || seen.has(path)) continue;
        seen.add(path);
        out.push({ ...link, path });
      }
    }
  }
  return out;
}
