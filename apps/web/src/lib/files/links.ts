// "Get a link to this" — what a row's link actually points at.
//
// For almost every row there is one answer: the Files page, opened on that node.
// A chat is the exception, and the reason this is a registry rather than a
// branch: a chat row is a conversation AND a folder of files at the same time,
// so a person sharing one has to be asked which they meant. Other object types
// will want the same, and none of them should have to edit the share dialog to
// get it — they register their targets and the dialog renders whatever came
// back.
//
// An unregistered type — or one whose builder answers nothing — still gets the
// Files link. A Share menu with no link in it is the one outcome that is never
// right.

import { activeOrgId } from "@/api/activeOrg";
import type { Item } from "@/api/files";
import { withOrg } from "@/lib/orgLink";

import { CHAT_FILES_NODE_KEY } from "./chatFolder";

/** One thing a link can point at. `id` says which of the two readings it is, so
 *  a caller can prefer one without matching on the label. */
export interface LinkTarget {
  readonly id: "page" | "files";
  readonly label: string;
  readonly href: string;
}

/** Build the targets for one row. `origin` is passed in rather than read, so the
 *  builder stays pure and a test can pin the whole URL. */
export type LinkTargetBuilder = (item: Item, origin: string) => LinkTarget[];

const BUILDERS = new Map<string, LinkTargetBuilder>();

/** Teach the link menu about an object type. Registering the same type twice
 *  replaces the builder, so a module re-imported by a hot reload does not stack
 *  two of them. */
export function registerLinkTargets(objectType: string, build: LinkTargetBuilder): void {
  BUILDERS.set(objectType, build);
}

/** The Files page, opened on this node — what every row has, and the floor a
 *  builder that answers nothing falls back to. */
function filesTarget(item: Item, origin: string): LinkTarget {
  return {
    id: "files",
    label: item.kind === "folder" ? "Link to folder" : "Link to file",
    href: `${origin}/files/${item.id}`,
  };
}

/** Every link this row offers, best first. Never empty. */
export function linkTargetsFor(item: Item, origin: string = window.location.origin): LinkTarget[] {
  const type = item.object?.type;
  const built = type ? BUILDERS.get(type)?.(item, origin) : undefined;
  return built && built.length > 0 ? built : [filesTarget(item, origin)];
}

/** The link a copy button copies when nobody chose between them. */
export function linkFor(item: Item, origin: string = window.location.origin): string {
  return linkTargetsFor(item, origin)[0].href;
}

registerLinkTargets("chat", (item, origin) => {
  // The conversation first: it is what a person means by "the chat", and the
  // files are reachable from it. `web_url` is the server's — a deployment with
  // no frontend base URL configured sends none, and a target with an empty href
  // would copy an empty string.
  const page = item.object?.web_url;
  const targets: LinkTarget[] = [];
  if (typeof page === "string" && page !== "") {
    targets.push({ id: "page", label: "Link to chat", href: page });
  }
  // The chat's files live at the working directory the server named on the
  // facet, never at a node derived from a name; a chat from before it had one
  // keeps its files at the chat folder itself.
  const named = item.object?.metadata?.[CHAT_FILES_NODE_KEY];
  const node = typeof named === "string" && named !== "" ? named : item.id;
  targets.push({ id: "files", label: "Link to files", href: `${origin}/files/${node}` });
  return targets;
});

/**
 * Put `text` on the clipboard and say whether it got there.
 *
 * "Copied" is reported only once the write has RESOLVED. A clipboard write is
 * permission-gated and asynchronous; a dialog that flips its button on the call
 * tells someone their link is on the clipboard when the browser has just
 * refused it, and they paste the previous one somewhere it does not belong.
 */
export async function copyText(text: string): Promise<"copied" | "failed"> {
  try {
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) return "failed";
    await clipboard.writeText(text);
    return "copied";
  } catch {
    return "failed";
  }
}

/**
 * Copy a link and say whether it got there.
 *
 * Given a row, the link is the one its copy button means (its first target);
 * given an href, that link (the share dialog offers each target). Either way it
 * names the org it was copied in, so it opens there for whoever it is sent to.
 */
export function copyLinkTo(subject: Item | string): Promise<"copied" | "failed"> {
  const href = typeof subject === "string" ? subject : linkFor(subject);
  return copyText(withOrg(href, activeOrgId()));
}
