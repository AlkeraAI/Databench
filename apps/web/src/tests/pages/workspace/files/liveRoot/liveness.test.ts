// @vitest-environment node
//
// "Leased" means a live machine is holding these files and writing them back.
// What a person needs to know is narrower than the lease row: are they looking
// at what the machine has right now, or at the last copy that reached storage?
// The server decides that and puts it on the lease facet as a status; `liveState`
// turns it into the verdict the copy is written from, as one pure function, so
// the rail, the folder header and the preview modal can never disagree.

import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import {
  chatLeaseOf,
  isHeldByMachine,
  liveState,
  type FolderLiveness,
} from "@/pages/workspace/files/liveRoot/liveness";
import {
  fetchingLine,
  livenessLabel,
  staleCopyLine,
  livenessLine,
  liveContentChip,
  livenessNotes,
  liveRowChip,
  liveStatus,
  SAVED_COPY_LINE,
  THIS_CHAT,
} from "@/pages/workspace/files/liveRoot/liveCopy";

import { filesLiveFact } from "../../../../fixtures/statusFacts";

const NOW = Date.parse("2026-09-16T12:00:00Z");
const SOON = "2026-09-16T12:01:00Z";
const PAST = "2026-09-16T11:59:00Z";
const SYNCED = "2026-09-16T11:58:00Z";
const MTIME = "2026-09-16T09:30:00Z";
/** What a box's lease actually carries: the allocation's uuid, twice — it holds
 *  its own lease, so the holder and the machine are the same id. */
const MACHINE_ID = "807bf89a-464c-4867-8b08-6e020a9bd8a3";
/** An id anywhere inside a line a person is shown. */
const ANY_ID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

type Facet = Record<string, unknown>;

function node(lease: Facet | null, over: Record<string, unknown> = {}): Item {
  return {
    id: "n1",
    driveId: "d1",
    kind: "folder",
    name: "Q3 review.alkerachat",
    stale: false,
    attrs: { mtime: MTIME },
    ...(lease ? { lease } : {}),
    ...over,
  } as unknown as Item;
}

const held = (over: Facet = {}): Facet => ({
  holder: "Robin",
  machine: "box-1",
  purpose: "box",
  mine: false,
  since: "2026-09-16T11:40:00Z",
  expires_at: SOON,
  last_sync_at: SYNCED,
  live: true,
  pending: 0,
  status: filesLiveFact("live"),
  ...over,
});

/** The server's status on a lease, by the shared fixture's name for it. */
const paused = (reason: "behind" | "silent" | "unreachable"): Facet => ({
  status: filesLiveFact(`paused_${reason}`),
});
const savedCopy: Facet = { live: false, status: filesLiveFact("saved_copy") };

/** A hand-back verdict for the copy tests. No server status produces it yet;
 *  the sentences for it are still the owner's and stay pinned. */
function handingBack(lease: Facet, onBox = 0): FolderLiveness {
  const live = liveState(node(lease), NOW, onBox);
  if (live.state !== "live") throw new Error("expected a live lease to hand back");
  return { state: "handing-back", holder: live.holder, machine: live.machine };
}

describe("liveState", () => {
  it("a node with no lease is the saved copy", () => {
    expect(liveState(node(null), NOW)).toEqual({
      state: "persisted",
      asOf: MTIME,
      reason: "no-lease",
    });
  });

  it("a node with no lease and no mtime says so rather than inventing a time", () => {
    expect(liveState(node(null, { attrs: {} }), NOW)).toEqual({
      state: "persisted",
      asOf: null,
      reason: "no-lease",
    });
  });

  it("a lease that has run out is not live, whatever its flag says", () => {
    // An expired lease row outlives the machine that held it: the reaper has not
    // run yet. Reading `live: true` off it would paint a live badge over a box
    // that is gone.
    expect(liveState(node(held({ expires_at: PAST })), NOW)).toEqual({
      state: "persisted",
      asOf: SYNCED,
      reason: "not-live",
    });
  });

  it("expiry is decided at the instant, not around it", () => {
    const atTheTick = "2026-09-16T12:00:00Z";
    expect(liveState(node(held({ expires_at: atTheTick })), NOW).state).toBe("persisted");
    expect(liveState(node(held({ expires_at: atTheTick })), NOW - 1).state).toBe("live");
  });

  it.each([
    { name: "a holder that is behind", reason: "behind", reads: "stale" },
    { name: "a holder that went quiet", reason: "silent", reads: "offline" },
    {
      name: "a machine that stopped answering",
      reason: "unreachable",
      reads: "machine-unreachable",
    },
  ] as const)("a paused sync for $name is the last sync", ({ reason, reads }) => {
    // The server judges the beat, the last sync and the machine; the page
    // only picks the sentence its reason is told with.
    expect(liveState(node(held(paused(reason))), NOW)).toMatchObject({
      state: "persisted",
      asOf: SYNCED,
      reason: reads,
    });
  });

  it("the raw fields alone never make a lease live or paused", () => {
    // Fields that once decided the verdict here now decide nothing: only the
    // server's status does.
    expect(liveState(node(held({ served: "offline" }), { stale: true }), NOW).state).toBe("live");
    expect(liveState(node(held({ ...paused("silent"), served: "live" })), NOW)).toMatchObject({
      reason: "offline",
    });
  });

  it("a lease with no status, or one this build does not know, is never shown live", () => {
    expect(liveState(node(held({ status: undefined })), NOW)).toEqual({
      state: "persisted",
      asOf: SYNCED,
      reason: "not-live",
    });
    const unknown = { ...filesLiveFact("live"), state: "something_new" };
    expect(liveState(node(held({ status: unknown })), NOW)).toMatchObject({ reason: "not-live" });
  });

  it("a paused reason this build does not know reads as the machine gone quiet", () => {
    const unknown = { ...filesLiveFact("paused_silent"), reason_code: "something_new" };
    expect(liveState(node(held({ status: unknown })), NOW)).toMatchObject({ reason: "offline" });
  });

  it("a lease that is not on the live plane shows the saved copy", () => {
    // A `mount` lease writes back on a checkpoint, not continuously. Its files
    // are real, they are just not this second's.
    expect(liveState(node(held({ ...savedCopy, purpose: "mount" })), NOW)).toEqual({
      state: "persisted",
      asOf: SYNCED,
      reason: "not-live",
    });
  });

  it("a live lease carries who is holding it, what is landing, and what is still on the machine", () => {
    expect(liveState(node(held({ landing_count: 3 })), NOW, 2)).toEqual({
      state: "live",
      holder: "Robin",
      machine: "box-1",
      since: "2026-09-16T11:40:00Z",
      landing: 3,
      onBox: 2,
    });
  });

  it.each([
    {
      name: "the server's landing count over the in-flight rows",
      lease: { landing_count: 4, pending: 9 },
      landing: 4,
    },
    {
      name: "a landing count of zero, not the in-flight rows",
      lease: { landing_count: 0, pending: 9 },
      landing: 0,
    },
    {
      name: "the in-flight rows from a server with no landing count",
      lease: { pending: 9 },
      landing: 9,
    },
    { name: "nothing when neither is a number", lease: { pending: "9" }, landing: 0 },
  ])("counts $name", ({ lease, landing }) => {
    expect(liveState(node(held(lease)), NOW)).toMatchObject({ state: "live", landing });
  });

  it("a holder gone quiet is the last sync, naming the machine", () => {
    expect(
      liveState(node(held({ ...paused("silent"), machine_name: "demo-box" })), NOW),
    ).toEqual({
      state: "persisted",
      asOf: SYNCED,
      reason: "offline",
      machine: "demo-box",
    });
  });

  it.each([
    {
      name: "a checkpoint lease, which is never served live",
      lease: savedCopy,
      reason: "not-live",
    },
    {
      name: "an expired lease",
      lease: { expires_at: PAST, ...paused("silent") },
      reason: "not-live",
    },
  ])("offline is not what $name reads as", ({ lease, reason }) => {
    expect(liveState(node(held(lease)), NOW)).toMatchObject({ state: "persisted", reason });
  });

  it("a live lease with nothing named still names somebody", () => {
    // The facet defaults both to the empty string; a badge reading
    // "Live.  is working on " is worse than a vague one.
    const bare = liveState(node(held({ holder: "", machine: "", since: null })), NOW);
    expect(bare).toMatchObject({ state: "live", landing: 0, onBox: 0, since: null });
    if (bare.state !== "live") throw new Error("expected live");
    expect(bare.holder).not.toBe("");
    expect(bare.machine).not.toBe("");
  });

  it("a lease with no expiry is read as live rather than as expired", () => {
    // `expires_at` is optional on the facet. Treating an absent one as "in the
    // past" would make every lease that omits it read as the saved copy.
    expect(liveState(node(held({ expires_at: null })), NOW).state).toBe("live");
  });

  it("an expired lease reads as expired, whatever the server said when it sent it", () => {
    // The item the page holds outlived its lease; the status on it is from
    // before the lease ended.
    const both = liveState(node(held({ expires_at: PAST, ...paused("unreachable") })), NOW);
    expect(both).toMatchObject({ state: "persisted", reason: "not-live" });
  });
});

describe("isHeldByMachine", () => {
  // The predicate that makes a folder read-only on the Files page. It is the
  // lease, not the badge: a rule inverted here changes what a person is
  // allowed to do to a folder for the whole time a machine holds it.
  it.each([
    ["a live lease", held(), {}, true],
    ["a hand-back in progress", held({ grantable_after: SOON }), {}, true],
    // The user's rule: read-only "until the lease on the folder ends". A holder
    // that is behind has not let go, so the folder is still its writer.
    ["a holder that stopped syncing", held(), { stale: true }, true],
    ["a live lease with no expiry", held({ expires_at: null }), {}, true],
    ["an expired lease", held({ expires_at: PAST }), {}, false],
    [
      "an expired lease whose holder is also behind",
      held({ expires_at: PAST }),
      { stale: true },
      false,
    ],
    ["a checkpointing lease", held({ live: false }), {}, false],
    ["a checkpointing lease whose holder is behind", held({ live: false }), { stale: true }, false],
  ])("%s is held: %s", (_name, lease, over, expected) => {
    expect(isHeldByMachine(node(lease as Facet, over as Facet), NOW)).toBe(expected);
  });

  it("is decided at the instant, like expiry", () => {
    const atTheTick = "2026-09-16T12:00:00Z";
    expect(isHeldByMachine(node(held({ expires_at: atTheTick })), NOW)).toBe(false);
    expect(isHeldByMachine(node(held({ expires_at: atTheTick })), NOW - 1)).toBe(true);
  });

  it("a folder with no lease at all is not held, and neither is no folder", () => {
    expect(isHeldByMachine(node(null), NOW)).toBe(false);
    expect(isHeldByMachine(undefined, NOW)).toBe(false);
  });

  it("does not follow the badge: a stale holder reads as the saved copy and is still held", () => {
    // The two verdicts are deliberately different questions. The badge says
    // what the rows are; this says who may write them.
    const behind = node(held(paused("behind")), { stale: true });
    expect(liveState(behind, NOW)).toMatchObject({ state: "persisted", reason: "stale" });
    expect(isHeldByMachine(behind, NOW)).toBe(true);
  });
});

describe("chatLeaseOf", () => {
  it("names the chat, its title and the reader's right to open it off the facet", () => {
    expect(
      chatLeaseOf(node(held({ chat_id: "ch_1", chat_title: "Q3 review", can_open_chat: true }))),
    ).toEqual({ chatId: "ch_1", title: "Q3 review", canOpen: true });
  });

  it("withholds the link on anything but the server's own true", () => {
    for (const denied of [false, undefined, "true", 1]) {
      const chat = chatLeaseOf(node(held({ chat_id: "ch_1", can_open_chat: denied })));
      expect(chat?.canOpen).toBe(false);
    }
  });

  it("is nothing for a lease that is not a chat's, and for no lease", () => {
    expect(chatLeaseOf(node(held()))).toBeNull();
    expect(chatLeaseOf(node(held({ chat_id: "" })))).toBeNull();
    expect(chatLeaseOf(node(held({ chat_id: 7 })))).toBeNull();
    expect(chatLeaseOf(node(null))).toBeNull();
    expect(chatLeaseOf(undefined)).toBeNull();
  });

  it("keeps an empty title as empty rather than inventing one", () => {
    expect(chatLeaseOf(node(held({ chat_id: "ch_1" })))?.title).toBe("");
  });
});

describe("liveCopy", () => {
  it("names the holder and the machine while it is live", () => {
    const live = liveState(node(held()), NOW);
    expect(livenessLabel(live)).toBe("Live. Robin is working on box-1");
  });

  it("counts what is in flight and what is still on the machine, and says neither when there is nothing", () => {
    expect(livenessNotes(liveState(node(held({ landing_count: 2 })), NOW, 1))).toEqual([
      "Live · 2 files syncing",
      "Live · 1 file on the machine",
    ]);
    expect(livenessNotes(liveState(node(held()), NOW, 0))).toEqual([]);
  });

  it("names one file in the singular and says what is happening to it", () => {
    expect(livenessNotes(liveState(node(held({ landing_count: 1 })), NOW, 2))).toEqual([
      "Live · 1 file syncing",
      "Live · 2 files on the machine",
    ]);
  });

  it("says when the copy is the last one saved, and when the machine stopped syncing", () => {
    const at = (iso: string): string => `at ${iso}`;
    expect(livenessLabel(liveState(node(null), NOW), { formatTime: at })).toBe(
      `Last saved at ${MTIME}`,
    );
    expect(
      livenessLabel(liveState(node(held(paused("behind")), { stale: true }), NOW), {
        formatTime: at,
      }),
    ).toBe(SAVED_COPY_LINE);
  });

  it("the saved-copy line says what the copy is and when it stops being one", () => {
    // Dictated wording. Pinned literally rather than through the constant, so
    // rewording the constant fails here instead of quietly changing the screen.
    expect(SAVED_COPY_LINE).toBe(
      "This is the last synced copy to your Files and is not always up to date until the lease on the folder ends.",
    );
  });

  it("a saved copy with no time says so without a dangling 'since'", () => {
    expect(livenessLabel(liveState(node(null, { attrs: {} }), NOW))).toBe("Last saved");
  });

  it("says the view is unavailable when the event stream is down, whatever the lease says", () => {
    // With no stream the browser cannot know the folder changed. Claiming
    // "Live" then is a promise the page cannot keep.
    expect(livenessLabel(liveState(node(held()), NOW), { streamDown: true })).toBe(SAVED_COPY_LINE);
  });

  it("no sentence a reader is shown carries an em dash", () => {
    // Every state the label can take, and every chip and note beside it. The
    // dash reads as machine-written, and a new state that reintroduces one is
    // caught here rather than on screen.
    const at = (iso: string): string => `at ${iso}`;
    const labels = [
      livenessLabel(liveState(node(held()), NOW), { formatTime: at }),
      livenessLabel(handingBack(held()), { formatTime: at }),
      livenessLabel(liveState(node(held(paused("behind")), { stale: true }), NOW), {
        formatTime: at,
      }),
      livenessLabel(liveState(node(held({ expires_at: PAST })), NOW), { formatTime: at }),
      livenessLabel(liveState(node(held(savedCopy)), NOW), { formatTime: at }),
      livenessLabel(liveState(node(null), NOW), { formatTime: at }),
      livenessLabel(liveState(node(null, { attrs: {} }), NOW), { formatTime: at }),
      livenessLabel(liveState(node(held()), NOW), { streamDown: true, formatTime: at }),
      ...livenessNotes(liveState(node(held({ landing_count: 2 })), NOW, 1)),
      livenessLabel(liveState(node(held({ served: "offline", ...paused("silent") })), NOW), {
        formatTime: at,
      }),
      livenessLabel(
        liveState(node(held({ served: "offline", ...paused("silent"), last_sync_at: null })), NOW),
      ),
      ...[
        "writing",
        "uploading",
        "on_box",
        "deferred",
        "inbound",
        "inbound_delete",
        "inbound_rename",
      ]
        .map((state) => liveRowChip(state, "2 GB"))
        .filter((chip): chip is string => chip !== null),
      ...["unlanded", "behind", "unsynced"]
        .map((content) => liveContentChip(content, "demo-box"))
        .filter((chip): chip is string => chip !== null),
      ...everyLine(held).filter((line) => line !== ""),
    ];
    // Every state above really produced a sentence, so the scan is not passing
    // on an empty list.
    expect(labels.length).toBeGreaterThanOrEqual(18);
    for (const line of labels) {
      expect(line).not.toBe("");
      expect(line).not.toMatch(/[—–]/);
    }
  });

  it("capitalises the holder that leads the sentence, and leaves a resolved name alone", () => {
    // The drive falls back to "someone" when it resolved no name, and that word
    // opens the sentence. Shipped lowercase: "Live. someone is working on …".
    const anonymous = liveState(node(held({ holder: "" })), NOW);
    expect(livenessLabel(anonymous)).toBe("Live. Someone is working on box-1");
    // A name the server did resolve keeps its own spelling — only the first
    // letter is ever touched, so "McCall" does not become "Mccall".
    expect(livenessLabel(liveState(node(held({ holder_name: "McCall" })), NOW))).toBe(
      "Live. McCall is working on box-1",
    );
    // The machine sits mid-sentence, where the grammar wants it lowercase, and
    // its own fallback stays that way.
    expect(livenessLabel(liveState(node(held({ holder: "", machine: "" })), NOW))).toBe(
      "Live. Someone is working on the workspace machine",
    );
  });

  it("names the chat that holds the folder instead of the machine, and links to it", () => {
    const chat = { chatId: "ch_1", title: "Q3 review", canOpen: true };
    const live = liveState(node(held({ holder: "" })), NOW);
    expect(livenessLine(live, { chat })).toEqual({
      lead: "Live. Someone is working on ",
      subject: { text: "Q3 review", href: "/chat/ch_1" },
    });
    expect(livenessLabel(live, { chat })).toBe("Live. Someone is working on Q3 review");
  });

  it("names a chat the reader may not open without a link, and an untitled one as a chat", () => {
    const live = liveState(node(held()), NOW);
    expect(
      livenessLine(live, { chat: { chatId: "ch_1", title: "Q3 review", canOpen: false } }).subject,
    ).toEqual({ text: "Q3 review", href: null });
    expect(
      livenessLine(live, { chat: { chatId: "ch_1", title: "", canOpen: true } }).subject,
    ).toEqual({ text: "a chat", href: "/chat/ch_1" });
  });

  it("says 'this chat' rather than linking the reader to what they are reading", () => {
    const live = liveState(node(held({ holder_name: "Ana" })), NOW);
    const chat = { chatId: "ch_1", title: "Q3 review", canOpen: true };
    expect(livenessLine(live, { chat, inChat: true })).toEqual({
      lead: "Live. Ana is working on ",
      subject: { text: THIS_CHAT, href: null },
    });
    expect(livenessLabel(live, { chat, inChat: true })).toBe("Live. Ana is working on this chat");
  });

  it("falls back to the machine when no chat holds the folder", () => {
    const live = liveState(node(held()), NOW);
    expect(livenessLine(live, { chat: null }).subject).toEqual({ text: "box-1", href: null });
  });

  it("carries no subject to link in a state that is not live", () => {
    const chat = { chatId: "ch_1", title: "Q3 review", canOpen: true };
    for (const state of [
      handingBack(held()),
      liveState(node(null), NOW),
      liveState(node(held(paused("behind")), { stale: true }), NOW),
    ]) {
      expect(livenessLine(state, { chat }).subject).toBeNull();
    }
    expect(
      livenessLine(liveState(node(held()), NOW), { chat, streamDown: true }).subject,
    ).toBeNull();
  });

  it("says a hand-back is in progress", () => {
    expect(livenessLabel(handingBack(held()))).toBe("Handing back…");
  });

  it.each([
    ["writing", "writing…"],
    ["uploading", "uploading…"],
  ])("a row in %s says %s", (state, expected) => {
    expect(liveRowChip(state)).toBe(expected);
  });

  it.each(["inbound", "inbound_delete", "inbound_rename"])(
    "a change of the reader's own arriving at the machine (%s) says nothing",
    (state) => {
      expect(liveRowChip(state)).toBeNull();
    },
  );

  it("a row still on the machine says how big it is", () => {
    expect(liveRowChip("on_box", "48 MB")).toBe("on the machine (48 MB)");
    expect(liveRowChip("deferred", "48 MB")).toBe("on the machine (48 MB)");
    expect(liveRowChip("on_box")).toBe("on the machine");
  });

  it("a row whose bytes the machine will not send names it", () => {
    expect(liveContentChip("unsynced", "demo-box")).toBe(
      "left on demo-box, not saved",
    );
  });

  it.each([["behind"], ["unlanded"], ["on_drive"], ["none"], [undefined], ["teleported"], [""]])(
    "a row whose content is %s says nothing",
    (content) => {
      // Awake, the folder is served from the machine and a row the drive has
      // not caught up with opens as the machine has it; asleep, the release made
      // the drive current. So bytes on their way are not news, any more than
      // the drive's copy being the machine's, the machine having no word on the
      // row, or this build predating the word.
      expect(liveContentChip(content, "demo-box")).toBeNull();
    },
  );

  it("a state this build does not know shows no chip at all", () => {
    expect(liveRowChip("teleporting")).toBeNull();
    expect(liveRowChip("")).toBeNull();
  });
});

/** The lease a box takes: it names itself by allocation id and holds its own
 *  lease, so both halves of the facet are the same uuid and a server that
 *  resolved no name leaves them that way. */
const byId = (over: Facet = {}): Facet =>
  held({ holder: MACHINE_ID, machine: MACHINE_ID, ...over });

/** Every state, each read with the stream up and with it down. */
function everyLine(lease: (over?: Facet) => Facet): string[] {
  const states = [
    liveState(node(lease()), NOW),
    liveState(node(lease({ landing_count: 2 })), NOW, 3),
    liveState(node(lease({ served: "offline", ...paused("silent") })), NOW),
    handingBack(lease()),
    liveState(node(lease(paused("behind")), { stale: true }), NOW),
    liveState(node(lease({ expires_at: PAST })), NOW),
    liveState(node(lease(savedCopy)), NOW),
    liveState(node(null), NOW),
  ];
  const lines: string[] = [];
  for (const state of states) {
    for (const streamDown of [false, true]) {
      lines.push(livenessLabel(state, { streamDown }));
      lines.push(...livenessNotes(state));
      const bar = liveStatus(state, { streamDown, savedAgo: "3 min ago" });
      lines.push(bar.label, bar.machine, bar.when, ...bar.chips);
    }
  }
  return lines;
}

describe("what a reader is never shown", () => {
  it("puts no id in any state, in any line, in either surface", () => {
    // The facet carries ids; copy that interpolates them reads
    // "807bf89a-… is working on 807bf89a-…". A state added later that forgets
    // to resolve a name fails here.
    const lines = everyLine(byId);
    // Every state above really produced lines, so the scan is not vacuous.
    expect(lines.length).toBeGreaterThanOrEqual(56);
    expect(lines.some((line) => line.includes("the workspace machine"))).toBe(true);
    for (const line of lines) expect(line).not.toMatch(ANY_ID);
  });

  it("names what the machine IS when the server resolved no name for it", () => {
    const anonymous = liveState(node(byId()), NOW);
    expect(anonymous).toMatchObject({ state: "live", machine: "the workspace machine" });
    expect(liveStatus(anonymous).machine).toBe("the workspace machine");
  });

  it("uses the name the server resolved over the id the lease fences on", () => {
    const named = liveState(
      node(byId({ holder_name: "Dana", machine_name: "demo-box" })),
      NOW,
    );
    expect(livenessLabel(named)).toBe("Live. Dana is working on demo-box");
    expect(liveStatus(named).machine).toBe("demo-box");
  });

  it("keeps a name the holder sent for itself, which is not an id", () => {
    // A laptop mount registers a hostname rather than an allocation id: there
    // is nothing for the server to resolve and nothing to hide.
    const mounted = liveState(node(held({ holder: "Dana", machine: "dana-macbook" })), NOW);
    expect(liveStatus(mounted).machine).toBe("dana-macbook");
  });
});

describe("the status bar", () => {
  it("is a state, a machine and counts, never a sentence", () => {
    const bar = liveStatus(
      liveState(node(held({ landing_count: 2, machine_name: "demo-box" })), NOW, 3),
    );
    expect(bar).toEqual({
      state: "landing",
      label: "Live",
      machine: "demo-box",
      when: "",
      chips: ["2 files syncing", "3 files on the machine"],
    });
    for (const line of [bar.label, bar.machine, ...bar.chips]) {
      expect(line).not.toMatch(/[.…]$/);
    }
  });

  it("counts nothing when there is nothing to count", () => {
    const bar = liveStatus(liveState(node(held()), NOW, 0));
    expect(bar).toMatchObject({ state: "live", label: "Live", chips: [] });
  });

  it("says a hand-back without the counts it is no longer taking", () => {
    const bar = liveStatus(handingBack(held({ landing_count: 2 }), 3));
    expect(bar).toMatchObject({ state: "handing-back", label: "Handing back", chips: [] });
  });

  it("dates the settled copy, and only the settled copy", () => {
    const settled = liveStatus(liveState(node(null), NOW), { savedAgo: "3 min ago" });
    expect(settled).toMatchObject({
      state: "persisted",
      label: "Saved copy",
      when: "Saved 3 min ago",
    });
    // A folder being written has no "when": the answer is "now".
    expect(liveStatus(liveState(node(held()), NOW), { savedAgo: "3 min ago" }).when).toBe("");
    // And the drive does not always know when the copy was written.
    expect(liveStatus(liveState(node(null, { attrs: {} }), NOW)).when).toBe("");
  });

  it("stops claiming a lease the stream cannot deliver, counts included", () => {
    const bar = liveStatus(
      liveState(node(held({ landing_count: 2, machine_name: "demo-box" })), NOW, 3),
      {
        streamDown: true,
      },
    );
    expect(bar).toMatchObject({
      state: "stream-down",
      label: "Stream down",
      // Which machine holds it is still true with the stream down; what it is
      // doing this second is not.
      machine: "demo-box",
      chips: [],
    });
  });

  it("says a browser with no network is offline and reconnecting, never live", () => {
    const bar = liveStatus(
      liveState(node(held({ landing_count: 2, machine_name: "demo-box" })), NOW, 3),
      { streamDown: true, offline: true },
    );
    expect(bar).toMatchObject({
      state: "stream-down",
      label: "Offline",
      when: "Reconnecting…",
      chips: [],
    });
    expect(bar.label).not.toBe("Live");
  });

  it.each([
    {
      name: "live with nothing landing",
      lease: { machine_name: "demo-box" },
      bar: { state: "live", label: "Live", machine: "demo-box", when: "", chips: [] },
    },
    {
      name: "live with files landing",
      lease: { machine_name: "demo-box", landing_count: 17 },
      bar: {
        state: "landing",
        label: "Live",
        machine: "demo-box",
        when: "",
        chips: ["17 files syncing"],
      },
    },
    {
      name: "offline since the last sync",
      lease: {
        machine_name: "demo-box",
        served: "offline",
        ...paused("silent"),
        landing_count: 17,
      },
      bar: {
        state: "offline",
        label: "Offline",
        machine: "demo-box",
        when: "Since 12:04 · showing last sync",
        chips: [],
      },
    },
    {
      name: "offline with no sync on record",
      lease: {
        machine_name: "demo-box",
        served: "offline",
        ...paused("silent"),
        last_sync_at: null,
      },
      bar: {
        state: "offline",
        label: "Offline",
        machine: "demo-box",
        when: "Showing last sync",
        chips: [],
      },
    },
  ])("reads $name", ({ lease, bar }) => {
    expect(
      liveStatus(liveState(node(held(lease)), NOW), {
        savedAgo: "3 min ago",
        formatTime: () => "12:04",
      }),
    ).toEqual(bar);
  });
});

describe("the folder line while the machine is offline", () => {
  it.each([
    { name: "since its last sync", lease: {}, line: "Offline since 12:04 · showing last sync" },
    {
      name: "with no sync on record",
      lease: { last_sync_at: null },
      line: "Offline · showing last sync",
    },
  ])("says it is offline $name, and what is on screen", ({ lease, line }) => {
    const offline = liveState(
      node(held({ served: "offline", ...paused("silent"), ...lease })),
      NOW,
    );
    expect(livenessLabel(offline, { formatTime: () => "12:04" })).toBe(line);
    // Nothing is moving, so there is no chat to link and nothing to count.
    expect(
      livenessLine(offline, { chat: { chatId: "ch_1", title: "Q3", canOpen: true } }).subject,
    ).toBeNull();
    expect(livenessNotes(offline)).toEqual([]);
  });
});

describe("what a preview says while the drive brings a file from the machine", () => {
  it.each([
    ["accepted", "demo-box", "Fetching from demo-box…"],
    ["timed_out", "demo-box", "Fetching from demo-box…"],
    [null, "demo-box", "Fetching from demo-box…"],
    ["offline", "demo-box", "Demo-box is offline · showing nothing yet"],
    ["busy", "demo-box", "Demo-box is busy · trying again"],
    ["throttled", "demo-box", "Demo-box is busy · trying again"],
    // The drive's fallback name opens the sentence raised, and mid-sentence stays as it is.
    ["offline", "the workspace machine", "The workspace machine is offline · showing nothing yet"],
    ["accepted", "the workspace machine", "Fetching from the workspace machine…"],
    // Only the first letter moves: a name the server resolved keeps its own casing.
    ["busy", "MacBook Pro", "MacBook Pro is busy · trying again"],
  ])("a machine that answered %s, named %s, reads %s", (outcome, machine, line) => {
    expect(fetchingLine(outcome, machine)).toBe(line);
  });
});

describe("the line over a copy older than the machine's", () => {
  it.each([
    [
      "2026-09-23T10:04:00Z",
      "demo-box",
      "Showing the copy from 12:04; demo-box has a newer one",
    ],
    [null, "demo-box", "Showing an older copy; demo-box has a newer one"],
    // Mid-sentence, the drive's fallback name stays lowercase.
    [
      "2026-09-23T10:04:00Z",
      "the workspace machine",
      "Showing the copy from 12:04; the workspace machine has a newer one",
    ],
  ])("from %s on %s reads %s", (asOf, machine, line) => {
    expect(staleCopyLine(asOf, machine, () => "12:04")).toBe(line);
  });
});

describe("the copy rules on the live view's new lines", () => {
  const at = { formatTime: () => "12:04", savedAgo: "3 min ago" };
  const offline = liveState(
    node(held({ served: "offline", ...paused("silent"), machine_name: "demo-box" })),
    NOW,
  );
  const landing = liveState(
    node(held({ landing_count: 17, machine_name: "demo-box" })),
    NOW,
    2,
  );
  const offlineBar = liveStatus(offline, at);
  const landingBar = liveStatus(landing, at);
  // Lines that open a sentence or fill a slot of their own.
  const leading = [
    livenessLabel(offline, at),
    offlineBar.label,
    offlineBar.when,
    landingBar.label,
    ...livenessNotes(landing),
    ...[null, "offline", "busy"].map((outcome) => fetchingLine(outcome, "demo-box")),
    fetchingLine("offline", "the workspace machine"),
    staleCopyLine("2026-09-23T10:04:00Z", "demo-box", () => "12:04"),
    staleCopyLine(null, "demo-box"),
  ];
  // Lines that sit beside a row, lowercase like every chip before them.
  const chips = [...landingBar.chips, liveContentChip("unsynced", "demo-box") ?? ""];

  it.each(leading.map((line) => [line]))("%s is sentence case", (line) => {
    expect(line).toMatch(/^[A-Z0-9]/);
    // Only the first word is raised; a machine's own name keeps its spelling.
    const rest = line.slice(1).replace("demo-box", "");
    expect(rest).not.toMatch(/(^|\s)[A-Z]/);
  });

  it.each([...leading, ...chips].map((line) => [line]))(
    "%s is one sentence with no filler",
    (line) => {
      expect(line).not.toBe("");
      // One sentence: no full stop inside it and none closing it.
      expect(line).not.toMatch(/\.\s|\.$/);
      expect(line).not.toMatch(/[—–]/);
      expect(line).not.toMatch(/\b(just|simply|please|don't worry|currently)\b/i);
    },
  );

  it.each(chips.map((line) => [line]))(
    "the row chip %s starts lowercase, as its neighbours do",
    (line) => {
      expect(line).toMatch(/^[a-z0-9]/);
    },
  );
});
