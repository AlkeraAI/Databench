// The browser tab's name, composed in one place.
//
// A title is `<the thing> · <section> · <product>`, dropping whatever is absent:
// a hub page is `Files · Alkera`, a page about one thing is
// `What connections do you see? · Chat · Alkera`. The section is the SAME string
// the masthead names the page with (AppLayout reads `sectionTitleFor` too), so a
// page renamed in the nav is renamed in the tab with it.
//
// The product name is per-deployment configuration, never a literal: until the
// public config answers, the tab carries the section alone rather than a brand a
// white-labeled install does not use.
//
// A page that is about one thing declares that thing with `useSpecificTitle`;
// the claim is released on unmount, so the next page starts from its section.
// Routes whose subject already sits in the query cache (a chat, a Files folder)
// need no page edit at all — `specificFromCache` reads the row the page fetched,
// which is also why a rename retitles the tab the moment the cache row changes.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";

import { displayNameOf } from "@/lib/files/columns";

import type { ChatSessionList, ChatSessionRead } from "../api/cloudChat/transport";
import { usePublicConfig } from "../api/config";
import type { Item } from "../api/files";
import { keys } from "../api/keys";
import { PORTAL_ROUTES } from "./extensions/portal";
import { hasChildren, portalNav, type NavLeaf } from "./nav";

/** Every leaf of the sidebar as the shell renders it. Read when a title is asked for, never
 *  at module scope: the extension points are filled before the first render, after this
 *  module has loaded. */
const flatNav = (): NavLeaf[] =>
  portalNav().flatMap((g) => g.items.flatMap((i) => (hasChildren(i) ? i.children : [i])));

/** Pages whose name is not a nav leaf, by exact path: the ones reached from the
 *  account popover rather than the sidebar. Without an entry the prefix match
 *  below would name them after a PARENT nav item. */
export const PATH_TITLES: Record<string, string> = {
  "/settings/profile": "Profile",
  "/preferences": "Preferences",
  "/settings/organization": "Organization settings",
  "/verify-email": "Verify email",
  // The platform console's landing page shares the nav label "Overview" with the workspace's own;
  // the tab and the masthead say which overview this is.
  "/admin": "Admin overview",
};

/** Drill-in pages under a nav destination, matched by prefix since their paths
 *  carry an id. */
export const PREFIX_TITLES: [prefix: string, title: string][] = [
  // Every org-settings tab is the same page; the tab strip names which one, so the masthead
  // title holds still rather than flickering between four words as the reader moves across it.
  // The tab's own name reaches the browser tab as the specific instead.
  // Machines sits under the org-settings path but is its own nav page (and a machine's own
  // page under it), so it is named for itself. Listed first: the first matching prefix wins.
  ["/settings/organization/machines", "Machines"],
  ["/settings/organization/", "Organization settings"],
  // A saved object is opened from Files and is a page of it; without this the
  // masthead fell through to "Overview".
  ["/objects/", "Files"],
  // A workspace's page names what it is; the tab names which one.
  ["/workspaces/", "Workspace"],
];

/** What this route is a page OF — the masthead's title and the tab's middle part.
 *  `null` where no nav leaf or map entry claims the path (an auth page, a 404):
 *  those pages name themselves. */
export function sectionTitleFor(pathname: string): string | null {
  const mapped = PATH_TITLES[pathname];
  if (mapped) return mapped;
  const detail = PREFIX_TITLES.find(([prefix]) => pathname.startsWith(prefix));
  if (detail) return detail[1];
  const registered = registeredTitleFor(pathname);
  if (registered) return registered;
  const leaves = flatNav();
  const exact = leaves.find((l) => l.to === pathname);
  if (exact) return exact.label;
  const prefix = leaves.filter((l) => l.to !== "/" && pathname.startsWith(l.to)).sort(
    (a, b) => b.to.length - a.to.length,
  )[0];
  return prefix?.label ?? null;
}

/** The title an extension's route declares: by exact path, or for a path with a `:param`,
 *  by the static prefix before it. */
function registeredTitleFor(pathname: string): string | null {
  for (const route of PORTAL_ROUTES.items()) {
    if (!route.title) continue;
    const param = route.path.indexOf(":");
    if (param < 0 ? pathname === route.path : pathname.startsWith(route.path.slice(0, param))) return route.title;
  }
  return null;
}

const SEPARATOR = " · ";
/** Long enough for a real chat title, short enough that the tab still shows the
 *  beginning of it rather than an unreadable sliver. */
const SPECIFIC_MAX = 80;

/** One line, no runs of whitespace — a chat title is user-written and may carry
 *  newlines, which a tab renders as an empty gap. */
const oneLine = (value: string | null | undefined): string =>
  value ? value.replace(/\s+/g, " ").trim() : "";

export function trimSpecific(value: string): string {
  return value.length > SPECIFIC_MAX ? `${value.slice(0, SPECIFIC_MAX - 1).trimEnd()}…` : value;
}

export function composeDocumentTitle(
  specific: string | null | undefined,
  section: string | null | undefined,
  productName: string | null | undefined,
): string {
  const thing = trimSpecific(oneLine(specific));
  const where = oneLine(section);
  const parts: string[] = [];
  if (thing) parts.push(thing);
  // A page whose subject IS its section (the chat list, a folder named like its
  // place) says it once.
  if (where && where.toLowerCase() !== thing.toLowerCase()) parts.push(where);
  const product = oneLine(productName);
  if (product) parts.push(product);
  return parts.join(SEPARATOR);
}

/** The id in `/<prefix>/<id>/…`, or null when the path is the prefix itself. */
function idUnder(pathname: string, prefix: string): string | null {
  if (!pathname.startsWith(prefix)) return null;
  const id = pathname.slice(prefix.length).split("/")[0];
  return id ? decodeURIComponent(id) : null;
}

/** The subject of a route that already fetched it — read from the cache rather
 *  than fetched again, so naming the tab costs no request and follows a rename
 *  as soon as the cache row does. */
export function specificFromCache(client: QueryClient, pathname: string): string | null {
  const chatId = idUnder(pathname, "/chat/");
  if (chatId) {
    const one = client.getQueryData<ChatSessionRead>(keys.chats.one(chatId));
    if (one?.title) return one.title;
    const list = client.getQueryData<ChatSessionList>(keys.chats.all);
    return list?.items.find((row) => row.id === chatId)?.title ?? null;
  }
  const nodeId = idUnder(pathname, "/files/");
  if (nodeId && nodeId !== "trash") {
    // Not the row's `name`: a chat or a report is a `<Title>.alkerachat` FOLDER
    // whose filesystem name was minted once from the title and never rewritten,
    // so reading it named the tab after a uuid the reader never typed and kept
    // naming it that after every rename. Derive it the way the rows, the
    // breadcrumb and the right pane do.
    const item = client.getQueryData<Item>(keys.files.item(nodeId));
    return item ? displayNameOf(item) || null : null;
  }
  return null;
}

function useCachedSpecific(pathname: string): string | null {
  const client = useQueryClient();
  // The cache emits while another component is RENDERING — react-query builds a
  // query entry, and a page seeds one, with its caller still on the render stack.
  // Handing that straight to the store schedules an update on this component from
  // inside somebody else's render, which React refuses to do quietly ("Cannot
  // update a component while rendering a different component"). The notification
  // is taken a microtask later instead: the render that emitted it has finished by
  // then, the snapshot is re-read on every render regardless, and a tab title one
  // microtask late is not a difference anyone can see.
  const subscribe = useCallback(
    (onChange: () => void) => {
      let queued = false;
      return client.getQueryCache().subscribe(() => {
        if (queued) return;
        queued = true;
        queueMicrotask(() => {
          queued = false;
          onChange();
        });
      });
    },
    [client],
  );
  const read = useCallback(() => specificFromCache(client, pathname), [client, pathname]);
  return useSyncExternalStore(subscribe, read, read);
}

interface SpecificTitleApi {
  claim: (owner: symbol, value: string | null) => void;
  release: (owner: symbol) => void;
}

const SpecificTitleContext = createContext<SpecificTitleApi | null>(null);

/** Names what this page is about. The claim is the page's own — released when it
 *  unmounts, so a page that declares nothing shows its section. Passing
 *  null/undefined (nothing loaded yet) is the same as declaring nothing. */
export function useSpecificTitle(specific: string | null | undefined): void {
  const api = useContext(SpecificTitleContext);
  const owner = useRef<symbol | null>(null);
  owner.current ??= Symbol("page title");
  const token = owner.current;
  useEffect(() => {
    if (!api) return undefined;
    api.claim(token, specific ?? null);
    return () => api.release(token);
  }, [api, token, specific]);
}

function TitleWriter({ specific }: { specific: string | null }) {
  const { pathname } = useLocation();
  const productName = usePublicConfig().data?.product_name;
  const cached = useCachedSpecific(pathname);
  const title = composeDocumentTitle(specific ?? cached, sectionTitleFor(pathname), productName);
  useEffect(() => {
    // Nothing to say (an unnamed route before the config answers) leaves the
    // document's own title standing rather than blanking the tab.
    if (title) document.title = title;
  }, [title]);
  return null;
}

/** The single writer of `document.title`, wrapping the routed tree. */
export function DocumentTitle({ children }: { children: ReactNode }) {
  const [claim, setClaim] = useState<{ owner: symbol; value: string | null } | null>(null);
  const api = useMemo<SpecificTitleApi>(
    () => ({
      claim: (owner, value) =>
        setClaim((prev) =>
          prev?.owner === owner && prev.value === value ? prev : { owner, value },
        ),
      // Only the page that holds the claim may drop it: on a route change the
      // outgoing page's cleanup can run after the incoming page has claimed.
      release: (owner) => setClaim((prev) => (prev?.owner === owner ? null : prev)),
    }),
    [],
  );
  return (
    <SpecificTitleContext.Provider value={api}>
      <TitleWriter specific={claim?.value ?? null} />
      {children}
    </SpecificTitleContext.Provider>
  );
}
