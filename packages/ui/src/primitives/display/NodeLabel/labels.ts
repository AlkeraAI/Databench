// What a Files node is CALLED where a person reads it — the one rule every surface
// (a listing, a breadcrumb, a picker, a location, a tab title, a path) goes through.
//
// A member's home is stored under its owner's id, which is an address and never a
// label: it reads as the owner's current display name, which the server resolves
// on every read and sends as the `home` facet. An object (a chat, a template)
// reads as its current title. Everything else reads as its own name.

/** The fields of a Files item the label is read from — structural, so any surface's
 *  item shape fits without this package knowing the API types. */
export interface NodeLabelSource {
  readonly name?: string | null;
  readonly nameDisplay?: string | null;
  readonly object?: { readonly title?: string | null } | null;
  readonly home?: { readonly owner_name?: string | null } | null;
}

/** The home a path runs through, as the server names it on `pathHome`. */
export interface PathHomeSource {
  readonly owner_id?: string | null;
  readonly owner_name?: string | null;
}

export interface NodeLabelText {
  /** The label, unescaped: the caller escapes it for display as it escapes any name. */
  readonly text: string;
  /** The node is a member's home, drawn with the home mark. */
  readonly home: boolean;
}

/** What a home is called when the server sent no name for its owner. Never their address. */
export const HOME_FALLBACK_LABEL = "Member";

/** The label a node is read by. */
export function nodeLabel(node: NodeLabelSource): NodeLabelText {
  if (node.home) {
    const owner = node.home.owner_name?.trim() ?? "";
    return { text: owner === "" ? HOME_FALLBACK_LABEL : owner, home: true };
  }
  const title = node.object?.title?.trim() ?? "";
  if (title !== "") return { text: node.object?.title ?? title, home: false };
  return { text: node.nameDisplay || node.name || "", home: false };
}

/** `path` with the segment that is a home read as its owner's name.
 *
 *  The stored path names a home by its owner's id: `/home/<id>/Chats`. A path cut
 *  above the home by the reader's access starts AT the home (`<id>/Chats`), so the
 *  first segment that is the owner's id is the one replaced, wherever it sits. */
export function pathLabel(path: string, pathHome: PathHomeSource | null | undefined): string {
  const owner = pathHome?.owner_id ?? "";
  if (owner === "") return path;
  const segments = path.split("/");
  const at = segments.indexOf(owner);
  if (at < 0) return path;
  const name = pathHome?.owner_name?.trim() ?? "";
  segments[at] = name === "" ? HOME_FALLBACK_LABEL : name;
  return segments.join("/");
}
