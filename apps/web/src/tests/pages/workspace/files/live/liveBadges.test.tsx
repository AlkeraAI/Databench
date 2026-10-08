// What a reader is told about a leased folder, and about one row inside it.
//
// The badge must never claim "Live" for a state that is not live — a stream
// that is down beats the lease, because the browser has no way to learn about a
// save — and the row chip must stay silent for a state this build has no
// sentence for rather than printing the server's raw spelling at a person.

import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { LiveBadge } from "@/pages/workspace/files/live/LiveBadge";
import { LiveRowChip } from "@/pages/workspace/files/live/LiveRowChip";
import type { Item } from "@/api/files";
import { SAVED_COPY_LINE } from "@/pages/workspace/files/liveRoot/liveCopy";
import type { FolderLiveness } from "@/pages/workspace/files/liveRoot/liveness";

const live: FolderLiveness = {
  state: "live",
  holder: "Ada",
  machine: "the box",
  since: "2026-09-16T10:00:00Z",
  landing: 0,
  onBox: 0,
};

type LiveFacet = NonNullable<Item["live"]>;
type LeaseFacet = NonNullable<Item["lease"]>;

const facet = (over: Partial<LiveFacet>): LiveFacet => ({ state: "writing", ...over }) as LiveFacet;

afterEach(cleanup);

describe("LiveBadge", () => {
  it("names the holder and the machine while the folder is live", () => {
    render(<LiveBadge liveness={live} />);
    expect(screen.getByRole("status").textContent).toContain("Live. Ada is working on the box");
  });

  it("names the chat holding the folder instead of the machine, as the link to it", () => {
    render(
      <MemoryRouter>
        <LiveBadge liveness={live} chat={{ chatId: "ch 1", title: "Q3 review", canOpen: true }} />
      </MemoryRouter>,
    );
    const badge = screen.getByRole("status");
    expect(badge.textContent).toContain("Live. Ada is working on Q3 review");
    expect(badge.textContent).not.toContain("the box");
    // The id is escaped into the route rather than pasted into it.
    expect(within(badge).getByRole("link", { name: "Q3 review" })).toHaveAttribute(
      "href",
      "/chat/ch%201",
    );
  });

  it("says 'this chat' with nothing to click when the listing IS that chat's files", () => {
    render(
      <MemoryRouter>
        <LiveBadge
          liveness={live}
          chat={{ chatId: "ch_1", title: "Q3 review", canOpen: true }}
          inChat
        />
      </MemoryRouter>,
    );
    const badge = screen.getByRole("status");
    expect(badge.textContent).toContain("Live. Ada is working on this chat");
    expect(within(badge).queryByRole("link")).toBeNull();
  });

  it("capitalises the holder the drive could name only as someone", () => {
    render(<LiveBadge liveness={{ ...live, holder: "someone" }} />);
    expect(screen.getByRole("status").textContent).toContain("Live. Someone is working on");
  });

  it("counts what is landing and what is held back, and says nothing when there is nothing", () => {
    const { container, rerender } = render(<LiveBadge liveness={live} />);
    expect(container.querySelectorAll(".alk-files-live__note")).toHaveLength(0);

    rerender(<LiveBadge liveness={{ ...live, landing: 3, onBox: 2 }} />);
    const notes = [...container.querySelectorAll(".alk-files-live__note")].map(
      (n) => n.textContent,
    );
    expect(notes).toEqual(["Live · 3 files syncing", "Live · 2 files on the machine"]);
  });

  it("stops claiming live when the event stream is down, whatever the lease says", () => {
    const { container } = render(<LiveBadge liveness={live} streamDown />);
    expect(screen.getByRole("status").textContent).toBe(SAVED_COPY_LINE);
    expect(container.querySelector(".alk-files-live")?.getAttribute("data-state")).toBe(
      "persisted",
    );
    // A count off a lease the browser can no longer hear about would be a
    // number that quietly stops moving.
    expect(container.querySelectorAll(".alk-files-live__note")).toHaveLength(0);
  });

  it("says when the copy was written, in the caller's own spelling of a time", () => {
    render(
      <LiveBadge
        liveness={{ state: "persisted", asOf: "2026-09-16T10:05:00Z", reason: "no-lease" }}
        formatTime={() => "10:05"}
      />,
    );
    expect(screen.getByRole("status").textContent).toBe("Last saved 10:05");
  });

  it("says a machine that stopped answering is offline, with no dot and no counts", () => {
    const { container } = render(
      <LiveBadge
        liveness={{
          state: "persisted",
          asOf: "2026-09-16T12:04:00Z",
          reason: "offline",
          machine: "demo-box",
        }}
        formatTime={() => "12:04"}
      />,
    );
    expect(screen.getByRole("status").textContent).toBe("Offline since 12:04 · showing last sync");
    expect(container.querySelector(".alk-files-live__dot")).toBeNull();
    expect(container.querySelectorAll(".alk-files-live__note")).toHaveLength(0);
  });

  it("marks the two moving states with a dot and a settled one without", () => {
    const { container, rerender } = render(<LiveBadge liveness={live} />);
    expect(container.querySelector(".alk-files-live__dot")).not.toBeNull();

    rerender(<LiveBadge liveness={{ state: "handing-back", holder: "Ada", machine: "the box" }} />);
    expect(screen.getByRole("status").textContent).toBe("Handing back…");
    expect(container.querySelector(".alk-files-live__dot")).not.toBeNull();

    rerender(<LiveBadge liveness={{ state: "persisted", asOf: null, reason: "stale" }} />);
    expect(container.querySelector(".alk-files-live__dot")).toBeNull();
  });
});

describe("LiveRowChip", () => {
  it("renders nothing for a row the machine is not touching", () => {
    const { container } = render(<LiveRowChip live={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("renders nothing for a state this build has no sentence for", () => {
    const { container } = render(<LiveRowChip live={facet({ state: "teleporting" } as never)} />);
    expect(container.innerHTML).toBe("");
  });

  it("spells each state a reader can act on", () => {
    const cases: Array<[NonNullable<LiveFacet["state"]>, string]> = [
      ["writing", "writing…"],
      ["uploading", "uploading…"],
    ];
    for (const [state, text] of cases) {
      const { container, unmount } = render(<LiveRowChip live={facet({ state })} />);
      expect(container.textContent, state).toBe(text);
      unmount();
    }
  });

  it("names the size only for a file left on the machine", () => {
    const onBox = render(<LiveRowChip live={facet({ state: "on_box", box_size: 5_242_880 })} />);
    expect(onBox.container.textContent).toBe("on the machine (5.2 MB)");
    onBox.unmount();

    const deferred = render(<LiveRowChip live={facet({ state: "deferred", box_size: null })} />);
    expect(deferred.container.textContent).toBe("on the machine");
    deferred.unmount();

    // A size on a file that is already moving is a number the reader cannot use.
    const uploading = render(<LiveRowChip live={facet({ state: "uploading", box_size: 99 })} />);
    expect(uploading.container.textContent).toBe("uploading…");
  });
});

describe("LiveRowChip, by where the row's bytes stand", () => {
  const lease = (over: Partial<LeaseFacet> = {}): LeaseFacet =>
    ({
      holder: "Ada",
      machine: "box-1",
      machine_name: "demo-box",
      ...over,
    }) as LeaseFacet;
  const content = (over: Partial<LiveFacet>): LiveFacet =>
    ({ state: "writing", ...over }) as LiveFacet;
  const chip = (live: LiveFacet, on: LeaseFacet | null = lease()) => {
    const { container, unmount } = render(<LiveRowChip live={live} lease={on} />);
    const text = container.textContent;
    const state = container.querySelector(".alk-files-live-chip")?.getAttribute("data-state");
    unmount();
    return { text, state };
  };

  it("names the machine only for bytes it will not send", () => {
    // `writing` is the facet's default state, so a row the plane never touched
    // carries it too: the content word has to win over it.
    expect(chip(content({ content: "unsynced" }))).toEqual({
      text: "left on demo-box, not saved",
      state: "unsynced",
    });
  });

  it.each(["behind", "unlanded"] as const)("a %s row says nothing about where its bytes stand", (state) => {
    // Awake, the folder is served from the machine; asleep, the release made
    // the drive current. Either way the word would name nothing to act on, and
    // the folder's own line says when what is listed is the last synced copy.
    expect(chip(content({ content: state })).text).toBe("");
    expect(chip(content({ content: state }), lease({ served: "offline" } as never)).text).toBe("");
  });

  it.each([
    ["on_drive", "writing"],
    ["on_drive", "uploading"],
    ["none", "writing"],
    ["none", "on_box"],
  ] as const)("a %s row says nothing, whatever its %s state", (state, plane) => {
    expect(chip(content({ content: state, state: plane })).text).toBe("");
  });

  it("names the machine by what it IS when the server resolved no name", () => {
    const anonymous = lease({ machine: "807bf89a-464c-4867-8b08-6e020a9bd8a3", machine_name: "" });
    expect(chip(content({ content: "unsynced" }), anonymous).text).toBe(
      "left on the workspace machine, not saved",
    );
    expect(chip(content({ content: "unsynced" }), null).text).toBe(
      "left on the workspace machine, not saved",
    );
  });

  it.each([["inbound"], ["inbound_delete"], ["inbound_rename"]] as const)(
    "a write of the reader's own on its way to the machine (%s) says nothing",
    (state) => {
      // It settles within moments; a chip for it read as something wrong.
      expect(chip(content({ state, content: "behind" })).text).toBe("");
    },
  );

  it("a server that sends no content word keeps the plane's word", () => {
    expect(chip(content({ state: "uploading" })).text).toBe("uploading…");
    expect(chip(content({ state: "on_box", box_size: 5_242_880 })).text).toBe(
      "on the machine (5.2 MB)",
    );
  });
});

describe("LiveRowChip in a listing", () => {
  const lease = (over: Partial<LeaseFacet> = {}): LeaseFacet =>
    ({
      holder: "Ada",
      machine: "box-1",
      machine_name: "demo-box",
      ...over,
    }) as LeaseFacet;
  const content = (over: Partial<LiveFacet>): LiveFacet =>
    ({ state: "writing", ...over }) as LiveFacet;
  const text = (live: LiveFacet, on: LeaseFacet) => {
    const { container, unmount } = render(<LiveRowChip live={live} lease={on} />);
    const said = container.textContent;
    unmount();
    return said;
  };

  it("still says what the machine will not send, live or not", () => {
    expect(text(content({ content: "unsynced" }), lease({ served: "live" }))).toBe(
      "left on demo-box, not saved",
    );
  });

  it("yields its width to the name in a listing", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    // The chip's own sheet — the one every listing that draws it imports —
    // and not a page's, which is absent wherever that page is not open.
    const css = readFileSync(
      join(process.cwd(), "src/pages/workspace/files/live/live.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    const start = css.indexOf('.alk-files-grid__cell[data-column="name"] .alk-files-live-chip');
    expect(start).toBeGreaterThan(-1);
    const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
    // A chip that could not shrink pushed the name down to "geo…" beside itself.
    expect(body).not.toMatch(/flex:\s*0 0 auto/);
    expect(body).toMatch(/min-width:\s*0/);
    expect(body).toMatch(/max-width:\s*\d+%/);
    expect(body).toMatch(/text-overflow:\s*ellipsis/);
  });
});
