import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { LimitsProvider } from "@/lib/limits";
import { MoveToDialog } from "@/pages/workspace/files/MoveToDialog";

// The folder picker is the browser's own parts in a dialog — the trail, the treegrid in
// its list layout, folders only — so what is pinned is what a person can do with it: pick
// a folder with one click, walk into one, climb back by the trail, and land the action in
// the picked folder or the one open.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_x",
    kind: "folder",
    name: "x",
    nameDisplay: "x",
    nameEncoding: "utf-8",
    etag: "et",
    ctag: "ct",
    parentId: "nd_home",
    capabilities: { can_read: true, can_write: true },
    ...over,
  } as Item;
}

const HOME_ROWS = [
  item({ id: "nd_a", name: "archive", nameDisplay: "archive" }),
  item({ id: "nd_b", name: "budgets", nameDisplay: "budgets" }),
  // A file rides along to prove the picker shows folders only, whatever the wire holds.
  item({ id: "nd_f", kind: "file", name: "report.csv", nameDisplay: "report.csv" }),
  // A chat: the wire carries it as a FOLDER with the object facet.
  item({
    id: "nd_chat",
    name: "Q3 revenue.alkerachat",
    nameDisplay: "Q3 revenue.alkerachat",
    object: { type: "chat", id: "cht_9", web_url: "/chat/cht_9" },
  } as Partial<Item>),
  // A chat template: a folder object too, and a place for the same reason a chat
  // is — its files are what a new chat starts with.
  item({
    id: "nd_tpl",
    name: "Monthly revenue.alkerachat.template",
    nameDisplay: "Monthly revenue.alkerachat.template",
    object: { type: "chat_template", id: "tpl_4", web_url: "/templates/tpl_4" },
  } as Partial<Item>),
  // A saved query: also a folder carrying the object facet, and NOT a place — the
  // pair is what proves the picker tests the object behind the node, not its kind.
  item({
    id: "nd_query",
    name: "Revenue by region.alkeraquery",
    nameDisplay: "Revenue by region.alkeraquery",
    object: { type: "query", id: "qry_2", web_url: "/query/qry_2" },
  } as Partial<Item>),
];
const ARCHIVE_ROWS = [item({ id: "nd_a1", name: "2024", nameDisplay: "2024", parentId: "nd_a" })];

/** What a drive-wide search for "bud" finds: a folder far from the one open, and a file
 *  of the same stem the picker must not show. */
const SEARCH_HITS = [
  // The wire carries `pathBytes` and no `path`, as the server really answers.
  item({
    id: "nd_far",
    name: "budgets-2023",
    nameDisplay: "budgets-2023",
    parentId: "nd_old",
    path: null,
    pathBytes: "/home/dana/old/budgets-2023",
  } as Partial<Item>),
  item({
    id: "nd_far_file",
    kind: "file",
    name: "budget.csv",
    nameDisplay: "budget.csv",
    path: "/home/dana/budget.csv",
  }),
  // A chat whose name matches too: the drive-wide search is the other way a chat
  // anywhere in the drive reaches the grid.
  item({
    id: "nd_far_chat",
    name: "budget review.alkerachat",
    nameDisplay: "budget review.alkerachat",
    path: "/home/dana/budget review.alkerachat",
    object: { type: "chat", id: "cht_7", web_url: "/chat/cht_7" },
  } as Partial<Item>),
];

function stubApi(): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      urls.push(url);
      // The node read behind the folder the picker is standing in: it judges what
      // it opened on, not only the rows it was handed.
      const node = /\/items\/(nd_[a-z_]+)$/.exec(url)?.[1];
      if (node) {
        return new Response(JSON.stringify(item({ id: node, name: node, nameDisplay: node })), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      const rows = url.includes("/search")
        ? SEARCH_HITS
        : url.includes("/items/nd_a/children")
          ? ARCHIVE_ROWS
          : url.includes("/items/nd_home/children")
            ? HOME_ROWS
            : [];
      return new Response(JSON.stringify({ value: rows, nextMarker: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return urls;
}

// The picker's search field debounces by 150ms before it asks the server. No case
// here is about that window -- they are about what the drive search asks for and
// what comes back -- so the window is taken to zero for all of them. Left in, it is
// a timer nothing waits on: every search assertion then sits behind a delay it
// never observes, and on a loaded runner the `findBy*` budget, not the picker, is
// what decides whether the row arrived. The hook reads the window through the
// limits provider for exactly this.
function mount(over: Partial<React.ComponentProps<typeof MoveToDialog>> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <QueryClientProvider client={createQueryClient()}>
      <LimitsProvider overrides={{ filesSearchDebounceMs: 0 }}>
        <MemoryRouter>
          <MoveToDialog
            open
            driveId="dr_1"
            startFolderId="nd_home"
            startLabel="home"
            count={1}
            initialRect={{ width: 800, height: 400 }}
            onConfirm={onConfirm}
            onCancel={onCancel}
            {...over}
          />
        </MemoryRouter>
      </LimitsProvider>
    </QueryClientProvider>,
  );
  return { onConfirm, onCancel };
}

/**
 * The dialog, once it has taken focus.
 *
 * The modal's focus trap moves focus into the dialog one animation frame AFTER it
 * opens, and the first focusable thing inside is the close button, not the search
 * field. A case that reaches the field before that frame lands has its keystrokes
 * taken off it mid-word — `userEvent.type` clicks the field, the frame then fires
 * and re-focuses the close button, and the rest of the word is typed at a button:
 * the field stays empty, no search is ever asked for, and the row the case waits
 * for never arrives. On a quiet machine the frame is long gone by the time the
 * first row renders; on a loaded one it is not, which is the whole flake. So every
 * case waits for the move to have happened before it touches anything — focus
 * inside the dialog is the observable proof that the one-shot frame has run and
 * cannot fire again.
 */
async function openedDialog(name: RegExp = /^Move 1 item to/): Promise<HTMLElement> {
  const dialog = await screen.findByRole("dialog", { name });
  await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  return dialog;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the folder picker", () => {
  it("lists folders only, as rows of the browser's treegrid, and asks the server for folders", async () => {
    const urls = stubApi();
    mount();

    const dialog = await openedDialog();
    const grid = await within(dialog).findByRole("treegrid", { name: "Folders" });
    expect(within(grid).getByText("archive")).toBeInTheDocument();
    expect(within(grid).getByText("budgets")).toBeInTheDocument();
    expect(within(dialog).queryByText("report.csv")).not.toBeInTheDocument();
    expect(
      urls.some((url) => url.includes("/items/nd_home/children") && url.includes("kind=folder")),
    ).toBe(true);
    // The trail names the folder open, the way the rail spells it.
    expect(within(dialog).getByRole("navigation", { name: "Destination" })).toHaveTextContent(
      "Home",
    );
  });

  it("with nothing picked, the action lands in the folder open", async () => {
    stubApi();
    const { onConfirm } = mount();
    const dialog = await openedDialog();
    await within(dialog).findByText("archive");

    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));

    expect(onConfirm).toHaveBeenCalledWith("nd_home", undefined, undefined);
  });

  it("one click picks a folder, and the action lands in it", async () => {
    stubApi();
    const { onConfirm } = mount();
    const dialog = await openedDialog();

    await userEvent.click(await within(dialog).findByText("budgets"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));

    expect(onConfirm).toHaveBeenCalledWith("nd_b", expect.anything(), undefined);
  });

  it("opening a folder walks into it: the trail grows, the pick is forgotten, the crumb climbs back", async () => {
    stubApi();
    const { onConfirm } = mount();
    const dialog = await openedDialog();

    await userEvent.dblClick(await within(dialog).findByText("archive"));

    await within(dialog).findByText("2024");
    const trail = within(dialog).getByRole("navigation", { name: "Destination" });
    expect(trail).toHaveTextContent("Home");
    expect(trail).toHaveTextContent("archive");
    // Nothing is picked in the new folder, so the action lands in the folder open.
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));
    expect(onConfirm).toHaveBeenLastCalledWith("nd_a", undefined, undefined);

    await userEvent.click(within(trail).getByText("Home"));
    await within(dialog).findByText("budgets");
    expect(within(dialog).queryByText("2024")).not.toBeInTheDocument();
  });

  it("offers a chat as a destination, in the listing and in the drive search", async () => {
    stubApi();
    const { onConfirm } = mount();
    const dialog = await openedDialog();
    const grid = await within(dialog).findByRole("treegrid", { name: "Folders" });

    // Putting a file in a chat is how a person hands it to the conversation: the
    // chat is the directory its agent runs in, so it is a place like any folder.
    expect(within(grid).getByText("archive")).toBeInTheDocument();
    expect(within(grid).getByText("Q3 revenue.alkerachat")).toBeInTheDocument();

    // Picking it hands back the chat's own node id AND the row, so the caller can
    // name where the file went without re-reading it.
    await userEvent.click(within(grid).getByText("Q3 revenue.alkerachat"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));
    expect(onConfirm).toHaveBeenCalledWith(
      "nd_chat",
      expect.objectContaining({ id: "nd_chat", nameDisplay: "Q3 revenue.alkerachat" }),
      undefined,
    );

    // The drive search is the other way a chat reaches the grid.
    await userEvent.type(within(dialog).getByRole("searchbox", { name: "Find a folder" }), "bud");
    await within(dialog).findByText("budgets-2023");
    expect(within(dialog).getByText("budget review.alkerachat")).toBeInTheDocument();
  });

  it("offers a chat template as a destination", async () => {
    stubApi();
    const { onConfirm } = mount();
    const dialog = await openedDialog();
    const grid = await within(dialog).findByRole("treegrid", { name: "Folders" });

    // Dropping a file into a template is how a person adds to what a new chat
    // started from it will begin with, so it is a place like a chat.
    await within(grid).findByText("archive");
    await userEvent.click(within(grid).getByText("Monthly revenue.alkerachat.template"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));
    expect(onConfirm).toHaveBeenCalledWith(
      "nd_tpl",
      expect.objectContaining({ id: "nd_tpl" }),
      undefined,
    );
  });

  it("still refuses an object that is neither a chat nor a template", async () => {
    stubApi();
    mount();
    const dialog = await openedDialog();
    const grid = await within(dialog).findByRole("treegrid", { name: "Folders" });

    // A saved query folder holds a spec a person does not drop things beside, and
    // nothing runs in it — so it is the thing it is, not a place.
    await within(grid).findByText("archive");
    expect(within(dialog).queryByText("Revenue by region.alkeraquery")).not.toBeInTheDocument();
  });

  it("Cancel confirms nothing", async () => {
    stubApi();
    const { onConfirm, onCancel } = mount();
    const dialog = await openedDialog();
    await within(dialog).findByText("archive");

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("says what it is for when the caller names it", async () => {
    stubApi();
    mount({ title: "Restore to…", confirmLabel: "Restore here" });
    const dialog = await openedDialog(/^Restore to/);
    expect(within(dialog).getByRole("button", { name: "Restore here" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Move here" })).not.toBeInTheDocument();
  });

  it("finds a folder anywhere in the drive by name, shows where it lives, and lands in it", async () => {
    const urls = stubApi();
    const { onConfirm } = mount();
    const dialog = await openedDialog();
    await within(dialog).findByText("archive");

    await userEvent.type(within(dialog).getByRole("searchbox", { name: "Find a folder" }), "bud");

    // The drive search, folders only — not a filter over the folder open.
    const hit = await within(dialog).findByText("budgets-2023");
    expect(hit).toBeInTheDocument();
    expect(within(dialog).getByText("/home/dana/old")).toBeInTheDocument();
    expect(within(dialog).queryByText("budget.csv")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("archive")).not.toBeInTheDocument();
    // Every ask the typing produced is drive-wide and folders-only, and the last
    // one -- the one the rows on screen answer -- carries the whole word.
    const asks = urls.filter((url) => url.includes("/search"));
    expect(asks.length).toBeGreaterThan(0);
    for (const ask of asks) {
      expect(ask).toContain("kind=folder");
      expect(ask).not.toContain("scope=folder");
    }
    expect(asks.at(-1)).toContain("q=bud");
    // The trail stays where it was — nothing above the grid moves while typing.
    expect(within(dialog).getByRole("navigation", { name: "Destination" })).toHaveTextContent(
      "Home",
    );

    // Nothing picked while searching → nothing to land in; a picked match → that folder.
    expect(within(dialog).getByRole("button", { name: "Move here" })).toBeDisabled();
    await userEvent.click(hit);
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));
    expect(onConfirm).toHaveBeenCalledWith("nd_far", expect.anything(), undefined);
  });

  it("opening a search hit walks into it and the trail starts over there", async () => {
    stubApi();
    mount();
    const dialog = await openedDialog();
    await within(dialog).findByText("archive");
    const field = within(dialog).getByRole("searchbox", { name: "Find a folder" });
    await userEvent.type(field, "bud");

    await userEvent.dblClick(await within(dialog).findByText("budgets-2023"));

    expect(field).toHaveValue("");
    const trail = await within(dialog).findByRole("navigation", { name: "Destination" });
    expect(trail).toHaveTextContent("budgets-2023");
    expect(trail).not.toHaveTextContent("Home");
  });

  it("finds folders with the UI library's search field", async () => {
    stubApi();
    mount();
    const dialog = await openedDialog();
    const field = within(dialog).getByRole("searchbox", { name: "Find a folder" });
    expect(field).toHaveClass("alk-input");
    // The field fills the picker's width rather than the library's toolbar default.
    expect(field.closest(".alk-files-picker__search")).not.toBeNull();
  });

  it("Escape empties the search and brings the folder open back", async () => {
    stubApi();
    const { onCancel } = mount();
    const dialog = await openedDialog();
    await within(dialog).findByText("archive");
    const field = within(dialog).getByRole("searchbox", { name: "Find a folder" });
    await userEvent.type(field, "bud");
    await within(dialog).findByText("budgets-2023");

    await userEvent.keyboard("{Escape}");

    expect(field).toHaveValue("");
    await within(dialog).findByText("archive");
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("says so when a folder holds no folders", async () => {
    stubApi();
    mount({ startFolderId: "nd_empty", startLabel: "empty" });
    const dialog = await openedDialog();
    await waitFor(() => expect(within(dialog).getByText("No folders here.")).toBeInTheDocument());
  });
});
