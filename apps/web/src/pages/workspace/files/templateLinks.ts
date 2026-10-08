// What a link to a chat template points at.
//
// A template is two things at once for the same reason a chat is — a page
// carrying the brief its author wrote, and a folder of the files a new chat
// starts with — so "copy a link to this" has two honest answers and the person
// sharing it has to be asked which they meant. That is what the link registry
// exists for: this module teaches it about templates without the share dialog
// learning a second branch.

import type { Item } from "@/api/files";

import { CHAT_FILES_NODE_KEY, TEMPLATE_OBJECT_TYPE } from "@/lib/files/chatFolder";
import { registerLinkTargets, type LinkTarget } from "@/lib/files/links";

/** The links a template row offers, best first. Exported so a test names the
 *  builder rather than reaching through the registry for it. */
export function templateLinkTargets(item: Item, origin: string): LinkTarget[] {
  const targets: LinkTarget[] = [];
  // The page first: it is what a person means by "the template" — the brief,
  // and the button that starts a chat from it. A deployment with no frontend
  // base URL configured sends no `web_url`, and a target with an empty href
  // would copy an empty string.
  const page = item.object?.web_url;
  if (typeof page === "string" && page !== "") {
    targets.push({ id: "page", label: "Link to template", href: page });
  }
  // The files live at the working folder the server named on the facet, never
  // at a node derived from a name; a template that names none keeps them at the
  // template folder itself.
  const named = item.object?.metadata?.[CHAT_FILES_NODE_KEY];
  const node = typeof named === "string" && named !== "" ? named : item.id;
  targets.push({ id: "files", label: "Link to files", href: `${origin}/files/${node}` });
  return targets;
}

registerLinkTargets(TEMPLATE_OBJECT_TYPE, templateLinkTargets);
