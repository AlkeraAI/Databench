// A plan is a document, so it opens where documents open.
//
// The chat-ui frontend hands the plan to the host, which shows it as a read-only
// markdown tab in the editor. That puts a plan next to the files it talks
// about, with the editor's own split, search, and outline already working,
// instead of covering the conversation it belongs to with a page.
//
// This module is the whole webview half of the seam; its host twin is
// the editor extension's plan document. Today the document is
// virtual and read-only. The next steps land in these two files: annotating a
// plan, and writing a reviewer's comments back onto the turn that proposed it.

import { currentBrand } from "@alkera/ui";
import type { QuestionConversationPart } from "@alkera/chat-model";
import { chatHost, type ChatDocumentRequest } from "./data";


/** Shown when a plan approval carries no plan text at all, so the tab says what
 *  happened rather than opening empty. */
const NO_PLAN_TEXT = "This approval arrived with no plan text.";

/** One title is one document, so reopening a plan refreshes that tab instead of
 *  stacking another one up. */
export function planDocumentOf(part: QuestionConversationPart, chatTitle: string): ChatDocumentRequest {
  const markdown = part.planMarkdown?.trim() || part.questions[0]?.question?.trim() || NO_PLAN_TEXT;
  const name = chatTitle.trim() || currentBrand().productName;
  return { title: `${name} plan`, markdown };
}

/** Opens the plan in the host's editor. False where there is no editor to open
 *  it in (the browser preview), so the caller keeps its own page there. */
export function openPlanDocument(document: ChatDocumentRequest): boolean {
  if (chatHost().kind !== "vscode") return false;
  void chatHost().openPlanDocument(document);
  return true;
}
