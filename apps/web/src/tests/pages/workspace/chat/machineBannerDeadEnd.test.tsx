// The banner above a chat with no workspace must not dead-end the reader.
//
// On a brand-new organization the person reading this IS the organization
// admin, and no page in the product — theirs or ours — starts a workspace
// machine: a box registers itself once the platform provisions it. So the
// banner may not send the reader to an administrator, and it may not ask for a
// reload of a state the page already polls and invalidates for itself.
//
// "No machine" is placement's own answer — nothing of the org's, no dedicated
// box and no shared box would take a chat now — so it is stated as that fact
// and clears itself when a box comes up; it offers no escalation. Neither does
// a refused chat: the box that refused it reports why, the refusal clears when
// the box can run the chat, and a tenant sent to support over it was being
// asked to chase a state that fixes itself. No machine state escalates.

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { MachineBanner, machineIsReady } from "@/pages/workspace/chat/MachineBanner";

import { chatFact } from "../../../fixtures/statusFacts";

/** The brand a deployment serves. A white-labeled install answers its own
 *  support address, and the banner must offer THAT one, not the product's own. */
let supportEmail: string;

function stubConfig() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/v1/config")) {
        return new Response(
          JSON.stringify({
            product_name: "Databench",
            support_email: supportEmail,
            telemetry_enabled: false,
            self_hosted: false,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

function renderBanner(node: ReactElement) {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>{node}</QueryClientProvider>,
  );
  return screen.getByRole("status");
}

beforeEach(() => {
  supportEmail = "support@example.com";
  stubConfig();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("a chat with no workspace machine", () => {
  it("does not send the reader to an administrator who has no control either", () => {
    const banner = renderBanner(<MachineBanner status="none" fact={chatFact("no_machine")} />);
    expect(banner.textContent ?? "").not.toMatch(/organization admin|organisation admin/i);
  });

  it("does not ask for a reload of a state the page polls for itself", () => {
    const banner = renderBanner(<MachineBanner status="none" fact={chatFact("no_machine")} />);
    expect(banner.textContent ?? "").not.toMatch(/reload this page/i);
  });

  it("states the fact in the server's sentence, and offers no support escalation", async () => {
    const banner = renderBanner(<MachineBanner status="none" fact={chatFact("no_machine")} />);
    expect(banner).toHaveTextContent("No machine can serve your organization right now.");
    // Let the config read land: the link, were it offered, renders after it.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(within(banner).queryByRole("link")).toBeNull();
    expect(banner.textContent ?? "").not.toMatch(/contact|support/i);
  });
});

describe("a chat whose machine was released with nothing to take it", () => {
  it("says it continues on the next machine, offers no escalation and asks for no reload", async () => {
    const banner = renderBanner(<MachineBanner status="stranded" fact={chatFact("released")} />);
    expect(banner).toHaveAttribute("data-state", "unavailable");
    expect(banner).toHaveTextContent("The machine this chat ran on is no longer active.");
    expect(banner.textContent ?? "").not.toMatch(/organization admin|reload this page/i);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(within(banner).queryByRole("link")).toBeNull();
    expect(banner.textContent ?? "").not.toMatch(/contact|support/i);
  });

  it("closes the composer", () => {
    expect(machineIsReady("stranded")).toBe(false);
  });
});

describe("a chat the machine serving it refused", () => {
  it("states the kind in one line of the server's, and sends the reader nowhere", async () => {
    const banner = renderBanner(<MachineBanner status="refused" fact={chatFact("waiting_files")} />);
    expect(banner).toHaveTextContent("The machine serving it can't reach the chat's files right now.");
    expect(banner.textContent ?? "").not.toMatch(/organization admin|reload this page|support|contact/i);
    // Given a beat for the deployment's config to answer: a support link that
    // arrived with it would be the escalation this banner must not make.
    await waitFor(() => expect(within(banner).queryByRole("link")).toBeNull());
  });
});

describe("a workspace that is fixing itself", () => {
  it.each([
    ["starting", "starting"],
    ["unreachable", "unreachable"],
    ["restarting", "restarting"],
  ] as const)("offers no escalation while %s", (status, fact) => {
    const banner = renderBanner(<MachineBanner status={status} fact={chatFact(fact)} />);
    expect(within(banner).queryByRole("link")).toBeNull();
    expect(within(banner).queryByRole("button")).toBeNull();
  });
});

describe("a workspace whose daemon is restarting in place", () => {
  it("says it is restarting and promises no new machine", () => {
    const banner = renderBanner(<MachineBanner status="restarting" fact={chatFact("restarting")} />);
    expect(banner).toHaveTextContent("lab-b is restarting and picks this chat up again when it is back.");
    expect(banner.textContent ?? "").not.toMatch(/replaced|new machine/i);
  });
});

describe("what the banner draws", () => {
  it("draws nothing for a healthy or resting chat", () => {
    for (const name of ["working", "awake", "asleep", "waking", "queued"]) {
      const { container, unmount } = render(<MachineBanner status="ready" fact={chatFact(name)} />);
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });

  it("says a stalled turn and a turn stopped for credit, which no machine word could", () => {
    const stalled = renderBanner(<MachineBanner status="ready" fact={chatFact("stalled_turn_silent")} />);
    expect(stalled).toHaveTextContent("The agent has stopped reporting progress.");
    cleanup();
    const stopped = renderBanner(<MachineBanner status="asleep" fact={chatFact("stopped_credits")} />);
    expect(stopped).toHaveTextContent("Stopped because the organization ran out of credit.");
  });

  it("says the box's socket went away before the server's read can", () => {
    // The page saw the socket go; the last read still says the chat is awake.
    const banner = renderBanner(<MachineBanner status="unreachable" fact={chatFact("awake")} />);
    expect(banner).toHaveTextContent("The machine is unreachable");
  });

  it("prefers the server's sentence once it has one", () => {
    const banner = renderBanner(<MachineBanner status="unreachable" fact={chatFact("unreachable")} />);
    expect(banner).toHaveTextContent("lab-b isn't responding.");
    expect(banner.textContent ?? "").not.toMatch(/The machine is unreachable/);
  });
});
