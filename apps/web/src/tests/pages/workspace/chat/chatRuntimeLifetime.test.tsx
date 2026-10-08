// How long the chat runtime lives, and who decides.
//
// The source and the host are reached through a module-level getter, so their
// lifetime is not React's unless something makes it React's. It was not: the
// layout installed the pair while rendering and its effect CLEANUP cleared the
// module slot. React re-attaches a subtree's effects without re-rendering the
// parent — StrictMode's dev remount does it on every mount, and a Suspense
// re-reveal, an error-boundary reset and a keyed remount do it in production —
// and in that order the cleanup runs first and a CHILD's effect reads the slot
// before the parent's effect can fill it again. `useComposerPrefs` subscribes
// to the host in exactly such an effect, so every chat address in the dev build
// threw `no chat runtime installed` into the app's error boundary.
//
// These pin the lifetime end to end: valid for as long as a consumer is
// mounted, retired when the reader leaves the chat area, and never shared
// between one visit and the next.

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import {
  Component,
  Suspense,
  useEffect,
  useLayoutEffect,
  useState,
  type ErrorInfo,
  type ReactElement,
  type ReactNode,
} from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryClient } from "@/api/queryClient";
import { ChatRuntimeProvider } from "@/pages/workspace/chat/ChatRuntimeLayout";
import { chatData, chatHost, resetChatRuntime } from "@/pages/workspace/chat/data";

import { CHAT_ID, CHAT_PATHS, PART_ID, scriptChatFetch } from "./chatRouteFixtures";
import { ChatRoutes, ChatUnderTest } from "./chatRouteTree";

// The browser host, counting the subscriptions that are live right now. The
// real factory is kept — a host that only counted would prove nothing about the
// chat it is wired into.
const liveSubscriptions = vi.hoisted(() => ({ now: 0, ever: 0 }));
// And the source's own transport. A host nobody subscribes to would keep the
// ceiling below at zero while every abandoned CloudDataSource went on holding a
// chat's event stream open.
const liveTransports = vi.hoisted(() => ({ now: 0, ever: 0 }));
vi.mock("@/pages/workspace/chat/data/CloudDataSource", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/pages/workspace/chat/data/CloudDataSource")>();
  type Subscribe = InstanceType<typeof actual.CloudDataSource>["subscribeChat"];
  return {
    ...actual,
    CloudDataSource: class extends actual.CloudDataSource {
      subscribeChat(chatId: string, onEvent: Parameters<Subscribe>[1]): ReturnType<Subscribe> {
        liveTransports.now += 1;
        liveTransports.ever += 1;
        const off = super.subscribeChat(chatId, onEvent);
        let stopped = false;
        return () => {
          if (!stopped) liveTransports.now -= 1;
          stopped = true;
          off();
        };
      }
    },
  };
});
vi.mock("@/pages/workspace/chat/data/browserChatHost", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/pages/workspace/chat/data/browserChatHost")>();
  return {
    ...actual,
    createBrowserChatHost: (...args: Parameters<typeof actual.createBrowserChatHost>) => {
      const host = actual.createBrowserChatHost(...args);
      return {
        ...host,
        subscribe(handler: Parameters<typeof host.subscribe>[0]) {
          liveSubscriptions.now += 1;
          liveSubscriptions.ever += 1;
          const off = host.subscribe(handler);
          let stopped = false;
          return () => {
            if (!stopped) liveSubscriptions.now -= 1;
            stopped = true;
            off();
          };
        },
      };
    },
  };
});

/** What the app's own root boundary would have caught. */
class Boundary extends Component<{ children: ReactNode }, { caught: Error | null }> {
  state = { caught: null as Error | null };
  static getDerivedStateFromError(caught: Error): { caught: Error } {
    return { caught };
  }
  // The boundary swallows the error; the test reads it off the DOM.
  componentDidCatch(_error: Error, _info: ErrorInfo): void {}
  render(): ReactNode {
    if (this.state.caught) return <div data-testid="caught">{this.state.caught.message}</div>;
    return this.props.children;
  }
}

function caughtMessage(): string | null {
  return screen.queryByTestId("caught")?.textContent ?? null;
}

let consoleErrors: string[] = [];

beforeEach(() => {
  queryClient.clear();
  scriptChatFetch();
  liveSubscriptions.now = 0;
  liveSubscriptions.ever = 0;
  liveTransports.now = 0;
  liveTransports.ever = 0;
  consoleErrors = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    consoleErrors.push(args.map(String).join(" "));
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetChatRuntime();
});

describe("a chat address opened cold, under the lifetime the app runs", () => {
  it.each(CHAT_PATHS)("renders %s rather than the error boundary", async (path) => {
    render(
      <ChatUnderTest path={path}>
        <Boundary>
          <ChatRoutes />
        </Boundary>
      </ChatUnderTest>,
    );

    // Give the mount — and StrictMode's remount of it — every effect it has.
    await act(async () => {
      await Promise.resolve();
    });

    expect(caughtMessage()).toBeNull();
    expect(consoleErrors.filter((line) => line.includes("no chat runtime installed"))).toEqual([]);
    // The getter the surfaces read through answers, which is the thing that
    // threw.
    expect(() => chatData()).not.toThrow();
  });
});

describe("a chat subtree React re-attaches without re-rendering its parent", () => {
  it("keeps its children working across a keyed remount", async () => {
    function Remountable(): ReactElement {
      const [generation, setGeneration] = useState(0);
      return (
        <Boundary>
          <button type="button" onClick={() => setGeneration((n) => n + 1)}>
            remount
          </button>
          <div key={generation}>
            <ChatRoutes />
          </div>
        </Boundary>
      );
    }
    render(
      <ChatUnderTest path={`/chat/${CHAT_ID}/compaction/${PART_ID}`}>
        <Remountable />
      </ChatUnderTest>,
    );
    expect(await screen.findByText("Compaction")).toBeTruthy();

    for (let round = 0; round < 3; round += 1) {
      await act(async () => {
        screen.getByRole("button", { name: "remount" }).click();
      });
      expect(caughtMessage()).toBeNull();
      expect(await screen.findByText("Compaction")).toBeTruthy();
    }
  });

  it("keeps its children working when an error boundary below it is reset", async () => {
    function Resettable(): ReactElement {
      const [attempt, setAttempt] = useState(0);
      return (
        <Boundary>
          <button type="button" onClick={() => setAttempt((n) => n + 1)}>
            try again
          </button>
          <Boundary key={attempt}>
            <ChatRoutes />
          </Boundary>
        </Boundary>
      );
    }
    render(
      <ChatUnderTest path={`/chat/${CHAT_ID}/results`}>
        <Resettable />
      </ChatUnderTest>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(caughtMessage()).toBeNull();

    await act(async () => {
      screen.getByRole("button", { name: "try again" }).click();
    });
    expect(caughtMessage()).toBeNull();
    expect(() => chatHost()).not.toThrow();
  });
});

describe("leaving the chat area", () => {
  it("retires the runtime, and coming back builds a fresh one", async () => {
    function Visit({ inChat }: { inChat: boolean }): ReactElement {
      return inChat ? <ChatRoutes /> : <div>away from the chat</div>;
    }
    function App(): ReactElement {
      const [inChat, setInChat] = useState(true);
      return (
        <Boundary>
          <button type="button" onClick={() => setInChat((v) => !v)}>
            toggle
          </button>
          <Visit inChat={inChat} />
        </Boundary>
      );
    }
    render(
      <ChatUnderTest path={`/chat/${CHAT_ID}`}>
        <App />
      </ChatUnderTest>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    const first = chatData();

    await act(async () => {
      screen.getByRole("button", { name: "toggle" }).click();
    });
    // Nothing is mounted that could read it, and nothing is left holding the
    // chat's transport open.
    await waitFor(() => expect(() => chatData()).toThrow(/no chat runtime installed/));
    expect(liveSubscriptions.now).toBe(0);

    await act(async () => {
      screen.getByRole("button", { name: "toggle" }).click();
    });
    await act(async () => {
      await Promise.resolve();
    });
    // A returning reader gets a source that will read the chat for itself, not
    // the fold the last visit left behind.
    expect(caughtMessage()).toBeNull();
    expect(chatData()).not.toBe(first);
  });

  it("leaks no host subscription across twenty visits", async () => {
    function App(): ReactElement {
      const [inChat, setInChat] = useState(true);
      return (
        <Boundary>
          <button type="button" onClick={() => setInChat((v) => !v)}>
            toggle
          </button>
          {inChat ? <ChatRoutes /> : <div>away from the chat</div>}
        </Boundary>
      );
    }
    render(
      <ChatUnderTest path={`/chat/${CHAT_ID}`}>
        <App />
      </ChatUnderTest>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    // What ONE visit legitimately holds open. A chat subscribes more than once
    // — the transcript and the store each take the source's event stream — so
    // the invariant is not a fixed ceiling, it is that twenty visits hold no
    // more than one does.
    const perVisitSubscriptions = liveSubscriptions.now;
    const perVisitTransports = liveTransports.now;

    for (let visit = 0; visit < 20; visit += 1) {
      await act(async () => {
        screen.getByRole("button", { name: "toggle" }).click();
      });
      await act(async () => {
        screen.getByRole("button", { name: "toggle" }).click();
      });
      await act(async () => {
        await Promise.resolve();
      });
      expect(caughtMessage()).toBeNull();
    }
    // Subscribing at all is the point of the count — a wrapper nobody called
    // would satisfy the ceiling below by accident.
    expect(liveSubscriptions.ever).toBeGreaterThan(0);
    expect(liveTransports.ever).toBeGreaterThan(0);
    // Whatever one visit holds open, twenty visits hold no more of — on the
    // host's side AND on the source's own transport, which is what an abandoned
    // CloudDataSource would keep open.
    expect(liveSubscriptions.now).toBeLessThanOrEqual(perVisitSubscriptions);
    expect(liveTransports.now).toBeLessThanOrEqual(perVisitTransports);

    await act(async () => {
      screen.getByRole("button", { name: "toggle" }).click();
    });
    await waitFor(() => expect(liveSubscriptions.now).toBe(0));
    await waitFor(() => expect(liveTransports.now).toBe(0));
  });
});

describe("a render React throws away", () => {
  it("installs nothing, so the mounted instance can still retire its own pair", async () => {
    // React renders far more than it commits. An install from a render that is
    // then discarded would land under an owner that never mounts, and the
    // mounted instance's release (guarded on owner) could then do nothing,
    // leaving the discarded pair installed after the reader has gone.
    //
    // Scope: this suspends a child so the provider's render is retried, but
    // React reuses the fiber here, so an install-from-render would still pass.
    // That case is closed by nothing installing from a render at all, which no
    // jsdom test can demonstrate directly. What this
    // pins is the property that matters either way: a mount that took two
    // renders to settle ends with ONE installation, and the reader leaving
    // clears it.
    let resolveChild = (): void => {};
    const ready = new Promise<void>((resolve) => {
      resolveChild = resolve;
    });
    let settled = false;
    function Suspending(): ReactElement {
      if (!settled) throw ready.then(() => (settled = true));
      return <div>settled child</div>;
    }
    const view = render(
      <ChatUnderTest path={`/chat/${CHAT_ID}`}>
        <Boundary>
          <ChatRuntimeProvider>
            <Suspense fallback={<div>waiting</div>}>
              <Suspending />
            </Suspense>
          </ChatRuntimeProvider>
        </Boundary>
      </ChatUnderTest>,
    );
    await act(async () => {
      resolveChild();
      await ready;
    });
    expect(await screen.findByText("settled child")).toBeTruthy();
    const mounted = chatData();

    view.unmount();
    await waitFor(() => expect(() => chatData()).toThrow(/no chat runtime installed/));

    // Nothing of the discarded render outlived the reader: the slot is empty,
    // not holding a pair whose owner will never come back to release it.
    expect(mounted).toBeTruthy();
  });
});

describe("the commit that mounts the chat", () => {
  it("puts the chat on screen in that commit, not a frame later", () => {
    // The children cannot render before the install, so SOMETHING has to let
    // them through afterwards — and where that happens is the difference
    // between a chat area that is simply there and one that is blank for a
    // frame. A layout effect is finished before the browser paints; a passive
    // effect is not, and this renders nothing until a later task.
    //
    // flushSync runs the commit and its layout effects and stops. Whatever is
    // on screen at that point is what the reader would see on the first frame.
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    // React only allows a render outside `act` when this says so, and flushSync
    // is exactly such a render.
    const reactEnv = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
    const wasActEnvironment = reactEnv.IS_REACT_ACT_ENVIRONMENT;
    reactEnv.IS_REACT_ACT_ENVIRONMENT = false;
    try {
      flushSync(() => {
        root.render(
          <QueryClientProvider client={queryClient}>
            <MemoryRouter initialEntries={[`/chat/${CHAT_ID}`]}>
              <ChatRuntimeProvider>
                <div>first frame</div>
              </ChatRuntimeProvider>
            </MemoryRouter>
          </QueryClientProvider>,
        );
      });

      expect(container.textContent).toContain("first frame");
    } finally {
      flushSync(() => root.unmount());
      reactEnv.IS_REACT_ACT_ENVIRONMENT = wasActEnvironment;
      container.remove();
    }
  });
});

describe("a child that reads the runtime while the subtree is coming down", () => {
  it("still finds it in its own cleanup, after the provider has let go", async () => {
    // React destroys ALL layout effects before ANY passive one, so the
    // provider's release — a layout cleanup — runs before a child's passive
    // cleanup. A child that reaches for the source to tear its own work down
    // would read an empty slot. Holding the release back for a task is what
    // makes the order stop mattering; nothing in the chat composition reads the
    // runtime from a cleanup today, and this is why it may.
    const failures: string[] = [];
    function LateReader(): ReactElement {
      useEffect(() => {
        return () => {
          try {
            chatHost();
          } catch (err) {
            failures.push(err instanceof Error ? err.message : String(err));
          }
        };
      }, []);
      return <div>late reader</div>;
    }
    const view = render(
      <ChatUnderTest path={`/chat/${CHAT_ID}`}>
        <Boundary>
          <ChatRuntimeProvider>
            <LateReader />
          </ChatRuntimeProvider>
        </Boundary>
      </ChatUnderTest>,
    );
    expect(await screen.findByText("late reader")).toBeTruthy();
    expect(failures).toEqual([]);

    view.unmount();

    expect(failures).toEqual([]);
  });

  it("finds it in a layout effect through a re-attach of the subtree", async () => {
    const failures: string[] = [];
    function EarlyReader(): ReactElement {
      useLayoutEffect(() => {
        try {
          chatHost();
        } catch (err) {
          failures.push(err instanceof Error ? err.message : String(err));
        }
      });
      return <div>early reader</div>;
    }
    render(
      <ChatUnderTest path={`/chat/${CHAT_ID}`}>
        <Boundary>
          <ChatRuntimeProvider>
            <EarlyReader />
          </ChatRuntimeProvider>
        </Boundary>
      </ChatUnderTest>,
    );
    await act(async () => {
      await Promise.resolve();
    });

    expect(await screen.findByText("early reader")).toBeTruthy();
    expect(failures).toEqual([]);
    expect(caughtMessage()).toBeNull();
  });
});

describe("a chat surface mounted outside the chat routes", () => {
  it("brings its own runtime", async () => {
    const { ChatPage } = await import("@/pages/workspace/chat/ChatPage");
    render(
      <ChatUnderTest path="/chat">
        <Boundary>
          <ChatPage />
        </Boundary>
      </ChatUnderTest>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(caughtMessage()).toBeNull();
    expect(chatHost().kind).toBe("browser");
  });
});
