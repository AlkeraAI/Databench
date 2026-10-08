// The reader's own preferences in the browser: what the page offers, and what a save sends.
//
// The hooks are the costly external boundary and are mocked; everything the page
// DOES with them runs for real — the narrowing of the stance picker, the effort
// list that follows the chosen model, the PATCH body, and the re-seed the
// mutation declares.
//
// Each case is written as the failure it prevents:
//   - a picker offering a stance the server refuses would be a dead control;
//   - a save that echoed the whole document would re-write a newer client's key;
//   - an effort list that ignored the chosen model would pin a variant the model
//     does not offer;
//   - a save that named a neighbouring key family instead of the one the
//     composer reads its seed from would leave every open composer starting
//     (and billing) the next chat on the old default model.

import { MemoryRouter } from "react-router-dom";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Query<T> = { data?: T; isPending?: boolean; isError?: boolean; error?: unknown; refetch?: () => void };

const CATALOG = [
  {
    id: "claude-opus-4.5",
    display_name: "Claude Opus 4.5",
    wire: "anthropic" as const,
    efforts: ["low", "medium", "high"],
    default_effort: "medium",
  },
  {
    id: "gpt-5.2",
    display_name: "GPT-5.2",
    wire: "openai" as const,
    efforts: [],
    default_effort: null,
  },
];

const STORED: Record<string, unknown> = {
  schema_version: "2.0.0",
  default_permission_mode: "read_only",
  default_chat_model: null,
  default_chat_effort: null,
  telemetry_enabled: true,
};

const h = {
  prefs: {} as Query<Record<string, unknown>>,
  models: {} as Query<typeof CATALOG>,
  save: { mutate: vi.fn(), isPending: false },
};

vi.mock("@/api/preferences", () => ({
  useMyPreferences: () => h.prefs,
  useSetMyPreferences: () => h.save,
}));
vi.mock("@/api/chatModels", () => ({ useMyChatModels: () => h.models }));

import { PreferencesBody } from "@/pages/workspace/preferences/PreferencesPage";
import { silentNotify } from "@/tests/fixtures/notify";

function renderPage() {
  return render(
    <MemoryRouter>
      <PreferencesBody notify={silentNotify} />
    </MemoryRouter>,
  );
}

// The shared Select is a custom listbox, not a native <select>: its closed
// trigger is a button carrying the control's aria-label, and it commits on
// MOUSEDOWN so the trigger keeps focus (a plain click never lands).

/** A control's closed trigger, by its accessible name. */
const trigger = (name: RegExp): HTMLElement => screen.getByRole("button", { name });

/** Open a control and return the option VALUES it offers. */
function optionsOf(name: RegExp): string[] {
  fireEvent.click(trigger(name));
  const listed = screen.getAllByRole("option").map((option) => option.textContent ?? "");
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
  return listed;
}

/** Open a control and pick the option reading `label`. */
function pick(name: RegExp, label: RegExp): void {
  fireEvent.click(trigger(name));
  fireEvent.mouseDown(screen.getByRole("option", { name: label }));
}

/** What a save sent, as the page's PATCH body. */
const savedBody = (): Record<string, unknown> =>
  h.save.mutate.mock.calls.at(-1)?.[0] as Record<string, unknown>;

function save(): void {
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
}

beforeEach(() => {
  h.prefs = { data: { ...STORED } };
  h.models = { data: CATALOG };
  h.save = { mutate: vi.fn(), isPending: false };
});

afterEach(cleanup);

describe("the preferences page", () => {
  // The save bar is hidden from the accessibility tree (aria-hidden) whenever the
  // form is pristine, so its absence from the a11y tree IS "no unsaved changes".
  const saveBar = (): HTMLElement | null =>
    screen.queryByRole("region", { name: /unsaved changes/i });

  it("announces no unsaved changes on a fresh load", async () => {
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    expect(saveBar()).toBeNull();
    expect(screen.queryByRole("button", { name: /save changes/i })).toBeNull();
  });

  it("announces no unsaved changes on a document that stores no preference yet", async () => {
    // A reader who has never chosen: the keys are ABSENT, not null.
    h.prefs = { data: { schema_version: "2.0.0" } };
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    expect(saveBar()).toBeNull();
  });

  it("stops announcing changes once a pick is put back where it started", async () => {
    h.prefs = { data: { schema_version: "2.0.0" } };
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    pick(/default model/i, /Claude Opus 4\.5/);
    await waitFor(() => expect(saveBar()).not.toBeNull());

    // Back to "no preference" — which writes `null` where the document held
    // nothing at all. The form is where it started, so nothing is unsaved.
    pick(/default model/i, /Let the workspace choose/);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /default reasoning effort/i })).toBeNull(),
    );
    expect(saveBar()).toBeNull();
  });

  it("keeps announcing a real change, and a key this page cannot edit never raises it", async () => {
    h.prefs = { data: { ...STORED, a_field_from_2027: { nested: 1 } } };
    renderPage();
    await screen.findByRole("button", { name: /default permission mode/i });

    expect(saveBar()).toBeNull();
    pick(/default permission mode/i, /^Plan/);
    await waitFor(() => expect(saveBar()).not.toBeNull());
  });

  // The saved default seeds a chat the browser opens, so this picker has to
  // offer exactly what the chat's own does — a stance a reader can switch INTO
  // but never start in would be the preference quietly overriding the chip.
  it("offers every stance a cloud chat runs in", async () => {
    renderPage();
    await screen.findByRole("button", { name: /default permission mode/i });

    const offered = optionsOf(/default permission mode/i).join("|");

    // Every stance a cloud chat can be put in is a stance it can be SEEDED in:
    // a default the chat picker offers but this page withholds would leave the
    // person setting it once per chat forever.
    for (const name of [/Read-only/, /Default/, /Plan/, /\bAuto\b/, /Bypass/]) {
      expect(offered).toMatch(name);
    }
  });

  it("offers only the settings the web actually acts on", async () => {
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    // The editor's own controls — the ones the daemon honours and the server
    // does not — must not appear here: a toggle nothing reads tells the person
    // they changed something when nothing changed.
    expect(screen.queryByRole("button", { name: /tool card load policy/i })).toBeNull();
    expect(screen.queryByLabelText(/reduce motion/i)).toBeNull();
    expect(screen.queryByLabelText(/sql statement timeout/i)).toBeNull();
  });

  it("lists the efforts the CHOSEN model offers, and none for a model with no variants", async () => {
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    pick(/default model/i, /Claude Opus 4\.5/);
    await screen.findByRole("button", { name: /default reasoning effort/i });
    expect(optionsOf(/default reasoning effort/i).join("|")).toMatch(/Low\|Medium\|High/);

    pick(/default model/i, /GPT-5\.2/);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /default reasoning effort/i })).toBeNull(),
    );
  });

  it("drops the effort when the model changes, so a stale variant is never pinned", async () => {
    h.prefs = {
      data: { ...STORED, default_chat_model: "claude-opus-4.5", default_chat_effort: "high" },
    };
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    pick(/default model/i, /GPT-5\.2/);
    save();

    expect(savedBody().default_chat_model).toBe("gpt-5.2");
    expect(savedBody().default_chat_effort).toBeNull();
  });

  it("sends only the fields it edits, so a newer client's key is not re-written", async () => {
    h.prefs = { data: { ...STORED, a_field_from_2027: { nested: 1 } } };
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    pick(/default model/i, /GPT-5\.2/);
    save();

    expect(savedBody()).not.toHaveProperty("a_field_from_2027");
    expect(savedBody()).not.toHaveProperty("schema_version");
    expect(savedBody().default_chat_model).toBe("gpt-5.2");
  });

  it("persists the stance's raw value, never its label", async () => {
    renderPage();
    await screen.findByRole("button", { name: /default permission mode/i });

    pick(/default permission mode/i, /^Plan/);
    save();

    expect(savedBody().default_permission_mode).toBe("plan");
  });

  it("says the catalog is unavailable rather than pretending the reader has no models", async () => {
    h.models = { data: [], isError: true, error: new Error("gateway down") };
    renderPage();

    expect(await screen.findByText(/catalog is unavailable/i)).toBeInTheDocument();
  });

  it("still shows a saved model the catalog no longer offers", async () => {
    h.prefs = { data: { ...STORED, default_chat_model: "claude-opus-4.0" } };
    renderPage();

    // The trigger reads the value back, so the person sees what they are pinned
    // to rather than a blank that reads as "nothing chosen".
    const model = await screen.findByRole("button", { name: /default model/i });
    expect(model.textContent).toMatch(/claude-opus-4\.0/);
  });

  it("offers no save until something changes", async () => {
    renderPage();
    await screen.findByRole("button", { name: /default model/i });

    // The bar is present but out of the accessible tree and out of the tab
    // order until there is something to save.
    const bar = document.querySelector('[aria-label="Unsaved changes"]');
    expect(bar).not.toBeNull();
    expect(bar?.getAttribute("aria-hidden")).toBe("true");
    expect(bar?.getAttribute("data-show")).toBeNull();
    expect(h.save.mutate).not.toHaveBeenCalled();

    pick(/default model/i, /GPT-5\.2/);

    await waitFor(() =>
      expect(
        document.querySelector('[aria-label="Unsaved changes"]')?.getAttribute("data-show"),
      ).not.toBeNull(),
    );
  });
});

describe("every preference is a labelled field, not a squeezed row", () => {
  // A control in a nowrap row BESIDE its label shrinks the name and its help to
  // one word per line and draws over them. Each preference uses the shared field
  // scaffold (FieldShell, via the control's own `label`/`description`): the name
  // is a real <label for>, the help is a node the control points at, and the
  // control owns the width below them.
  const FIELDS: [label: string, description: RegExp][] = [
    ["Default model", /a chat you start on the web opens on/i],
    ["Default reasoning effort", /thinks before it answers/i],
    ["Default permission mode", /./],
  ];

  beforeEach(() => {
    // A model WITH efforts, so all three controls are on the page at once.
    h.prefs = { data: { ...STORED, default_chat_model: "claude-opus-4.5" } };
  });

  it.each(FIELDS)("names %s with a real label element bound to the control", async (label) => {
    renderPage();
    const control = await screen.findByLabelText(label);

    // The same node the control role resolves to — the label names the CONTROL,
    // not a span sitting next to it.
    expect(control).toBe(screen.getByRole("button", { name: new RegExp(`^${label}$`, "i") }));
    const labelEl = document.querySelector(`label[for="${control.id}"]`);
    expect(labelEl?.textContent).toBe(label);
  });

  it.each(FIELDS)("associates %s's description with the control", async (label, description) => {
    renderPage();
    const control = await screen.findByLabelText(label);

    const ids = (control.getAttribute("aria-describedby") ?? "").split(/\s+/).filter(Boolean);
    expect(ids.length).toBeGreaterThan(0);
    const help = ids.map((id) => document.getElementById(id)?.textContent ?? "").join(" ");
    // Every referenced id resolves — an aria-describedby pointing at nothing is
    // no description at all — and reads as the field's own help.
    expect(ids.every((id) => document.getElementById(id) !== null)).toBe(true);
    expect(help).toMatch(description);
  });

  it.each(FIELDS)("stacks %s's control under its label instead of beside it", async (label) => {
    renderPage();
    const control = await screen.findByLabelText(label);

    // The squeezed layout put label, help and control in one nowrap flex row
    // (.alk-inline), which is what collapsed the label column. A field stacks
    // them in the shared .alk-field column instead.
    expect(control.closest(".alk-inline")).toBeNull();
    const field = control.closest(".alk-field");
    expect(field).not.toBeNull();
    expect(field?.querySelector(`label[for="${control.id}"]`)).not.toBeNull();
  });
});

describe("a saved preference reaches every open composer", () => {
  // The mechanism that makes "saved, but my next chat used the old default"
  // impossible: the mutation declares the key the COMPOSER reads its seed
  // under, and the shared query client's policy refetches it. Driven through
  // the REAL hook, the REAL key modules and a REAL client, so a `meta` that
  // named a neighbouring family — or a bare QueryClient with no policy — fails
  // here instead of going stale in a composer for five minutes.
  it("a save refetches the composer's resolved default, and leaves unrelated reads alone", async () => {
    vi.resetModules();
    vi.doUnmock("@/api/preferences");
    const [{ useSetMyPreferences }, { createQueryClient }, { keys: realKeys }, { chatKeys }] =
      await Promise.all([
        vi.importActual<typeof import("@/api/preferences")>("@/api/preferences"),
        vi.importActual<typeof import("@/api/queryClient")>("@/api/queryClient"),
        vi.importActual<typeof import("@/api/keys")>("@/api/keys"),
        vi.importActual<typeof import("@/pages/workspace/chat/chatKeys")>(
          "@/pages/workspace/chat/chatKeys",
        ),
      ]);
    const { renderHook, waitFor: waitForHook } = await import("@testing-library/react");
    const { QueryClientProvider, useQuery } = await import("@tanstack/react-query");

    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ preferences: { default_chat_model: "gpt-5.2" } }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    const qc = createQueryClient({ retry: false });
    const reads = { defaults: 0, chats: 0, credits: 0 };

    const { result } = renderHook(
      () => {
        // Where the composer actually reads its default model + effort from
        // (controller/useComposerPrefs.ts, useChatListSurface.ts) — a family of
        // its own that no `["chats"]` prefix reaches.
        const defaults = useQuery({
          queryKey: chatKeys.chatDefaults(),
          // The same five-minute staleness the composers carry: with nothing
          // invalidating it, a remount re-reads the OLD model. That is the bug.
          staleTime: 5 * 60_000,
          queryFn: async () => {
            reads.defaults += 1;
            return reads.defaults;
          },
        });
        const chats = useQuery({
          queryKey: realKeys.chats.all,
          queryFn: async () => {
            reads.chats += 1;
            return reads.chats;
          },
        });
        // A read the save cannot touch: it pins that the declared list NARROWS
        // the policy, so the case above passes on the named key rather than on
        // an accidental invalidate-everything.
        const credits = useQuery({
          queryKey: realKeys.me.credits,
          staleTime: 5 * 60_000,
          queryFn: async () => {
            reads.credits += 1;
            return reads.credits;
          },
        });
        return { defaults, chats, credits, save: useSetMyPreferences() };
      },
      {
        wrapper: ({ children }) => (
          <QueryClientProvider client={qc}>{children}</QueryClientProvider>
        ),
      },
    );

    await waitForHook(() => {
      expect(reads.defaults).toBe(1);
      expect(reads.chats).toBe(1);
      expect(reads.credits).toBe(1);
    });
    result.current.save.mutate({ default_chat_model: "gpt-5.2" });

    await waitForHook(() => expect(reads.defaults).toBe(2));
    expect(reads.chats).toBe(2);
    expect(reads.credits).toBe(1);
    vi.unstubAllGlobals();
  });
});
