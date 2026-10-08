// A browser that refuses site data must cost the reader their preferences and
// never the page.
//
// `localStorage` throws a `SecurityError` on the mere mention of it in a private
// window, in an embedded webview, and in any profile with site data blocked —
// and a throw inside a `useState` initializer happens during render, so it takes
// the whole subtree to the nearest error boundary. Every surface in the portal
// that remembers something is driven here under a store that refuses, and each
// has to come back with its default instead.

import { render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { readSize, usePersistedSize, writeSize } from "@/app/usePersistedSize";
import { useScopeToggle } from "@/app/useScopeToggle";
import { forgetDeletedChat, readLastChat, rememberLastChat } from "@/pages/workspace/chat/lastOpenChat";
import { readQueued, writeQueued } from "@/pages/workspace/chat/queuedMessages";
import { readStoredView, writeStoredView } from "@/pages/workspace/files/FilesBrowser";
import { clearStorageMirror } from "@alkera/ui/storage";

/** The person and org a remembered value is filed under. */
const ME = { userId: "user-1", orgId: "org-1" };

type StoreName = "localStorage" | "sessionStorage";

const real: Record<StoreName, PropertyDescriptor | undefined> = {
  localStorage: Object.getOwnPropertyDescriptor(globalThis, "localStorage"),
  sessionStorage: Object.getOwnPropertyDescriptor(globalThis, "sessionStorage"),
};

/** Naming the store throws — a private window, a blocked-cookie profile. */
function blockStore(): void {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get() {
        throw new DOMException("The operation is insecure.", "SecurityError");
      },
    });
  }
}

/** The store answers, but reading and writing it throw — a filled quota, or a
 *  store that turns hostile after the page has loaded. */
function breakMethods(): void {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    const underlying = real[name]?.get?.call(globalThis) ?? real[name]?.value;
    const broken = Object.create(underlying) as Record<string, unknown>;
    for (const method of ["getItem", "setItem", "removeItem", "key"]) {
      broken[method] = () => {
        throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
      };
    }
    Object.defineProperty(globalThis, name, { configurable: true, value: broken });
  }
}

function restore(): void {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    const descriptor = real[name];
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
  }
}

afterEach(() => {
  restore();
  clearStorageMirror();
  globalThis.localStorage.clear();
});

function ScopeHarness(): React.ReactElement {
  const { scope, control } = useScopeToggle("plugins.scope");
  return (
    <div>
      <span data-testid="scope">{scope}</span>
      {control}
    </div>
  );
}

function SizeHarness(): React.ReactElement {
  const [size] = usePersistedSize("chat.rail", { min: 176, max: 480, size: 256 });
  return <span data-testid="width">{size.width}</span>;
}

/** Each entry is one surface that remembers something, and what it has to do
 *  when the browser will not remember. */
const surfaces: readonly {
  id: string;
  render: () => React.ReactElement;
  expect: () => void;
}[] = [
  {
    id: "useScopeToggle",
    render: () => <ScopeHarness />,
    expect: () => {
      expect(screen.getByTestId("scope").textContent).toBe("org");
      expect(screen.getByRole("tab", { name: "Organization" })).toBeInTheDocument();
    },
  },
  {
    id: "usePersistedSize",
    render: () => <SizeHarness />,
    expect: () => {
      expect(screen.getByTestId("width").textContent).toBe("256");
    },
  },
];

describe.each([
  ["the store itself throws", blockStore],
  ["every store method throws", breakMethods],
])("a browser where %s", (_label, hostile) => {
  it.each(surfaces.map((surface) => [surface.id, surface] as const))(
    "%s still renders its default",
    (_id, surface) => {
      hostile();
      expect(() => render(surface.render())).not.toThrow();
      surface.expect();
    },
  );

  it("the files view falls back to the default and the switch still holds", () => {
    hostile();
    expect(readStoredView("drive-1")).toBeNull();
    expect(() => writeStoredView("drive-1", "grid")).not.toThrow();
    // A refused write still holds for this page session, so the reader's click
    // is not silently undone the next time the view is read.
    expect(readStoredView("drive-1")).toBe("grid");
  });

  it("a chat opens with an empty queue and still queues for this session", () => {
    hostile();
    expect(readQueued(ME, "chat-1")).toEqual([]);
    expect(() => writeQueued(ME, "chat-1", [{ id: "m1", text: "hi" }])).not.toThrow();
    expect(readQueued(ME, "chat-1")).toEqual([{ id: "m1", text: "hi", restored: true }]);
  });

  it("the last-open chat starts empty, holds for this session, and can be forgotten", () => {
    hostile();
    expect(readLastChat(ME)).toBeNull();
    expect(() => rememberLastChat(ME, "chat-1")).not.toThrow();
    expect(readLastChat(ME)).toBe("chat-1");
    // Deleting the chat has to reach the value wherever it is being held, or a
    // reader lands back in front of a transcript they just deleted.
    expect(() => forgetDeletedChat("chat-1")).not.toThrow();
    expect(readLastChat(ME)).toBeNull();
  });

  it("a remembered pane size reads as the default and writing one never throws", () => {
    hostile();
    const bounds = { min: 176, max: 480, size: 256 };
    expect(readSize("chat.rail", bounds)).toEqual({ collapsed: false, width: 256 });
    expect(() => writeSize("chat.rail", { collapsed: false, width: 300 }, bounds)).not.toThrow();
  });
});

describe("a browser that remembers", () => {
  it("useScopeToggle restores the stored scope and records a new pick", async () => {
    globalThis.localStorage.setItem("plugins.scope", "personal");
    render(<ScopeHarness />);
    expect(screen.getByTestId("scope").textContent).toBe("personal");

    await act(async () => {
      screen.getByRole("tab", { name: "Organization" }).click();
    });
    expect(screen.getByTestId("scope").textContent).toBe("org");
    expect(globalThis.localStorage.getItem("plugins.scope")).toBe("org");
  });
});
