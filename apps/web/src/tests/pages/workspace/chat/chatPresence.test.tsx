// The faces at the top right of a chat: everyone who has it open EXCEPT you.
//
// Driven through a REAL `WsClient` over a fake socket the test speaks the
// server's side of: the component subscribes the chat's channel, joins its
// presence once the subscription is confirmed, and draws whatever the roster
// and the deltas say. A person is a person however many tabs they have, the
// face for the window the reader is looking at is never drawn — alone in a chat
// the row is empty — while a second window of their own is drawn like anyone
// else's, and past five faces the rest become a count of the OTHERS.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { setRealtimeClientForTests } from "@/api/realtime/client";
import type { PresencePeer } from "@/api/realtime/presence";
import { WsClient, parseServerFrame } from "@/api/realtime/wsClient";
import { hueOf } from "@/lib/personHue";
import { ChatPresence, presenceLabel, viewersOf } from "@/pages/workspace/chat/ChatPresence";

import { advance, socketFactory, ticketMinter } from "../../../api/realtime/fakeWebSocket";

const CHANNEL = "doc:chat:c1";
const SELF = "u_self";

function peer(
  peerId: string,
  userId: string,
  displayName?: string,
  avatarUrl?: string,
  email?: string,
): PresencePeer {
  return {
    peer_id: peerId,
    user_id: userId,
    last_seen_at: "2026-09-06T12:00:00Z",
    ...(displayName === undefined ? {} : { display_name: displayName }),
    ...(avatarUrl === undefined ? {} : { avatar_url: avatarUrl }),
    ...(email === undefined ? {} : { email }),
  };
}

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const raw = input instanceof Request ? input.url : String(input);
      const path = new URL(raw, "http://localhost").pathname;
      const answer = (payload: unknown): Response =>
        new Response(JSON.stringify(payload), { status: 200, headers: { "content-type": "application/json" } });
      if (path === "/api/v1/auth/me") return answer({ id: SELF, email: "dana@acme.test" });
      if (path === "/api/v1/chats/c1") {
        return answer({
          id: "c1",
          title: "Quarterly plan",
          owner_user_id: "u_bo",
          machine_id: null,
          machine_status: "none",
          created_at: "2026-09-06T12:00:00Z",
          updated_at: "2026-09-06T12:00:00Z",
          last_seq: 0,
          permission_mode: "read_only",
          files_node_id: null,
        });
      }
      if (path === "/api/v1/files/drives") {
        return answer({ id: "drv_1", orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
      }
      return new Response("{}", { status: 404, headers: { "content-type": "application/json" } });
    }),
  );
}

let sockets: ReturnType<typeof socketFactory>;
let client: WsClient;

beforeEach(() => {
  vi.useFakeTimers();
  stubApi();
  sockets = socketFactory();
  client = new WsClient({
    mintTicket: ticketMinter().mint,
    socketUrl: (path) => `ws://api.test${path}`,
    factory: sockets.factory,
    backoff: { jitter: () => 0 },
    heartbeatMs: 60 * 60_000,
  });
  setRealtimeClientForTests(client);
});

afterEach(() => {
  cleanup();
  setRealtimeClientForTests(null);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Mount the stack and walk the socket to a confirmed subscription. */
async function mount(): Promise<ReturnType<typeof socketFactory>["last"]> {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <ChatPresence chatId="c1" />
    </QueryClientProvider>,
  );
  await act(async () => {
    await advance(0);
  });
  await act(async () => {
    sockets.last().welcome("p:self");
    await advance(0);
  });
  await act(async () => {
    sockets.last().serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
    await advance(0);
  });
  return sockets.last;
}

function serverSend(frame: object): Promise<void> {
  return act(async () => {
    sockets.last().serverSend(frame);
    await advance(0);
  });
}

const faces = (): HTMLElement[] =>
  Array.from(document.querySelectorAll<HTMLElement>(".chat-presence__face:not(.chat-presence__more)"));

describe("the presence stack over a fake socket", () => {
  it("subscribes the chat's channel and joins once the server confirms it", async () => {
    const last = await mount();
    expect(last().framesOf("subscribe").map((f) => f.channel)).toEqual([CHANNEL]);
    expect(last().framesOf("presence.join").map((f) => f.channel)).toEqual([CHANNEL]);
  });

  it("draws the OTHER two people and not the reader", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:2", "u_bo", "Bo Chen"), peer("p:self", SELF, "Dana Okafor"), peer("p:3", "u_cleo", "Cleo Marsh")],
    });

    const drawn = faces();
    expect(drawn.map((f) => f.textContent)).toEqual(["BC", "CM"]);
    expect(drawn.map((f) => f.dataset.userId)).not.toContain(SELF);
    expect(screen.getByRole("group", { name: "Bo Chen, Cleo Marsh have this chat open" })).toBeTruthy();
    // The stack says who, not a count.
    expect(document.body.textContent).not.toContain("people reading");
  });

  it("draws nothing at all when the reader is alone in the chat", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor")],
    });

    expect(faces()).toHaveLength(0);
    // Not an empty pill and not a "+0": the row carries no presence chrome.
    expect(document.querySelector(".chat-presence")).toBeNull();
    expect(document.querySelector(".chat-presence__more")).toBeNull();
    expect(document.body.textContent).not.toContain("+0");
  });

  it("draws the reader's SECOND window and never the one they are looking at", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor"), peer("p:other", SELF, "Dana Okafor")],
    });

    // One face, for the other window — the socket this tab holds is `p:self`.
    const drawn = faces();
    expect(drawn.map((f) => f.dataset.userId)).toEqual([SELF]);
    expect(drawn.map((f) => f.textContent)).toEqual(["DO"]);
    expect(screen.getByRole("group", { name: "Dana Okafor has this chat open" })).toBeTruthy();
  });

  it("takes the face away when the reader's other window closes, not when a peer id changes", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor"), peer("p:other", SELF, "Dana Okafor")],
    });
    expect(faces()).toHaveLength(1);
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "leave",
      peers: [peer("p:other", SELF, "Dana Okafor")],
    });
    expect(faces()).toHaveLength(0);
  });

  it("says who a face is on hover, and to a reader who never sees it", async () => {
    // A face carries a picture or two initials; neither is a name. Hovering it
    // is how the row is read, so the name has to be on the element itself —
    // and the same text has to reach a reader who cannot hover at all.
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:3", "u_cleo", "Cleo Marsh"), peer("p:4", "u_fay", "Fay Wu")],
    });

    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual(["Cleo Marsh", "Fay Wu"]);
    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual(["Cleo Marsh", "Fay Wu"]);
    expect(screen.getByRole("img", { name: "Cleo Marsh" })).toBeTruthy();
  });

  it("falls back to the address, then to Someone, for a face the server could not name", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:3", "u_cleo", undefined, undefined, "cleo@acme.test"), peer("p:4", "u_fay")],
    });

    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual(["cleo@acme.test", "Someone"]);
  });

  it("names the reader's own other window as well as a colleague's", async () => {
    // The face a reader is most likely to hover is their own, and "You" answers
    // the question they did not ask: the two letters on the disc are exactly
    // what a name would have told them. So the name leads, and the window it is
    // open in follows to keep it apart from a colleague who shares it.
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:other", SELF, "Dana Okafor"), peer("p:3", "u_cleo", "Cleo Marsh")],
    });

    // Roster order is by peer id: `p:other` follows `p:3`.
    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual(["Cleo Marsh", "Dana Okafor"]);
    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual([
      "Cleo Marsh",
      "Dana Okafor",
    ]);
  });

  it("names the reader's own window by their address where the server resolved no name", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:other", SELF, undefined, undefined, "dana@acme.test")],
    });

    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual(["dana@acme.test"]);
  });

  it("still calls the reader's own window theirs when it knows neither name nor address", async () => {
    // Never a bare "(another window)" hanging off nothing.
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:other", SELF)],
    });

    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual(["You"]);
  });

  it("carries a picture inside a named face without naming it twice", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:3", "u_cleo", "Cleo Marsh", "https://pics.test/cleo.png")],
    });

    // One name on the row, not two: the face is the named thing, the picture
    // inside it is decoration.
    expect(screen.getAllByRole("img", { name: "Cleo Marsh" })).toHaveLength(1);
    expect(document.querySelector<HTMLImageElement>(".alk-avatar__picture")?.alt).toBe("");
  });

  it("names each face on hover, with the rung where it is known", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor"), peer("p:2", "u_bo", "Bo Chen"), peer("p:3", "u_cleo", "Cleo Marsh")],
    });
    // The chat row names its owner, which is the one rung a reader who cannot
    // list the node's grants still knows.
    await act(async () => {
      await advance(0);
    });
    expect(faces().map((f) => f.getAttribute("aria-label"))).toEqual(["Bo Chen", "Cleo Marsh"]);
  });

  it("keeps THIS tab off the stack and puts the reader's other tab on it", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor"), peer("p:2", "u_bo", "Bo Chen")],
    });
    // Only the socket this window holds is missing from the row…
    expect(faces().map((f) => f.textContent)).toEqual(["BC"]);

    // …so a second window of the reader's own arrives like anybody else.
    await serverSend({ t: "presence", channel: CHANNEL, event: "join", peers: [peer("p:self2", SELF, "Dana Okafor")] });
    expect(faces().map((f) => f.textContent)).toEqual(["BC", "DO"]);

    // A third window of theirs is still one person, drawn once.
    await serverSend({ t: "presence", channel: CHANNEL, event: "join", peers: [peer("p:self3", SELF, "Dana Okafor")] });
    expect(faces().map((f) => f.textContent)).toEqual(["BC", "DO"]);
  });

  it("takes this socket off the stack even before it knows whose it is", async () => {
    // A tab whose `/auth/me` has not answered yet still knows its own peer id
    // from the welcome frame — it must not draw itself in the meantime.
    render(
      <QueryClientProvider client={createQueryClient()}>
        <ChatPresence chatId="c1" />
      </QueryClientProvider>,
    );
    await act(async () => {
      await advance(0);
    });
    await act(async () => {
      sockets.last().welcome("p:mine");
      await advance(0);
    });
    await act(async () => {
      sockets.last().serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
      await advance(0);
    });
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:mine", "u_unknown", "Dana Okafor"), peer("p:2", "u_bo", "Bo Chen")],
    });
    expect(faces().map((f) => f.textContent)).toEqual(["BC"]);
  });

  it("takes a face off the stack when its person leaves", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor"), peer("p:2", "u_bo", "Bo Chen"), peer("p:3", "u_cleo", "Cleo Marsh")],
    });
    expect(faces()).toHaveLength(2);
    await serverSend({ t: "presence", channel: CHANNEL, event: "leave", peers: [peer("p:3", "u_cleo", "Cleo Marsh")] });
    expect(faces().map((f) => f.textContent)).toEqual(["BC"]);
  });

  it("draws a person's picture when their profile carries one, initials otherwise", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:3", "u_cleo", "Cleo Marsh", "https://cdn.test/cleo.png"), peer("p:2", "u_bo", "Bo Chen")],
    });
    const [bo, cleo] = faces();
    expect(cleo?.querySelector("img")?.getAttribute("src")).toBe("https://cdn.test/cleo.png");
    expect(bo?.querySelector("img")).toBeNull();
    expect(bo?.textContent).toBe("BC");
  });

  it("stops at five faces and counts the rest, the reader in neither", async () => {
    await mount();
    const names = ["Ada Ling", "Bo Chen", "Cleo Marsh", "Dev Rao", "Eve Stone", "Fay Wu"];
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor"), ...names.map((name, i) => peer(`p:${i}`, `u${i}`, name))],
    });
    expect(faces().map((f) => f.textContent)).toEqual(["AL", "BC", "CM", "DR", "ES"]);
    const more = document.querySelector<HTMLElement>(".chat-presence__more");
    // Six others, five drawn: the overflow counts the OTHERS, never the reader.
    expect(more?.textContent).toBe("+1");
    expect(more?.getAttribute("aria-label")).toBe("Fay Wu");
  });

  it("wears the same colour for the same person on every render and every screen", () => {
    expect(hueOf("u_bo")).toBe(hueOf("u_bo"));
    expect(hueOf("u_bo")).not.toBe(hueOf("u_cleo"));
    expect(hueOf("u_bo")).toBeGreaterThanOrEqual(0);
    expect(hueOf("u_bo")).toBeLessThan(360);
  });

  it("paints a face with the hue of the person, not of their place on the roster", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [
        peer("p:9", "u_bo", "Bo Chen", undefined, "bo@acme.test"),
        peer("p:1", "u_cleo", "Cleo Marsh", undefined, "cleo@acme.test"),
      ],
    });
    // The roster comes back ordered by peer id, so Cleo ("p:1") is drawn first
    // and Bo ("p:9") second — and each still wears the colour of the PERSON.
    const drawn = faces();
    expect(drawn.map((f) => f.dataset.userId)).toEqual(["u_cleo", "u_bo"]);
    const hues = drawn.map((f) => f.style.getPropertyValue("--alk-avatar-hue"));
    expect(hues).toEqual([
      String(hueOf({ userId: "u_cleo", email: "cleo@acme.test" })),
      String(hueOf({ userId: "u_bo", email: "bo@acme.test" })),
    ]);
    // And not the hue their user id alone would have given them.
    expect(hues[1]).not.toBe(String(hueOf("u_bo")));
  });

  it("empties when the socket drops, rather than keeping faces nobody can vouch for", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:3", "u_cleo", "Cleo Marsh"), peer("p:2", "u_bo", "Bo Chen")],
    });
    expect(faces()).toHaveLength(2);
    await act(async () => {
      sockets.last().serverClose(1006);
      await advance(0);
    });
    expect(faces()).toHaveLength(0);
  });
});

/** Hover a face the way a pointer does, and let the tip's open delay run out. */
async function hover(el: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.pointerEnter(el);
    await advance(1_000);
  });
}

/** Leave it again, then let the tip's exit fade run out. The fade is armed by
 *  the commit the leave causes, so the clock runs in a second act. */
async function unhover(el: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.pointerLeave(el);
  });
  await act(async () => {
    await advance(1_000);
  });
}

/** Move keyboard focus onto a face, as Tab would. */
async function focusOn(el: HTMLElement): Promise<void> {
  await act(async () => {
    el.focus();
    await advance(0);
  });
}

const tipText = (): string | null => screen.queryByRole("tooltip")?.textContent ?? null;

describe("what a face says when a reader asks who it is", () => {
  // A face is two letters or a picture. The name has to appear on the page when
  // it is hovered, and when it is reached from the keyboard, and it has to be
  // what the face is called to a reader who hears the page rather than sees it.
  it("shows the full name in a tooltip on hover, and takes it away on leave", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:3", "u_cleo", "Cleo Marsh"), peer("p:4", "u_fay", "Fay Wu")],
    });
    const [cleo, fay] = faces();
    expect(tipText()).toBeNull();

    await hover(cleo!);
    expect(tipText()).toBe("Cleo Marsh");
    await unhover(cleo!);
    expect(tipText()).toBeNull();

    await hover(fay!);
    expect(tipText()).toBe("Fay Wu");
  });

  it("is reachable from the keyboard, and focus shows the same name", async () => {
    await mount();
    await serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:3", "u_cleo", "Cleo Marsh")] });
    const [cleo] = faces();

    await focusOn(cleo!);
    expect(document.activeElement).toBe(cleo);
    expect(tipText()).toBe("Cleo Marsh");
    // The tip describes the face it annotates.
    expect(cleo!.getAttribute("aria-describedby")).toBe(screen.getByRole("tooltip").id);

    await act(async () => {
      cleo!.blur();
    });
    await act(async () => {
      await advance(1_000);
    });
    expect(tipText()).toBeNull();
  });

  it("is called by the full name, not its initials", async () => {
    await mount();
    await serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:3", "u_cleo", "Cleo Marsh")] });
    const [cleo] = faces();
    expect(cleo!.textContent).toBe("CM");
    expect(screen.getByRole("img", { name: "Cleo Marsh" })).toBe(cleo);
  });

  it("names a person the server could not name as Someone, on hover and to a screen reader", async () => {
    await mount();
    await serverSend({ t: "presence", channel: CHANNEL, event: "roster", peers: [peer("p:4", "u_fay")] });
    const [fay] = faces();
    expect(screen.getByRole("img", { name: "Someone" })).toBe(fay);
    await hover(fay!);
    expect(tipText()).toBe("Someone");
  });

  it("names the reader's own other window with their name first", async () => {
    await mount();
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: [peer("p:self", SELF, "Dana Okafor"), peer("p:self2", SELF, "Dana Okafor")],
    });
    const [own] = faces();
    await hover(own!);
    expect(tipText()).toBe("Dana Okafor");
  });

  it("lists every person the +N stands for, on hover, on focus and by name", async () => {
    await mount();
    const names = ["Ada Ling", "Bo Chen", "Cleo Marsh", "Dev Rao", "Eve Stone", "Fay Wu", "Gus Park"];
    await serverSend({
      t: "presence",
      channel: CHANNEL,
      event: "roster",
      peers: names.map((name, i) => peer(`p:${i}`, `u${i}`, name)),
    });
    const more = document.querySelector<HTMLElement>(".chat-presence__more")!;
    expect(more.textContent).toBe("+2");
    expect(screen.getByRole("img", { name: "Fay Wu, Gus Park" })).toBe(more);

    await hover(more);
    const lines = Array.from(screen.getByRole("tooltip").querySelectorAll(".chat-presence__tip-line")).map(
      (l) => l.textContent,
    );
    expect(lines).toEqual(["Fay Wu", "Gus Park"]);
    await unhover(more);

    await focusOn(more);
    expect(document.activeElement).toBe(more);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Fay Wu");
  });
});

describe("the people behind a roster", () => {
  const self = { peerId: "p:1" };

  it("counts the readers it cannot name rather than dropping them", () => {
    const viewers = viewersOf([peer("p:1", SELF, "Dana Okafor"), peer("p:2", "u2"), peer("p:3", "u3")], self);
    expect(presenceLabel(viewers)).toBe("2 people have this chat open");
    expect(viewers).toHaveLength(2);
  });

  it("names the ones it can and counts the ones it cannot", () => {
    const viewers = viewersOf([peer("p:2", "u2", "Bo Chen"), peer("p:3", "u3")], self);
    expect(presenceLabel(viewers)).toBe("Bo Chen and 1 other have this chat open");
  });

  it("takes the name off whichever of a person's tabs carries one", () => {
    const viewers = viewersOf([peer("p:8", "u1"), peer("p:9", "u1", "Dana Okafor")], { peerId: null });
    expect(viewers.map((v) => v.name)).toEqual(["Dana Okafor"]);
  });

  it("is empty when this tab is the only peer on the roster", () => {
    expect(viewersOf([peer("p:1", SELF, "Dana")], self)).toEqual([]);
  });

  it("keeps the reader's OTHER window and drops only this one", () => {
    // A face is a SOCKET's person, not an account: the reader's second window
    // is where a caret they are not driving comes from, so it has to show.
    const viewers = viewersOf([peer("p:1", SELF, "Dana"), peer("p:7", SELF, "Dana")], self);
    expect(viewers.map((v) => v.userId)).toEqual([SELF]);
    expect(presenceLabel(viewers)).toBe("Dana has this chat open");
  });

  it("counts the reader's other window once, however many they have", () => {
    const viewers = viewersOf(
      [peer("p:1", SELF, "Dana"), peer("p:7", SELF, "Dana"), peer("p:8", SELF, "Dana"), peer("p:2", "u_bo", "Bo Chen")],
      self,
    );
    expect(viewers.map((v) => v.userId)).toEqual([SELF, "u_bo"]);
  });

  it("uses the singular for one other reader in the room", () => {
    expect(presenceLabel(viewersOf([peer("p:2", "u_bo", "Bo Chen")], self))).toBe("Bo Chen has this chat open");
  });
});

describe("the colour a person wears", () => {
  const self = { peerId: null };

  it("is the same in two rosters that disagree about their peer ids and their order", () => {
    // Two tabs, two sessions, two join orders — one colleague, one colour. The
    // peer id is per socket and the roster order is per race, so keying on
    // either would repaint her on every refresh.
    const morning = viewersOf(
      [peer("p:a1", "u_cleo", "Cleo Marsh", undefined, "cleo@acme.test"), peer("p:a2", "u_bo", "Bo", undefined, "bo@acme.test")],
      self,
    );
    const evening = viewersOf(
      [peer("p:zz", "u_bo", "Bo", undefined, "bo@acme.test"), peer("p:yy", "u_cleo", "Cleo Marsh", undefined, "cleo@acme.test")],
      self,
    );
    const hueFor = (vs: ReturnType<typeof viewersOf>, id: string): number => hueOf(vs.find((v) => v.userId === id)!);
    expect(hueFor(morning, "u_cleo")).toBe(hueFor(evening, "u_cleo"));
    expect(hueFor(morning, "u_bo")).toBe(hueFor(evening, "u_bo"));
    expect(hueFor(morning, "u_cleo")).not.toBe(hueFor(morning, "u_bo"));
  });

  it("is the email's, so one person keeps their colour across deployments that number them differently", () => {
    expect(hueOf({ userId: "u_1", email: "cleo@acme.test" })).toBe(hueOf({ userId: "u_999", email: "cleo@acme.test" }));
    expect(hueOf({ userId: "u_1", email: "cleo@acme.test" })).not.toBe(hueOf({ userId: "u_1", email: "bo@acme.test" }));
  });

  it("reads an address the same however it was typed", () => {
    expect(hueOf({ userId: "u_1", email: "  Cleo@Acme.Test " })).toBe(hueOf({ userId: "u_1", email: "cleo@acme.test" }));
  });

  it("falls back to the user id where the server resolved no address", () => {
    expect(hueOf({ userId: "u_cleo", email: "" })).toBe(hueOf("u_cleo"));
    expect(hueOf({ userId: "u_cleo", email: "" })).not.toBe(hueOf({ userId: "u_bo", email: "" }));
  });

  it("keeps the address off whichever of a person's tabs carries one", () => {
    const viewers = viewersOf([peer("p:8", "u1", "Cleo"), peer("p:9", "u1", "Cleo", undefined, "cleo@acme.test")], self);
    expect(viewers.map((v) => v.email)).toEqual(["cleo@acme.test"]);
    expect(hueOf(viewers[0]!)).toBe(hueOf({ userId: "u1", email: "cleo@acme.test" }));
  });
});

describe("the presence frame parser", () => {
  it("keeps the name and the picture the server resolved", () => {
    const frame = parseServerFrame(
      JSON.stringify({
        t: "presence",
        channel: CHANNEL,
        event: "join",
        peers: [
          {
            peer_id: "p:2",
            user_id: "u_bo",
            last_seen_at: "2026-09-06T12:00:00Z",
            email: "bo@acme.test",
            display_name: "Bo Chen",
            avatar_url: "https://cdn.test/bo.png",
          },
        ],
      }),
    );
    expect(frame?.t).toBe("presence");
    if (frame?.t !== "presence") return;
    expect(frame.peers[0]).toEqual({
      peer_id: "p:2",
      user_id: "u_bo",
      last_seen_at: "2026-09-06T12:00:00Z",
      email: "bo@acme.test",
      display_name: "Bo Chen",
      avatar_url: "https://cdn.test/bo.png",
    });
  });

  it("leaves an older server's peer without a name rather than inventing one", () => {
    const frame = parseServerFrame(
      JSON.stringify({
        t: "presence",
        channel: CHANNEL,
        event: "join",
        peers: [{ peer_id: "p:2", user_id: "u_bo", last_seen_at: "2026-09-06T12:00:00Z", avatar_url: "" }],
      }),
    );
    if (frame?.t !== "presence") throw new Error("not a presence frame");
    expect("display_name" in (frame.peers[0] ?? {})).toBe(false);
    expect("avatar_url" in (frame.peers[0] ?? {})).toBe(false);
    expect("email" in (frame.peers[0] ?? {})).toBe(false);
  });
});
