import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { MoveToDialog, destinationVerdict } from "@/pages/workspace/files/MoveToDialog";

// Two things a person needs from the picker that the grid alone cannot give: a way
// UP — the ancestors of the folder it opened in, and the drive above them — and a
// refusal BEFORE the request, on the destinations the server is going to refuse.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_x",
    kind: "folder",
    name: "x",
    nameDisplay: "x",
    nameEncoding: "utf-8",
    etag: "et",
    ctag: "ct",
    parentId: "nd_root",
    capabilities: { can_read: true, can_write: true },
    ...over,
  } as Item;
}

const HOME_ROWS = [item({ id: "nd_root", name: "root", nameDisplay: "root", parentId: "nd_home" })];
const ROOT_ROWS = [
  item({ id: "nd_a", name: "A", nameDisplay: "A" }),
  item({ id: "nd_locked", name: "locked", nameDisplay: "locked", capabilities: { can_read: true, can_write: false } } as Partial<Item>),
  item({ id: "nd_gone", name: "gone", nameDisplay: "gone", trashed: true }),
  // A row that says nothing at all about what the reader may do with it.
  item({ id: "nd_silent", name: "silent", nameDisplay: "silent", capabilities: undefined } as Partial<Item>),
];
const A_ROWS = [item({ id: "nd_a1", name: "a1", nameDisplay: "a1", parentId: "nd_a" })];

/** The nodes themselves, for the folder the picker stands in. The picker reads the
 *  one it opened on and any crumb it climbs to: a folder it cannot see the
 *  permissions of is a folder it does not offer. */
const NODES: Record<string, Item> = Object.fromEntries(
  [...HOME_ROWS, ...ROOT_ROWS, ...A_ROWS, item({ id: "nd_home", name: "home", nameDisplay: "home" })].map(
    (row) => [row.id, row],
  ),
);

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const body = url.includes("/items/nd_home/children")
        ? { value: HOME_ROWS, nextMarker: null }
        : url.includes("/items/nd_root/children")
          ? { value: ROOT_ROWS, nextMarker: null }
          : url.includes("/items/nd_a/children")
            ? { value: A_ROWS, nextMarker: null }
            : url.endsWith("/files/drives")
              ? { id: "dr_1", homeId: "nd_home" }
              : // The node read behind whichever folder the picker is standing in.
                (NODES[url.slice(url.lastIndexOf("/") + 1)] ?? { value: [], nextMarker: null });
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

function mount(over: Partial<React.ComponentProps<typeof MoveToDialog>> = {}) {
  const onConfirm = vi.fn();
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <MoveToDialog
          open
          driveId="dr_1"
          startFolderId="nd_a"
          startLabel="A"
          count={1}
          initialRect={{ width: 800, height: 400 }}
          onConfirm={onConfirm}
          onCancel={vi.fn()}
          {...over}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onConfirm };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the picker walks up", () => {
  it("offers the ancestors of the folder it opened in, and lands in the one clicked", async () => {
    stubApi();
    const { onConfirm } = mount({
      startTrail: [
        { id: "nd_root", name: "root" },
        { id: "nd_a", name: "A" },
      ],
    });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });
    const trail = within(dialog).getByRole("navigation", { name: "Destination" });
    // Moving one level up is the commonest move there is: the parent is a click away.
    expect(trail).toHaveTextContent("root");
    expect(trail).toHaveTextContent("A");

    await userEvent.click(within(trail).getByText("root"));
    await within(dialog).findByText("A");
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));
    expect(onConfirm).toHaveBeenCalledWith("nd_root", undefined, undefined);
  });

  it("roots the trail at the drive even when the caller named no ancestors", async () => {
    stubApi();
    mount();
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });
    const trail = await within(dialog).findByRole("navigation", { name: "Destination" });
    // The drive root is the one ancestor every picker can always name.
    await vi.waitFor(() => expect(trail).toHaveTextContent("My drive"));
    expect(trail).toHaveTextContent("A");
  });

  it("does not repeat the drive when the picker already opened on it", async () => {
    stubApi();
    mount({ startFolderId: "nd_home", startLabel: "My drive" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });
    const trail = await within(dialog).findByRole("navigation", { name: "Destination" });
    await within(dialog).findByText("root");
    expect(within(trail).getAllByText("My drive")).toHaveLength(1);
  });
});

describe("the picker refuses what the server would refuse", () => {
  it("disables the action inside the folder being moved, with the reason", async () => {
    stubApi();
    const moving = [item({ id: "nd_a", name: "A", nameDisplay: "A" })];
    const { onConfirm } = mount({ moving, startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.dblClick(await within(dialog).findByText("A"));
    await within(dialog).findByText("a1");

    const confirm = within(dialog).getByRole("button", { name: "Move here" });
    expect(confirm).toBeDisabled();
    expect(within(dialog).getByText("A folder can't move inside itself.")).toBeInTheDocument();
    await userEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("disables the action on the folder being moved itself", async () => {
    stubApi();
    const moving = [item({ id: "nd_a", name: "A", nameDisplay: "A" })];
    mount({ moving, startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.click(await within(dialog).findByText("A"));
    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
    expect(within(dialog).getByText("A folder can't move inside itself.")).toBeInTheDocument();
  });

  it("disables the action on the folder the items are already in", async () => {
    stubApi();
    mount({
      startFolderId: "nd_root",
      startLabel: "root",
      moving: [item({ id: "nd_x1", kind: "file", parentId: "nd_root" })],
    });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });
    await within(dialog).findByText("A");

    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
    expect(within(dialog).getByText("This item is already here.")).toBeInTheDocument();
  });

  it("disables the action on a folder the reader cannot write to", async () => {
    stubApi();
    mount({ startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.click(await within(dialog).findByText("locked"));
    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
    expect(within(dialog).getByText("You can't add items to this folder.")).toBeInTheDocument();
  });

  // Walking down is how this picker is used. A folder refused only when its row
  // happens to be PICKED is a folder the person walks into and the server refuses
  // a moment later instead.
  it("disables the action after walking INTO a folder the reader cannot write to", async () => {
    stubApi();
    mount({ startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.dblClick(await within(dialog).findByText("locked"));
    await within(dialog).findByText("No folders here.");

    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
    expect(within(dialog).getByText("You can't add items to this folder.")).toBeInTheDocument();
  });

  it("disables the action after walking INTO a trashed folder", async () => {
    stubApi();
    mount({ startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.dblClick(await within(dialog).findByText("gone"));
    await within(dialog).findByText("No folders here.");

    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
    expect(within(dialog).getByText("A folder in the trash can't hold items.")).toBeInTheDocument();
  });

  // The folder the picker OPENS in is the one a person presses Move here in most
  // often, and it was never judged at all: the caller hands a crumb, not a row.
  it("disables the action in a read-only folder the picker opened in", async () => {
    stubApi();
    mount({ startFolderId: "nd_locked", startLabel: "locked" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await within(dialog).findByText("You can't add items to this folder.");
    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
  });

  it("disables the action on a read-only ancestor climbed back to", async () => {
    stubApi();
    mount({
      startFolderId: "nd_a",
      startLabel: "A",
      startTrail: [
        { id: "nd_locked", name: "locked" },
        { id: "nd_a", name: "A" },
      ],
    });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });
    const trail = within(dialog).getByRole("navigation", { name: "Destination" });

    await userEvent.click(within(trail).getByText("locked"));

    await within(dialog).findByText("You can't add items to this folder.");
    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
  });

  it("refuses a folder that says nothing about what the reader may do", async () => {
    stubApi();
    mount({ startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.click(await within(dialog).findByText("silent"));
    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
    expect(within(dialog).getByText("You can't add items to this folder.")).toBeInTheDocument();
  });

  it("moves the rows that can move, and names the one that cannot", async () => {
    stubApi();
    const blocked = item({ id: "nd_a", name: "A", nameDisplay: "A" });
    const fine = item({
      id: "nd_f1",
      kind: "file",
      name: "a.txt",
      nameDisplay: "a.txt",
      parentId: "nd_other",
    });
    const { onConfirm } = mount({
      moving: [blocked, fine],
      startFolderId: "nd_root",
      startLabel: "root",
      count: 2,
    });
    const dialog = await screen.findByRole("dialog", { name: /^Move 2 items to/ });

    await userEvent.dblClick(await within(dialog).findByText("A"));
    await within(dialog).findByText("a1");

    // One of the two cannot land here; the other still can, and the sentence
    // names what is staying behind rather than blocking the pair.
    expect(within(dialog).getByText("A can't move inside itself.")).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Move here" });
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("nd_a", undefined, [fine]);
  });

  it("disables the action when every row is blocked", async () => {
    stubApi();
    const one = item({ id: "nd_a", name: "A", nameDisplay: "A" });
    const two = item({ id: "nd_a1", name: "a1", nameDisplay: "a1", parentId: "nd_a" });
    mount({ moving: [one, two], startFolderId: "nd_root", startLabel: "root", count: 2 });
    const dialog = await screen.findByRole("dialog", { name: /^Move 2 items to/ });

    await userEvent.dblClick(await within(dialog).findByText("A"));
    await within(dialog).findByText("a1");

    // One is the folder itself, the other is already inside it.
    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
  });

  it("leaves a legal destination alone", async () => {
    stubApi();
    const moving = [item({ id: "nd_far", kind: "file", parentId: "nd_elsewhere" })];
    const { onConfirm } = mount({ moving, startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.click(await within(dialog).findByText("A"));
    const confirm = within(dialog).getByRole("button", { name: "Move here" });
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("nd_a", expect.anything(), moving);
  });

  it("leaves a picker with nothing to move alone", async () => {
    // Restore to… and Copy to… mount the same picker and name no rows.
    stubApi();
    const { onConfirm } = mount({ startFolderId: "nd_root", startLabel: "root" });
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });

    await userEvent.click(await within(dialog).findByText("A"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));
    expect(onConfirm).toHaveBeenCalledWith("nd_a", expect.anything(), undefined);
  });
});

describe("destinationVerdict", () => {
  const moved = item({ id: "nd_a", path: "/home/dana/root/A" });
  const reasonFor = (check: Parameters<typeof destinationVerdict>[0]): string | null =>
    destinationVerdict(check).reason;

  it("refuses a descendant reached through a search, by its path", () => {
    expect(
      reasonFor({
        destinationId: "nd_deep",
        destination: item({ id: "nd_deep", path: "/home/dana/root/A/x/y" }),
        trailIds: ["nd_home"],
        moving: [moved],
      }),
    ).toBe("A folder can't move inside itself.");
  });

  it("allows a sibling whose path merely starts with the same letters", () => {
    expect(
      reasonFor({
        destinationId: "nd_sib",
        destination: item({ id: "nd_sib", path: "/home/dana/root/Archive" }),
        trailIds: ["nd_home"],
        moving: [moved],
      }),
    ).toBeNull();
  });

  // A search hit is not on the trail, so its path is the only proof of where it
  // lives. Without one the picker cannot show it is outside the folder being
  // moved, and an unprovable destination is refused rather than offered.
  it("refuses a search hit that cannot say where it lives", () => {
    expect(
      reasonFor({
        destinationId: "nd_nowhere",
        destination: item({ id: "nd_nowhere", path: null, pathBytes: "" } as Partial<Item>),
        trailIds: ["nd_home"],
        moving: [moved],
        viaSearch: true,
      }),
    ).toBe("A folder can't move inside itself.");
  });

  it("refuses a search hit when the folder being moved cannot say where it lives", () => {
    expect(
      reasonFor({
        destinationId: "nd_far",
        destination: item({ id: "nd_far", path: "/home/dana/elsewhere" }),
        trailIds: ["nd_home"],
        moving: [item({ id: "nd_b", path: null, pathBytes: "" } as Partial<Item>)],
        viaSearch: true,
      }),
    ).toBe("A folder can't move inside itself.");
  });

  it("does not invent a cycle for a file, which has no inside", () => {
    expect(
      reasonFor({
        destinationId: "nd_nowhere",
        destination: item({ id: "nd_nowhere", path: null, pathBytes: "" } as Partial<Item>),
        trailIds: ["nd_home"],
        moving: [item({ id: "nd_file", kind: "file", path: null } as Partial<Item>)],
        viaSearch: true,
      }),
    ).toBeNull();
  });

  it("says nothing when there is no destination to judge", () => {
    expect(reasonFor({ destinationId: undefined, trailIds: [], moving: [moved] })).toBeNull();
  });

  it("refuses a trashed destination", () => {
    expect(
      reasonFor({
        destinationId: "nd_t",
        destination: item({ id: "nd_t", trashed: true }),
        trailIds: [],
        moving: [],
      }),
    ).toBe("A folder in the trash can't hold items.");
  });

  it("refuses a destination row with no capabilities at all", () => {
    expect(
      reasonFor({
        destinationId: "nd_s",
        destination: item({ id: "nd_s", capabilities: undefined } as Partial<Item>),
        trailIds: [],
        moving: [],
      }),
    ).toBe("You can't add items to this folder.");
  });

  it("refuses a folder it holds no row for at all", () => {
    // An ancestor crumb whose read never answered. Unknown is not permitted:
    // offering it is how a read-only start folder kept a live Move here.
    expect(reasonFor({ destinationId: "nd_up", trailIds: [], moving: [] })).toBe(
      "You can't add items to this folder.",
    );
  });

  it("neither offers nor refuses while the row is still being read", () => {
    const verdict = destinationVerdict({
      destinationId: "nd_up",
      trailIds: [],
      moving: [],
      awaiting: true,
    });
    expect(verdict.reason).toBeNull();
    expect(verdict.blocksAll).toBe(true);
  });

  it("keeps the rows that can still move, and blocks only the rest", () => {
    const stuck = item({ id: "nd_a", path: "/home/dana/root/A" });
    const free = item({ id: "nd_free", kind: "file", parentId: "nd_other" });
    const verdict = destinationVerdict({
      destinationId: "nd_a",
      destination: item({ id: "nd_a", path: "/home/dana/root/A" }),
      trailIds: ["nd_home"],
      moving: [stuck, free],
    });
    expect(verdict.blocksAll).toBe(false);
    expect(verdict.movable).toEqual([free]);
    expect(verdict.reason).toBe("x can't move inside itself.");
  });

  it("counts them when more than one is staying behind", () => {
    const a = item({ id: "nd_a", nameDisplay: "A" });
    const b = item({ id: "nd_b", nameDisplay: "B", parentId: "nd_dest" });
    const free = item({ id: "nd_free", kind: "file", parentId: "nd_other" });
    const verdict = destinationVerdict({
      destinationId: "nd_dest",
      destination: item({ id: "nd_dest" }),
      trailIds: ["nd_a"],
      moving: [a, b, free],
    });
    expect(verdict.blocksAll).toBe(false);
    expect(verdict.movable).toEqual([free]);
    expect(verdict.reason).toBe("2 items are staying where they are.");
  });

  it("uses the plural when every row is already in the destination", () => {
    expect(
      reasonFor({
        destinationId: "nd_dest",
        destination: item({ id: "nd_dest" }),
        trailIds: [],
        moving: [
          item({ id: "nd_1", parentId: "nd_dest" }),
          item({ id: "nd_2", parentId: "nd_dest" }),
        ],
      }),
    ).toBe("These items are already here.");
  });

  // A selection made in a search or a feed comes from several folders, so the
  // picker reads each row's own parent rather than the folder on screen.
  it("lets a row from another folder move into the folder on screen", () => {
    const here = item({ id: "nd_1", parentId: "nd_dest" });
    const away = item({ id: "nd_2", parentId: "nd_far" });
    const verdict = destinationVerdict({
      destinationId: "nd_dest",
      destination: item({ id: "nd_dest" }),
      trailIds: [],
      moving: [here, away],
    });
    expect(verdict.blocksAll).toBe(false);
    expect(verdict.movable).toEqual([away]);
  });
});
