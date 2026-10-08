import { readFileSync } from "node:fs";
import { join } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { enterOrg, forgetActiveOrg } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import { SHARES_CONNECTIONS_NOTE, ShareDialog } from "@/pages/workspace/files/ShareDialog";

// Whether loading the dialog's module loads the dialog's sheet. The dialog opens
// from chunks that never load the files browser's stylesheet (the chat header,
// a chat's file tab), so a sheet it does not import itself is a sheet those
// surfaces render without — raw browser controls in one run-on line.
const shareSheet = vi.hoisted(() => ({ loaded: false }));
vi.mock("@/pages/workspace/files/share-dialog.css", () => {
  shareSheet.loaded = true;
  return {};
});

// The share dialog against a stubbed drive. What is pinned is what a person can
// actually do with it and what the server's rules make impossible: an inherited
// grant is shown and refused HERE with the folder it came from, a role change is
// one request whose row is held until it settles, and every write carries the
// idempotency key and the version fence the Files routes demand.

const DRIVE = "drv_1";
const NODE = "nd_a";
const PARENT = "nd_parent";

interface Call {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

let calls: Call[] = [];
/** Direct grants, as the server would answer them. Mutated by the stub so a
 *  write is visible to the refetch that follows it. */
let directGrants: unknown[] = [];
/** What the next permissions POST answers with; 201 unless a test says otherwise. */
let grantAnswer: { status: number; body: unknown } = { status: 201, body: { id: "sh_new" } };
let revokeAnswer: { status: number; body: unknown } = { status: 204, body: null };
/** Bumped by every write, the way a real node's etag moves under a grant. */
let etag = "7";
/** A version the node has already left: a grant naming it is refused with 412,
 *  which is what a dialog holding an etag from before someone else's write hits. */
let staleEtag: string | null = null;
/** Set by a test that needs the grant request to still be out while it looks at
 *  the dialog; resolved by `releaseGrant()`. */
let heldGrant: Promise<void> | null = null;
/** When set, a grant to somebody new adds their row and a revoke drops one,
 *  the way the server's listing answers after the write. */
let rowsFollowWrites = false;
let nextShare = 0;
let releaseGrant: () => void = () => undefined;

function holdTheGrant(): void {
  heldGrant = new Promise<void>((resolve) => {
    releaseGrant = resolve;
  });
}

const MEMBERS = [
  { user_id: "usr_dana", email: "dana@acme.test", display_name: "Dana Okafor" },
  { user_id: "usr_sam", email: "sam@acme.test", display_name: "Sam Reyes" },
  { user_id: "usr_sara", email: "sara@acme.test", display_name: "Sara Lind" },
  { user_id: "usr_admin", email: "admin@acme.test", display_name: "Alex Admin" },
];
const TEAMS = [
  { id: "team_root", name: "Acme", is_root: true, parent_team_id: null },
  { id: "team_an", name: "Analytics", is_root: false, parent_team_id: "team_root" },
];

function node(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: NODE,
    driveId: DRIVE,
    kind: "file",
    name: "quarterly.csv",
    nameDisplay: "quarterly.csv",
    parentId: PARENT,
    etag,
    ctag: "c1",
    capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
    ...over,
  };
}

let nodeOverrides: Record<string, unknown> = {};
let inheritedGrants: unknown[] = [];
/** What the directory search answers when a test wants it refused or broken;
 *  null means it answers the matches the way the server does. */
let candidatesAnswer: { status: number; body: unknown } | null = null;

/** The directory search as the server runs it: the org's people and teams
 *  whose name or email contains the fragment, in the wire's own shape. */
function directoryMatches(q: string): unknown[] {
  const text = q.trim().toLowerCase();
  if (text === "") return [];
  const people = MEMBERS.filter(
    (one) =>
      one.display_name.toLowerCase().includes(text) || one.email.toLowerCase().includes(text),
  ).map((one) => ({
    principal: { kind: "user", id: one.user_id },
    name: one.display_name,
    email: one.email,
    isOrg: false,
  }));
  const teams = TEAMS.filter((one) => one.name.toLowerCase().includes(text)).map((one) => ({
    principal: { kind: "team", id: one.id },
    name: one.name,
    email: null,
    isOrg: one.is_root,
  }));
  return [...people, ...teams];
}

/** The server's labels, as `alkera_core.files.authz.ladder` spells them for
 *  the listing: a withdrawn rung reads as the rung it behaves like. */
const SERVER_LABELS: Record<string, string> = {
  reader: "Can view",
  commenter: "Can view",
  writer: "Can edit",
  manager: "Full access",
  owner: "Owner",
};
const SERVER_SHOWN_AS: Record<string, string> = { commenter: "reader" };
/** The grants the stubbed server lets a manager move: every rung below owner. */
const SERVER_MOVABLE = new Set(["reader", "commenter", "writer", "manager"]);
const SERVER_OFFERED = ["reader", "writer", "manager"].map((role) => ({
  role,
  label: SERVER_LABELS[role],
}));

/** The node's owner, as the stubbed node names it. */
function stubOwner(): string | null {
  const attrs = nodeOverrides.attrs as { owner?: string } | undefined;
  return attrs?.owner ?? null;
}

function stubCanShare(): boolean {
  const caps = (node(nodeOverrides).capabilities ?? {}) as { can_share?: boolean };
  return caps.can_share === true;
}

/** One listing row as the server answers it to a manager: labelled, the owner
 *  marked, and a direct grant on a rung the manager holds or below movable.
 *  Whatever a test put on the row itself wins, so a test can say what the
 *  server decided for a row the stub would decide otherwise. */
function asServer(row: unknown): Record<string, unknown> {
  const given = row as {
    id?: string | null;
    role: string;
    origin?: string;
    principal: { kind: string; id: string };
  };
  const isOwner = given.principal.kind === "user" && given.principal.id === stubOwner();
  const movable =
    stubCanShare() &&
    !isOwner &&
    given.origin === "direct" &&
    typeof given.id === "string" &&
    SERVER_MOVABLE.has(given.role);
  return {
    shownRole: SERVER_SHOWN_AS[given.role] ?? given.role,
    roleLabel: isOwner ? "Owner" : (SERVER_LABELS[given.role] ?? given.role),
    isOwner,
    canChange: movable,
    canRemove: movable,
    ...(row as Record<string, unknown>),
  };
}

function stub(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The typed client hands `fetch` a built `Request`, so the method, the
      // headers and the body all live on IT, not on an init object — a stub
      // that reads only `init` records every write as a GET with no headers.
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const headers = Object.fromEntries(
        new Headers((init?.headers as HeadersInit | undefined) ?? asRequest?.headers).entries(),
      );
      let body: unknown = null;
      if (typeof init?.body === "string") body = JSON.parse(init.body);
      else if (asRequest && method !== "GET") {
        const text = await asRequest.clone().text();
        body = text === "" ? null : JSON.parse(text);
      }
      calls.push({ method, url: raw, headers, body });
      const url = new URL(raw, "http://localhost");

      const answer = (status: number, payload: unknown): Promise<Response> =>
        Promise.resolve(
          new Response(payload === null ? null : JSON.stringify(payload), {
            status,
            headers: { "content-type": "application/json" },
          }),
        );

      if (url.pathname.endsWith("/permissions")) {
        if (method === "POST") {
          if (heldGrant) await heldGrant;
          // The version fence, as the route enforces it: a grant naming a
          // version the node has left is refused, whatever the test has queued
          // as the answer for a well-fenced one.
          if (staleEtag !== null && headers["if-match"] === staleEtag) {
            return answer(412, { code: "files.precondition_failed", message: "This item changed" });
          }
          if (grantAnswer.status < 300) {
            etag = String(Number(etag) + 1);
            // The route's own upsert: a principal who already holds a grant here
            // moves to the new rung on the SAME row, so the listing the dialog
            // re-reads has one entry, not two.
            const asked = body as { principal: { kind: string; id: string }; role: string };
            const standing = directGrants.find(
              (row) =>
                (row as { principal: { kind: string; id: string } }).principal.kind ===
                  asked.principal.kind &&
                (row as { principal: { kind: string; id: string } }).principal.id ===
                  asked.principal.id,
            ) as { role: string } | undefined;
            if (standing) standing.role = asked.role;
            else if (rowsFollowWrites) {
              const member = MEMBERS.find((one) => one.user_id === asked.principal.id);
              directGrants.push({
                id: `sh_${++nextShare}`,
                principal: asked.principal,
                principalName: member?.display_name ?? asked.principal.id,
                role: asked.role,
                origin: "direct",
                grantingNodeId: null,
              });
            }
          }
          return answer(grantAnswer.status, grantAnswer.body);
        }
        const wantsEffective = url.searchParams.get("effective") === "true";
        const rows = wantsEffective ? [...directGrants, ...inheritedGrants] : directGrants;
        return answer(200, {
          value: rows.map(asServer),
          assignableRoles: stubCanShare() ? SERVER_OFFERED : [],
        });
      }
      if (url.pathname.includes("/permissions/")) {
        if (staleEtag !== null && headers["if-match"] === staleEtag) {
          return answer(412, { code: "files.precondition_failed", message: "This item changed" });
        }
        if (revokeAnswer.status < 300) etag = String(Number(etag) + 1);
        if (revokeAnswer.status < 300 && rowsFollowWrites) {
          const shareId = url.pathname.slice(url.pathname.lastIndexOf("/") + 1);
          directGrants = directGrants.filter((row) => (row as { id: string }).id !== shareId);
        }
        return answer(revokeAnswer.status, revokeAnswer.body);
      }
      if (url.pathname.endsWith("/share-candidates")) {
        if (candidatesAnswer !== null) {
          return answer(candidatesAnswer.status, candidatesAnswer.body);
        }
        // The route leaves the node's owner out: a share has nothing to give them.
        const matches = directoryMatches(url.searchParams.get("q") ?? "").filter(
          (one) => (one as { principal: { id: string } }).principal.id !== stubOwner(),
        );
        return answer(200, { value: matches });
      }
      if (url.pathname.includes(`/items/${PARENT}`)) {
        return answer(200, { ...node(), id: PARENT, kind: "folder", nameDisplay: "Papers" });
      }
      if (url.pathname.includes("/items/")) return answer(200, node(nodeOverrides));
      // The admin-only member table, as it answers the member this dialog is
      // most often opened by: refused. The picker must never depend on it.
      if (url.pathname.endsWith("/org/members")) {
        return answer(403, { code: "forbidden", message: "org admin role required" });
      }
      if (url.pathname.endsWith("/teams")) return answer(200, TEAMS);
      return answer(200, {});
    }),
  );
}

function mount(props: Partial<React.ComponentProps<typeof ShareDialog>> = {}) {
  // createQueryClient, never a bare QueryClient: the invalidation policy that
  // makes a landed write refresh the listing lives on it.
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ShareDialog driveId={DRIVE} nodeId={NODE} open onClose={() => undefined} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A row's role picker: the library Select, a trigger button a person opens. */
function rolePicker(row: HTMLElement): HTMLElement {
  return within(row).getByRole("button", { name: /^Role for / });
}

/** The form control behind a role picker — the native select the library keeps
 *  as the value's source of truth, with every option the picker lists. */
function roleControl(row: HTMLElement): HTMLSelectElement {
  const select = row.querySelector("select");
  if (!(select instanceof HTMLSelectElement)) throw new Error("the row has no role control");
  return select;
}

type User = ReturnType<typeof userEvent.setup>;

/** The option names a picker's open list shows a person, read by opening it and
 *  closing it again. */
async function listedRoles(user: User, trigger: HTMLElement): Promise<(string | null)[]> {
  await user.click(trigger);
  const list = await screen.findByRole("listbox", {
    name: trigger.getAttribute("aria-label") ?? "",
  });
  const names = within(list)
    .getAllByRole("option")
    .map((one) => one.textContent);
  await user.click(trigger);
  return names;
}

/** Pick a rung the way a person does: open the picker, click the option. */
async function pickRole(user: User, trigger: HTMLElement, label: string): Promise<void> {
  await user.click(trigger);
  const list = await screen.findByRole("listbox", {
    name: trigger.getAttribute("aria-label") ?? "",
  });
  await user.click(within(list).getByRole("option", { name: label }));
}

const INHERITED = "This access is inherited. Change it on the folder it comes from.";

describe("what the dialog is called", () => {
  it("titles a plain file with the file's own name", async () => {
    mount();
    expect(await screen.findByText("Share “quarterly.csv”")).toBeTruthy();
  });

  it("prefers a caller's name for the subject over the node's", async () => {
    mount({ subjectName: "Create a file named notes.txt" });
    expect(await screen.findByText("Share “Create a file named notes.txt”")).toBeTruthy();
    expect(screen.queryByText("Share “quarterly.csv”")).toBeNull();
  });
});

describe("sharing a chat or workspace shares its owner's connections", () => {
  it("tells the sharer, in one line", async () => {
    mount({ sharesConnections: true, subjectName: "Revenue model" });
    expect(await screen.findByText(SHARES_CONNECTIONS_NOTE)).toBeInTheDocument();
    expect(SHARES_CONNECTIONS_NOTE).toBe("People you share this with can query data through your connections.");
  });

  it("says nothing of connections when sharing a file", async () => {
    mount();
    expect(await screen.findByText("Share “quarterly.csv”")).toBeTruthy();
    expect(screen.queryByText(SHARES_CONNECTIONS_NOTE)).not.toBeInTheDocument();
  });

  it("says nothing to someone who cannot share it", async () => {
    nodeOverrides = {
      capabilities: { can_read: true, can_share: false, refusals: { share: "Not yours to share." } },
    };
    mount({ sharesConnections: true });
    expect(await screen.findByText("Not yours to share.")).toBeInTheDocument();
    expect(screen.queryByText(SHARES_CONNECTIONS_NOTE)).not.toBeInTheDocument();
  });
});

function writes(method: string): Call[] {
  return calls.filter((call) => call.method === method && call.url.includes("/permissions"));
}

beforeEach(() => {
  rowsFollowWrites = false;
  nextShare = 0;
  etag = "7";
  staleEtag = null;
  heldGrant = null;
  releaseGrant = () => undefined;
  nodeOverrides = {};
  candidatesAnswer = null;
  grantAnswer = { status: 201, body: { id: "sh_new" } };
  revokeAnswer = { status: 204, body: null };
  directGrants = [
    {
      id: "sh_dana",
      principal: { kind: "user", id: "usr_dana" },
      principalName: "Dana Okafor",
      role: "reader",
      origin: "direct",
      grantingNodeId: null,
    },
  ];
  inheritedGrants = [
    {
      id: null,
      principal: { kind: "team", id: "team_an" },
      principalName: "Analytics",
      role: "writer",
      origin: "inherited",
      grantingNodeId: PARENT,
    },
  ];
  stub();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the share dialog", () => {
  it("shows a direct grant as editable and an inherited one as the folder's business", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Analytics")).toBeInTheDocument());

    const rows = screen.getAllByRole("listitem");
    const direct = rows.find((row) => row.getAttribute("data-origin") === "direct");
    const inherited = rows.find((row) => row.getAttribute("data-origin") === "inherited");
    expect(direct).toBeDefined();
    expect(inherited).toBeDefined();

    // The direct row can be changed and taken away here.
    expect(rolePicker(direct as HTMLElement)).toBeEnabled();
    expect(within(direct as HTMLElement).getByRole("button", { name: /Remove/ })).toBeEnabled();

    // The inherited one cannot: this is not where it was made.
    expect(rolePicker(inherited as HTMLElement)).toBeDisabled();
    expect(
      within(inherited as HTMLElement).getByRole("button", { name: /Remove/ }),
    ).toBeDisabled();
    // …and it says why on the controls themselves, not as a "via <folder>" line
    // under the name (that read as a person's handle when the ancestor was a home).
    expect(within(inherited as HTMLElement).queryByText(/^via /)).toBeNull();
    expect(within(inherited as HTMLElement).queryByRole("link")).toBeNull();
    // Both held controls carry the sentence to a screen reader, and hovering
    // them shows it.
    const held = [
      rolePicker(inherited as HTMLElement),
      within(inherited as HTMLElement).getByRole("button", { name: /Remove/ }),
    ];
    for (const control of held) {
      expect(control).toHaveAccessibleDescription(INHERITED);
      expect(control.closest("[title]")).toHaveAttribute("title", INHERITED);
    }
    // …and only there: the direct row's controls are not described as inherited.
    expect(rolePicker(direct as HTMLElement)).not.toHaveAccessibleDescription(INHERITED);
    expect(rolePicker(direct as HTMLElement).closest("[title]")).toBeNull();
  });

  it("labels a rung the way the product says it, not the way the wire spells it", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const direct = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "direct") as HTMLElement;
    const select = roleControl(direct);
    expect(select.value).toBe("reader");
    expect(rolePicker(direct)).toHaveTextContent("Can view");
    // The WHOLE list, not a subset: the ladder this build offers is three rungs
    // and putting a withdrawn one back is a product decision, not a refactor.
    expect(
      Array.from(select.options).map((option) => `${option.value}:${option.textContent}`),
    ).toEqual(["reader:Can view", "writer:Can edit", "manager:Full access"]);
    // …and the list a person opens says the same three things.
    expect(await listedRoles(user, rolePicker(direct))).toEqual([
      "Can view",
      "Can edit",
      "Full access",
    ]);
  });

  it("never offers commenting — on a row, or to a new person", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Analytics")).toBeInTheDocument());

    const forNew = screen.getByLabelText("Role for new people");
    expect(await listedRoles(user, forNew)).toEqual(["Can view", "Can edit", "Full access"]);
    // Neither the rung nor its old name survives anywhere a person can read.
    expect(screen.queryByText(/comment/i)).toBeNull();
    // Every picker in the dialog: the new-person one and each row's.
    const selects = Array.from(document.querySelectorAll("select"));
    expect(selects).toHaveLength(3);
    for (const select of selects) {
      expect(Array.from(select.options).map((one) => one.value)).not.toContain("commenter");
    }
  });

  it("shows a grant left on the withdrawn rung as Can view, and still moves it", async () => {
    const user = userEvent.setup();
    // The server keeps the rung; this is a grant made before the portal stopped
    // offering it. It must not render as a blank select, as a second name for
    // read access, or as a row nobody can change.
    directGrants = [
      {
        id: "sh_dana",
        principal: { kind: "user", id: "usr_dana" },
        principalName: "Dana Okafor",
        role: "commenter",
        origin: "direct",
        grantingNodeId: null,
      },
    ];
    inheritedGrants = [];
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const row = screen
      .getAllByRole("listitem")
      .find((one) => one.getAttribute("data-origin") === "direct") as HTMLElement;
    const select = roleControl(row);

    expect(select.value).toBe("reader");
    expect(select.selectedOptions[0]?.textContent).toBe("Can view");
    expect(rolePicker(row)).toHaveTextContent("Can view");
    expect(Array.from(select.options).map((one) => one.value)).toEqual([
      "reader",
      "writer",
      "manager",
    ]);
    expect(rolePicker(row)).toBeEnabled();

    // And moving it off sends the rung that was picked, exactly as any other row
    // does — the fold is a display rule, not a write rule.
    await pickRole(user, rolePicker(row), "Can edit");
    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0]?.body).toMatchObject({
      principal: { kind: "user", id: "usr_dana" },
      role: "writer",
    });
    expect(writes("DELETE")).toHaveLength(0);
  });

  it("grants a new person with an idempotency key and the node's version", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Name, email or team"), "sam");
    await user.click(await screen.findByRole("option", { name: /Sam Reyes/ }));
    await pickRole(user, screen.getByLabelText("Role for new people"), "Can edit");
    await user.click(screen.getByRole("button", { name: "Share" }));

    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    const [post] = writes("POST");
    expect(post?.body).toMatchObject({
      principal: { kind: "user", id: "usr_sam" },
      role: "writer",
    });
    // Both fences the Files mutation routes refuse a request without.
    expect(post?.headers["idempotency-key"]).toBeTruthy();
    expect(post?.headers["if-match"]).toBe("7");
  });

  it("re-reads the version and sends the share again when the node moved under it", async () => {
    // The chat header's Share opens this dialog on a node a live chat is still
    // writing to, so the etag it opened with is routinely spent by the time a
    // person picks someone. A share is not a content change, and the person
    // clicking it has no version to hold — the dialog holds it for them.
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Name, email or team"), "sam");
    await user.click(await screen.findByRole("option", { name: /Sam Reyes/ }));
    // Someone else writes the node between the dialog opening and the click, so
    // the version it is holding ("7") is spent and the node is now at "9".
    staleEtag = "7";
    etag = "9";
    await user.click(screen.getByRole("button", { name: "Share" }));

    await waitFor(() => expect(writes("POST")).toHaveLength(2));
    const [first, second] = writes("POST");
    expect(first?.headers["if-match"]).toBe("7");
    // Not the stale one again: the retry names the version the re-read returned.
    expect(second?.headers["if-match"]).toBe("9");
    // A retry is a fresh attempt, so it must not replay the first one's answer.
    expect(second?.headers["idempotency-key"]).not.toBe(first?.headers["idempotency-key"]);
  });

  it("retries a stale share exactly once and then says what the server said", async () => {
    // The negative twin. Without a bound, a node under active change would spin
    // the dialog forever instead of telling the person what happened.
    const user = userEvent.setup();
    grantAnswer = {
      status: 412,
      body: { code: "files.precondition_failed", message: "This item changed" },
    };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Name, email or team"), "sam");
    await user.click(await screen.findByRole("option", { name: /Sam Reyes/ }));
    await user.click(screen.getByRole("button", { name: "Share" }));

    await waitFor(() => expect(writes("POST")).toHaveLength(2));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(writes("POST")).toHaveLength(2);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("says the server's sentence when the person picked already owns the item", async () => {
    const user = userEvent.setup();
    grantAnswer = {
      status: 409,
      body: { code: "files.already_owner", message: "You already own this." },
    };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Name, email or team"), "sam");
    await user.click(await screen.findByRole("option", { name: /Sam Reyes/ }));
    await user.click(screen.getByRole("button", { name: "Share" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You already own this.");
    expect(alert.textContent).not.toMatch(/files\.already_owner/);
  });

  it("does not re-read the version when the refusal is not about the version", async () => {
    // A 403 is an answer, not a race: retrying it would send a second refused
    // write and the person would wait twice as long for the same no.
    const user = userEvent.setup();
    grantAnswer = { status: 403, body: { code: "files.no_reshare", message: "You cannot" } };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Name, email or team"), "sam");
    await user.click(await screen.findByRole("option", { name: /Sam Reyes/ }));
    await user.click(screen.getByRole("button", { name: "Share" }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(writes("POST")).toHaveLength(1);
  });

  it("offers a team as well as a person, and never someone who already has a grant", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Name, email or team"), "a");
    const matches = await screen.findByRole("listbox", { name: "Matches" });
    const offered = within(matches)
      .getAllByRole("option")
      .map((button) => button.textContent ?? "");
    expect(offered.some((text) => text.includes("Analytics"))).toBe(true);
    expect(offered.some((text) => text.includes("Sam Reyes"))).toBe(true);
    // Dana already has a direct grant: her row is where that changes, so the
    // picker would only produce a second row for the same person.
    expect(offered.some((text) => text.includes("Dana Okafor"))).toBe(false);

    await user.click(within(matches).getByRole("option", { name: /Analytics/ }));
    await user.click(screen.getByRole("button", { name: "Share" }));
    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0]?.body).toMatchObject({
      principal: { kind: "team", id: "team_an" },
    });
  });

  it("changes a role with one request and no withdrawal", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const direct = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "direct") as HTMLElement;

    await pickRole(user, rolePicker(direct), "Full access");

    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0]?.body).toMatchObject({ role: "manager" });
    // The grant route moves the share this person already holds, so there is no
    // second row to take away. Withdrawing one here would leave two live
    // grants behind whenever a second change raced the first.
    expect(writes("DELETE")).toHaveLength(0);
    await waitFor(() => expect(roleControl(direct)).toHaveValue("manager"));
    expect(rolePicker(direct)).toHaveTextContent("Full access");
  });

  it("holds the row's controls and shows the asked-for rung while the change is out", async () => {
    const user = userEvent.setup();
    holdTheGrant();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const direct = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "direct") as HTMLElement;

    await pickRole(user, rolePicker(direct), "Full access");
    await waitFor(() => expect(writes("POST")).toHaveLength(1));

    // The select does NOT spring back to the rung the cache still holds: a
    // control that does invites the very second change this row cannot send.
    expect(roleControl(direct)).toHaveValue("manager");
    expect(rolePicker(direct)).toHaveTextContent("Full access");
    expect(rolePicker(direct)).toBeDisabled();
    expect(within(direct).getByRole("button", { name: /Remove/ })).toBeDisabled();
    // The inherited row belongs to somebody else and is untouched by it.
    const inherited = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "inherited") as HTMLElement;
    expect(within(inherited).getByRole("button", { name: /Remove/ })).toBeDisabled();

    await act(async () => {
      releaseGrant();
    });
    await waitFor(() => expect(rolePicker(direct)).toBeEnabled());
  });

  it("sends nothing more while a row's change is still out", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const direct = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "direct") as HTMLElement;
    // The form control itself: a held picker cannot be opened, so the only way
    // to put a second change to this row is to drive the value underneath it.
    const select = roleControl(direct);

    // First, with nothing in flight — which is what makes the rest of this case
    // mean something: it proves the event really does drive this select, so the
    // request that does not arrive below is one the dialog withheld.
    fireEvent.change(select, { target: { value: "writer" } });
    await waitFor(() => expect(writes("POST")).toHaveLength(1));

    holdTheGrant();
    fireEvent.change(select, { target: { value: "manager" } });
    await waitFor(() => expect(writes("POST")).toHaveLength(2));
    // What a person does when a control springs back: pick again. Both requests
    // would carry the same spent version, and admitting both would put one
    // person on two live grants.
    fireEvent.change(select, { target: { value: "reader" } });
    await act(async () => {
      await Promise.resolve();
    });
    expect(writes("POST")).toHaveLength(2);

    await act(async () => {
      releaseGrant();
    });
    await waitFor(() => expect(rolePicker(direct)).toBeEnabled());
  });

  it("leaves the row on its old rung when the change is refused", async () => {
    const user = userEvent.setup();
    grantAnswer = { status: 409, body: { code: "files.conflict", message: "no" } };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const direct = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "direct") as HTMLElement;

    await pickRole(user, rolePicker(direct), "Full access");

    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("DELETE")).toHaveLength(0);
    // The optimistic rung is not kept: nothing changed, so the row says so and
    // the controls come back.
    await waitFor(() => expect(roleControl(direct)).toHaveValue("reader"));
    expect(rolePicker(direct)).toHaveTextContent("Can view");
    expect(rolePicker(direct)).toBeEnabled();
  });

  it("explains a refused revoke with the folder the access comes from", async () => {
    const user = userEvent.setup();
    revokeAnswer = {
      status: 409,
      body: { code: "files.inherited_grant", message: "inherited" },
    };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const direct = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "direct") as HTMLElement;

    await user.click(within(direct).getByRole("button", { name: /Remove/ }));

    const alert = await screen.findByRole("alert");
    expect(alert.closest("[data-code]")).toHaveAttribute("data-code", "files.inherited_grant");
    // The product's sentence, not the envelope's word — and it says where to go.
    expect(alert.textContent).toContain("inherited");
    expect(alert.textContent).toMatch(/folder further up|Remove it there/);
  });

  it("says why, in the server's own words, when the caller may not share", async () => {
    nodeOverrides = {
      capabilities: {
        can_read: true,
        can_share: false,
        refusals: { share: "This item cannot be shared further." },
      },
    };
    mount();

    const refusal = await screen.findByText("This item cannot be shared further.");
    expect(refusal).toBeInTheDocument();
    // No way in: the add row is gone, and who has access reads as a list —
    // no role select and no Remove the reader cannot use.
    expect(screen.queryByPlaceholderText("Name, email or team")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const direct = screen
      .getAllByRole("listitem")
      .find((row) => row.getAttribute("data-origin") === "direct") as HTMLElement;
    expect(within(direct).queryByRole("button", { name: /^Role for / })).toBeNull();
    expect(direct.querySelector("select")).toBeNull();
    expect(within(direct).queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(within(direct).getByText(/^Can (view|edit)$|^Full access$/)).toBeInTheDocument();
  });

  it.each([
    { rung: "view", canWrite: false },
    { rung: "edit", canWrite: true },
  ])("names the rung, not the code, when a reader at Can $rung may not share", async ({ rung, canWrite }) => {
    nodeOverrides = {
      capabilities: {
        can_read: true,
        can_write: canWrite,
        can_share: false,
        refusals: { share: "insufficient_role" },
      },
    };
    mount();

    expect(await screen.findByText(`You can ${rung} this, not share it.`)).toBeInTheDocument();
    expect(screen.queryByText("insufficient_role")).toBeNull();
  });

  it("writes nothing while it is closed", async () => {
    mount({ open: false });
    await waitFor(() => expect(calls.filter((call) => call.method !== "GET")).toHaveLength(0));
    expect(calls.some((call) => call.url.includes("/permissions"))).toBe(false);
  });
});

describe("people coming and going", () => {
  it("lists a person again as soon as they are re-added after being removed", async () => {
    const user = userEvent.setup();
    rowsFollowWrites = true;
    mount();
    const dana = await screen.findByText("Dana Okafor");
    const row = dana.closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Remove Dana Okafor" }));
    await waitFor(() => expect(screen.queryByText("Dana Okafor")).toBeNull());

    await user.type(screen.getByPlaceholderText("Name, email or team"), "dana");
    await user.click(await screen.findByRole("option", { name: /Dana Okafor/ }));
    await user.click(screen.getByRole("button", { name: "Share" }));

    const back = await screen.findByText("Dana Okafor", { selector: "*" }, { timeout: 3_000 });
    expect(back.closest("li")?.getAttribute("data-origin")).toBe("direct");
  });
});

describe("the owner's row", () => {
  it("says Owner and offers no Remove or role picker, inherited or not", async () => {
    nodeOverrides = { attrs: { owner: "usr_admin" } };
    inheritedGrants = [
      {
        id: null,
        principal: { kind: "user", id: "usr_admin" },
        principalName: "Alex Admin",
        role: "owner",
        origin: "inherited",
        grantingNodeId: PARENT,
      },
    ];
    mount();
    const name = await screen.findByText("Alex Admin");
    const row = name.closest("li") as HTMLElement;
    expect(within(row).getByText("Owner")).toBeInTheDocument();
    expect(within(row).queryByText("Inherited")).toBeNull();
    expect(within(row).queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(within(row).queryByRole("button", { name: /^Role for / })).toBeNull();
  });

  it("reads the owner off the server's mark, whatever the row's rung", async () => {
    nodeOverrides = { attrs: { owner: "usr_dana" } };
    mount();
    const name = await screen.findByText("Dana Okafor");
    const row = name.closest("li") as HTMLElement;
    expect(within(row).getByText("Owner")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Remove/ })).toBeNull();
  });
});

describe("a rung the dialog does not hand out", () => {
  it("is shown, kept selected, and cannot be moved from here", async () => {
    directGrants = [
      {
        id: "sh_own",
        principal: { kind: "user", id: "usr_dana" },
        principalName: "Dana Okafor",
        role: "auditor",
        origin: "direct",
        grantingNodeId: null,
      },
    ];
    inheritedGrants = [];
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const row = screen
      .getAllByRole("listitem")
      .find((one) => one.getAttribute("data-origin") === "direct") as HTMLElement;
    const select = roleControl(row);

    // A rung this build does not hand out is displayed with its own label rather
    // than dropped (which would render the select empty) and cannot be moved here.
    // (Ownership has a row of its own: see "the owner's row".)
    expect(select.value).toBe("auditor");
    expect(Array.from(select.options).map((one) => one.textContent)).toContain("auditor");
    expect(rolePicker(row)).toHaveTextContent("auditor");
    expect(rolePicker(row)).toBeDisabled();
  });
});

/** Swap the clipboard the dialog writes through. jsdom ships none, so every case
 *  states the one it wants — including "there isn't one". Called AFTER
 *  `userEvent.setup()`, which installs a clipboard of its own. */
function clipboard(writeText: unknown): void {
  Object.defineProperty(globalThis.navigator, "clipboard", {
    value: writeText === undefined ? undefined : { writeText },
    configurable: true,
  });
}

describe("the links the dialog hands out", () => {
  it("says a link is copied only once the clipboard write has resolved", async () => {
    // The whole point of the section: a write to the clipboard is a
    // permission-gated promise. A button that flips on the CALL tells someone
    // their link is on the clipboard while the browser is still deciding — and
    // they paste the previous one somewhere it does not belong.
    let settle: (() => void) | undefined;
    const writeText = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const user = userEvent.setup();
    clipboard(writeText);
    mount();

    const copy = await screen.findByRole("button", { name: "Copy link" });
    await user.click(copy);
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/files/${NODE}`);
    expect(screen.queryByText("Link copied")).toBeNull();
    expect(screen.queryByText("The link was not copied.")).toBeNull();

    await act(async () => {
      settle?.();
    });
    expect(screen.getByRole("button", { name: "Link copied" })).toBeInTheDocument();
    expect(screen.queryByText("The link was not copied.")).toBeNull();
  });

  it("says the link was not copied when the browser refuses", async () => {
    const user = userEvent.setup();
    clipboard(vi.fn().mockRejectedValue(new Error("denied")));
    mount();

    await user.click(await screen.findByRole("button", { name: "Copy link" }));
    expect(await screen.findByText("The link was not copied.")).toBeInTheDocument();
    expect(screen.queryByText("Link copied")).toBeNull();
  });

  it("says the link was not copied when there is no clipboard at all", async () => {
    const user = userEvent.setup();
    clipboard(undefined);
    mount();

    await user.click(await screen.findByRole("button", { name: "Copy link" }));
    expect(await screen.findByText("The link was not copied.")).toBeInTheDocument();
  });

  it("names the org every copied link belongs to, merged into the link's own query", async () => {
    nodeOverrides = {
      kind: "folder",
      name: "3952c9e2.alkerachat",
      nameDisplay: "3952c9e2.alkerachat",
      object: {
        type: "chat",
        id: "cht_9",
        title: "Q3 review",
        web_url: "https://app.alkera.test/chat/cht_9?turn=t2",
        metadata: { files_node_id: "nd_scratch" },
      },
    };
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    clipboard(writeText);
    enterOrg("org_a");
    try {
      mount();
      await user.click(await screen.findByRole("button", { name: "Link to chat" }));
      expect(writeText).toHaveBeenLastCalledWith("https://app.alkera.test/chat/cht_9?turn=t2&org=org_a");
      await user.click(screen.getByRole("button", { name: "Link to files" }));
      expect(writeText).toHaveBeenLastCalledWith(`${window.location.origin}/files/nd_scratch?org=org_a`);
    } finally {
      forgetActiveOrg();
    }
  });

  it("offers a chat the conversation first and its files second", async () => {
    // A chat row is a page AND a folder, so one link cannot stand for it. The
    // order is the contract: the conversation is what a person means by "the
    // chat", and the files are the second thing the same row is.
    nodeOverrides = {
      kind: "folder",
      name: "3952c9e2.alkerachat",
      nameDisplay: "3952c9e2.alkerachat",
      object: {
        type: "chat",
        id: "cht_9",
        title: "Q3 review",
        web_url: "https://app.alkera.test/chat/cht_9",
        metadata: { files_node_id: "nd_scratch" },
      },
    };
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    clipboard(writeText);
    mount();

    // Wait for the node itself: until it lands the dialog knows only the id it
    // was opened on, and one link is all an id can offer.
    await screen.findByRole("button", { name: "Link to chat" });
    const links = within(screen.getByRole("region", { name: "Links" })).getAllByRole("button");
    expect(links.map((one) => one.textContent)).toEqual(["Link to chat", "Link to files"]);
    expect(screen.queryByRole("button", { name: "Copy link" })).toBeNull();

    await user.click(links[0] as HTMLElement);
    expect(writeText).toHaveBeenLastCalledWith("https://app.alkera.test/chat/cht_9");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Link copied" })).toHaveAttribute(
        "data-link",
        "page",
      ),
    );

    // The second link is its own button with its own outcome, not the first's.
    await user.click(screen.getByRole("button", { name: "Link to files" }));
    expect(writeText).toHaveBeenLastCalledWith(`${window.location.origin}/files/nd_scratch`);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Link copied" })).toHaveAttribute(
        "data-link",
        "files",
      ),
    );
  });
});

describe("a grant to something the dialog cannot name", () => {
  it("says so, keeps the id beside it, and still offers Remove", async () => {
    // `kind` rides the wire as a bare string. A grant made to something this
    // build has never heard of is still access somebody has, and access nobody
    // can identify is exactly the access that has to stay withdrawable — so the
    // row is legible rather than a bare uuid masquerading as a person's handle.
    directGrants = [
      {
        id: "sh_svc",
        principal: { kind: "service_account", id: "svc_99" },
        principalName: null,
        role: "reader",
        origin: "direct",
        grantingNodeId: null,
      },
    ];
    inheritedGrants = [];
    mount();

    const row = (await screen.findAllByRole("listitem")).find(
      (one) => one.getAttribute("data-origin") === "direct",
    ) as HTMLElement;
    expect(within(row).getByText("Unknown principal")).toBeInTheDocument();
    expect(within(row).getByText("svc_99")).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Remove Unknown principal svc_99" }),
    ).toBeEnabled();
  });

  it("still shows the name when the server resolved one for that kind", async () => {
    directGrants = [
      {
        id: "sh_svc",
        principal: { kind: "service_account", id: "svc_99" },
        principalName: "Nightly export",
        role: "reader",
        origin: "direct",
        grantingNodeId: null,
      },
    ];
    inheritedGrants = [];
    mount();

    expect(await screen.findByText("Nightly export")).toBeInTheDocument();
    expect(screen.queryByText("Unknown principal")).toBeNull();
  });

  it("keeps showing a user's own id when that is all the server sent", async () => {
    // A user the listing could not resolve is still a user: the id names the
    // row, and replacing it with "Unknown principal" would lose the only handle
    // an admin has on it.
    directGrants = [
      {
        id: "sh_kim",
        principal: { kind: "user", id: "usr_kim" },
        principalName: null,
        role: "reader",
        origin: "direct",
        grantingNodeId: null,
      },
    ];
    inheritedGrants = [];
    mount();

    expect(await screen.findByText("usr_kim")).toBeInTheDocument();
    expect(screen.queryByText("Unknown principal")).toBeNull();
  });
});

describe("the people suggestions", () => {
  const field = () => screen.getByPlaceholderText("Name, email or team");

  it("offers whoever the server offers, and never someone who already has a grant", async () => {
    // The server leaves the node's owner out of the candidates; the dialog
    // decides nothing about who owns what. An owner inherited from above is
    // offered like any inherited grant: a direct share to them is redundant,
    // and the server says so if it minds.
    const user = userEvent.setup();
    inheritedGrants = [
      {
        id: null,
        principal: { kind: "user", id: "usr_admin" },
        principalName: "Alex Admin",
        role: "owner",
        origin: "inherited",
        grantingNodeId: PARENT,
      },
    ];
    nodeOverrides = { attrs: { owner: "usr_sara" } };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(field(), "a");
    const offered = within(await screen.findByRole("listbox", { name: "Matches" }))
      .getAllByRole("option")
      .map((one) => one.textContent ?? "");
    expect(offered.some((text) => text.includes("Sam Reyes"))).toBe(true);
    expect(offered.some((text) => text.includes("Alex Admin"))).toBe(true);
    expect(offered.some((text) => text.includes("Sara Lind"))).toBe(false);
    expect(offered.some((text) => text.includes("Dana Okafor"))).toBe(false);
  });

  it("is a listbox the arrow keys walk and Enter picks from", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(field(), "sa");
    const box = await screen.findByRole("listbox", { name: "Matches" });
    const options = within(box).getAllByRole("option");
    expect(options.map((one) => one.textContent)).toEqual([
      expect.stringContaining("Sam Reyes"),
      expect.stringContaining("Sara Lind"),
    ]);
    const combo = screen.getByRole("combobox", { name: "Add people and teams" });
    expect(combo).toHaveAttribute("aria-expanded", "true");
    expect(combo).toHaveAttribute("aria-controls", box.id);
    expect(combo).toHaveAttribute("aria-activedescendant", options[0]!.id);

    await user.keyboard("{ArrowDown}");
    expect(combo).toHaveAttribute("aria-activedescendant", options[1]!.id);
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}");
    expect(combo).toHaveAttribute("aria-activedescendant", options[1]!.id);
    await user.keyboard("{ArrowUp}{ArrowDown}{Enter}");

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(combo).toHaveValue("Sara Lind");
    expect(combo).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Share" }));
    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0]?.body).toMatchObject({ principal: { kind: "user", id: "usr_sara" } });
  });

  it("finds a colleague by exact email for a member the admin member table refuses", async () => {
    // The member is no org admin: the org's member table answers them 403 (the
    // stub above). The picker reads the directory the node's share decides.
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(field(), "admin@acme.test");
    const box = await screen.findByRole("listbox", { name: "Matches" });
    expect(within(box).getByRole("option", { name: /Alex Admin/ })).toBeInTheDocument();
    const searched = calls.filter((call) => call.url.includes("/share-candidates"));
    expect(searched.length).toBeGreaterThan(0);
    expect(new URL(searched.at(-1)!.url).searchParams.get("q")).toBe("admin@acme.test");
    expect(searched.at(-1)!.url).toContain(`/drives/${DRIVE}/items/${NODE}/share-candidates`);
    expect(calls.some((call) => call.url.includes("/org/members"))).toBe(false);
  });

  it("waits out a burst of typing and asks once", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    await user.type(field(), "sam");
    await screen.findByRole("option", { name: /Sam Reyes/ });
    const asked = calls
      .filter((call) => call.url.includes("/share-candidates"))
      .map((call) => new URL(call.url).searchParams.get("q"));
    expect(asked).toEqual(["sam"]);
  });

  it.each([
    ["refused", 403, { code: "files.forbidden", message: "Not allowed" }],
    ["broken", 500, { code: "internal", message: "boom" }],
  ])("says the search was not answered when it is %s, never that nobody matches", async (_label, status, body) => {
    const user = userEvent.setup();
    candidatesAnswer = { status, body };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.type(field(), "admin@acme.test");
    const note = await screen.findByText("People and teams could not be searched.");
    expect(note).toHaveAttribute("role", "alert");
    expect(screen.queryByText(/Nobody here matches/)).toBeNull();
    expect(screen.getByRole("button", { name: "Share" })).toBeDisabled();
  });

  it("says nobody matches only once the search has answered with nobody", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    await user.type(field(), "zz-nobody");
    expect(screen.getByText("Searching…")).toBeInTheDocument();
    expect(await screen.findByText("Nobody here matches “zz-nobody”.")).toBeInTheDocument();
    expect(screen.queryByText("People and teams could not be searched.")).toBeNull();
  });

  it("labels the org itself as everyone in it", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    await user.type(field(), "acme");
    const option = await screen.findByRole("option", { name: /^Acme/ });
    expect(option).toHaveTextContent("Everyone in the organization");
  });

  it("does not search the directory for a caller who may not share", async () => {
    nodeOverrides = {
      capabilities: { can_read: true, can_write: true, can_share: false, refusals: {} },
    };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    expect(screen.queryByPlaceholderText("Name, email or team")).toBeNull();
    expect(calls.some((call) => call.url.includes("/share-candidates"))).toBe(false);
  });

  it("sits in the dialog's flow, so the dialog's scroll box cannot cut it off", async () => {
    // jsdom lays nothing out; the rule the placement rests on is read off the sheet.
    const css = readFileSync(
      join(process.cwd(), "src/pages/workspace/files/share-dialog.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = css
      .split("}")
      .find((chunk) => chunk.split("{")[0]!.trim() === ".alk-files-share__suggestions");
    expect(rule).toBeDefined();
    expect(rule).not.toMatch(/position:\s*absolute/);
    expect(rule).toMatch(/flex:\s*1 1 100%/);
  });
});

describe("the dialog's own look", () => {
  const read = (path: string): string =>
    readFileSync(join(process.cwd(), path), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

  it("brings its own stylesheet wherever it is opened from", () => {
    expect(shareSheet.loaded).toBe(true);
    // Nothing of it is left on the browser page's sheet, which a chat route
    // never loads: a rule there would style the dialog in one place only.
    expect(read("src/pages/workspace/files/files-page.css")).not.toMatch(/\.alk-files-share/);
  });

  it("has a rule for every class of its own it renders", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const sheet = read("src/pages/workspace/files/share-dialog.css");
    const used = new Set(
      Array.from(document.querySelectorAll("[class]"))
        .flatMap((el) => Array.from(el.classList))
        .filter((name) => name.startsWith("alk-files-share__")),
    );
    expect(used.size).toBeGreaterThan(5);
    const bare = [...used].filter((name) => !new RegExp(`\\.${name}(?![\\w-])`).test(sheet));
    expect(bare).toEqual([]);
  });

  it("builds its controls from the UI library", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    const search = screen.getByRole("combobox", { name: "Add people and teams" });
    const forNew = screen.getByLabelText("Role for new people");
    const share = screen.getByRole("button", { name: "Share" });

    expect(search).toHaveClass("alk-input");
    expect(forNew).toHaveClass("alk-select__trigger");
    expect(share).toHaveClass("alk-btn");
    // Search, role and Share are one row, not three runs of inline text.
    const row = search.closest(".alk-files-share__controls");
    expect(row).not.toBeNull();
    expect(row).toContainElement(forNew);
    expect(row).toContainElement(share);

    const direct = screen
      .getAllByRole("listitem")
      .find((one) => one.getAttribute("data-origin") === "direct") as HTMLElement;
    expect(rolePicker(direct)).toHaveClass("alk-select__trigger");
    expect(within(direct).getByRole("button", { name: /Remove/ })).toHaveClass("alk-btn");
    const links = within(screen.getByRole("region", { name: "Links" })).getAllByRole("button");
    for (const link of links) expect(link).toHaveClass("alk-btn");
  });

  it("lists who has access as rows with a mark, never as bullets", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("Analytics")).toBeInTheDocument());
    const rows = screen.getAllByRole("listitem");
    const direct = rows.find((one) => one.getAttribute("data-origin") === "direct")!;
    const inherited = rows.find((one) => one.getAttribute("data-origin") === "inherited")!;

    // A person carries the initials disc other member lists show; a team, the
    // team mark instead of letters.
    expect(direct.querySelector(".alk-identity .alk-avatar")).toHaveTextContent("DO");
    expect(inherited.querySelector(".alk-avatar")).toBeNull();
    expect(inherited.querySelector(".alk-identity .alk-iconchip")).not.toBeNull();
    // An inherited row says so on its second line.
    expect(within(inherited).getByText("Inherited")).toBeInTheDocument();
    expect(within(direct).queryByText("Inherited")).toBeNull();

    const list = direct.closest("ul")!;
    expect(list).toHaveClass("alk-files-share__rows");
    const rule = read("src/pages/workspace/files/share-dialog.css")
      .split("}")
      .find((chunk) => chunk.split("{")[0]!.trim() === ".alk-files-share__rows");
    expect(rule).toMatch(/list-style:\s*none/);
    expect(rule).toMatch(/padding:\s*0/);
  });

  it("puts the link note on its own line under the link buttons", async () => {
    mount();
    const note = await screen.findByText(
      "Only people who already have access can open this link.",
    );
    const buttons = within(screen.getByRole("region", { name: "Links" })).getAllByRole("button");
    const buttonRow = buttons[0]!.parentElement!;
    expect(buttonRow).toHaveClass("alk-files-share__link-buttons");
    expect(buttonRow).not.toContainElement(note);
    expect(note.tagName).toBe("P");
  });
});

describe("the row controls are the server's", () => {
  function directRow(): HTMLElement {
    return screen
      .getAllByRole("listitem")
      .find((one) => one.getAttribute("data-origin") === "direct") as HTMLElement;
  }

  it("holds a direct row the server says the reader may not touch, though they may share", async () => {
    // A manager looking at a grant above their own rung: the row is direct and
    // the reader may share the node, and the server still says no to both.
    directGrants = [
      {
        id: "sh_team",
        principal: { kind: "team", id: "team_an" },
        principalName: "Analytics",
        role: "writer",
        origin: "direct",
        grantingNodeId: null,
        canChange: false,
        canRemove: false,
      },
    ];
    inheritedGrants = [];
    mount();
    await waitFor(() => expect(screen.getByText("Analytics")).toBeInTheDocument());

    const row = directRow();
    expect(rolePicker(row)).toBeDisabled();
    expect(within(row).getByRole("button", { name: /Remove/ })).toBeDisabled();
  });

  it("reads a row the server said nothing about as a no", async () => {
    directGrants = [
      {
        id: "sh_dana",
        principal: { kind: "user", id: "usr_dana" },
        principalName: "Dana Okafor",
        role: "reader",
        origin: "direct",
        grantingNodeId: null,
        canChange: undefined,
        canRemove: undefined,
      },
    ];
    inheritedGrants = [];
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    const row = directRow();
    expect(rolePicker(row)).toBeDisabled();
    expect(within(row).getByRole("button", { name: /Remove/ })).toBeDisabled();
  });

  it("offers a new person only the rungs the server says the reader may hand out", async () => {
    const user = userEvent.setup();
    // What a writer-turned-sharer might be offered: the server's list, verbatim.
    vi.stubGlobal(
      "fetch",
      ((inner: typeof fetch) =>
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const response = await inner(input, init);
          const raw = input instanceof Request ? input.url : String(input);
          if (!new URL(raw, "http://localhost").pathname.endsWith("/permissions")) {
            return response;
          }
          if ((init?.method ?? (input instanceof Request ? input.method : "GET")) !== "GET") {
            return response;
          }
          const body = (await response.json()) as Record<string, unknown>;
          return new Response(
            JSON.stringify({ ...body, assignableRoles: [{ role: "reader", label: "Can view" }] }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }))(globalThis.fetch),
    );
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    expect(await listedRoles(user, screen.getByLabelText("Role for new people"))).toEqual([
      "Can view",
    ]);
  });
});

describe("a role change or a removal at a spent version", () => {
  function directRow(): HTMLElement {
    return screen
      .getAllByRole("listitem")
      .find((one) => one.getAttribute("data-origin") === "direct") as HTMLElement;
  }

  it("re-reads the version and changes the role again, once", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    // Someone else wrote the node after the dialog opened at "7".
    staleEtag = "7";
    etag = "9";

    await pickRole(user, rolePicker(directRow()), "Can edit");

    await waitFor(() => expect(writes("POST")).toHaveLength(2));
    expect(writes("POST").map((call) => call.headers["if-match"])).toEqual(["7", "9"]);
    expect(writes("POST")[1]?.body).toMatchObject({ role: "writer" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("re-reads the version and removes the person again, once", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());
    staleEtag = "7";
    etag = "9";

    await user.click(within(directRow()).getByRole("button", { name: /Remove/ }));

    await waitFor(() => expect(writes("DELETE")).toHaveLength(2));
    expect(writes("DELETE").map((call) => call.headers["if-match"])).toEqual(["7", "9"]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("stops after one retry and says what the server said", async () => {
    const user = userEvent.setup();
    revokeAnswer = {
      status: 412,
      body: { code: "files.precondition_failed", message: "This item changed" },
    };
    mount();
    await waitFor(() => expect(screen.getByText("Dana Okafor")).toBeInTheDocument());

    await user.click(within(directRow()).getByRole("button", { name: /Remove/ }));

    await waitFor(() => expect(writes("DELETE")).toHaveLength(2));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(writes("DELETE")).toHaveLength(2);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});
