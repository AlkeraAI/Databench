// Links the app hands out for sharing name the org they were copied in.
//
// One login can belong to several orgs, and a link is opened by whoever it was
// sent to, in whichever org their session happens to be acting in. Carrying
// `?org=<id>` lets the page it lands on open in the org the link belongs to
// instead of reading as "not found" in another one.

/** `url` with `org=<orgId>` in its query, replacing any `org` it already
 *  carried and keeping every other parameter and the fragment exactly as they
 *  were. With no org, the url is returned unchanged. */
export function withOrg(url: string, orgId: string | null | undefined): string {
  if (!orgId || !url) return url;
  const hashAt = url.indexOf("#");
  const head = hashAt === -1 ? url : url.slice(0, hashAt);
  const fragment = hashAt === -1 ? "" : url.slice(hashAt);
  const queryAt = head.indexOf("?");
  const path = queryAt === -1 ? head : head.slice(0, queryAt);
  const query = queryAt === -1 ? "" : head.slice(queryAt + 1);
  const kept = query
    .split("&")
    .filter((pair) => pair !== "" && decodeName(pair.split("=")[0] ?? "") !== "org");
  kept.push(`org=${encodeURIComponent(orgId)}`);
  return `${path}?${kept.join("&")}${fragment}`;
}

function decodeName(name: string): string {
  try {
    return decodeURIComponent(name.replace(/\+/g, " "));
  } catch {
    return name;
  }
}
