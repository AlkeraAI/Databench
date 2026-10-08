// What a failed turn offers the reader when no extension answers money refusals: the
// open causes resolve to their one next step, and a money cause states the cause alone,
// with no plan or credits link, whatever the wire says the reader may open.

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RETRY, useChatFailureNote } from "@/pages/workspace/chat/useChatFailureNote";

function resolver(onRetry?: () => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  return renderHook(() => useChatFailureNote({ enabled: false, onRetry }), { wrapper }).result.current;
}

describe("a failed turn with no billing extension installed", () => {
  it.each(["out_of_usage", "no_funding", "balance_owed", "pool_exhausted", "member_budget_exhausted", "member_pool_limit_exhausted", "cap_approval_required"])(
    "states %s alone, offering no page",
    (cause) => {
      expect(resolver()(cause, { manageUrl: "https://billing.example.com/plan", resetsAt: "2099-01-01T00:00:00Z" })).toBeNull();
    },
  );

  it("offers a retry for an unavailable provider when the shell can take a turn", () => {
    const onRetry = vi.fn();
    const note = resolver(onRetry)("provider_unavailable");
    expect(note?.action).toEqual({ label: RETRY, onClick: onRetry });
  });

  it("offers nothing for an unavailable provider when the shell cannot retry", () => {
    expect(resolver()("provider_unavailable")).toBeNull();
  });

  it("points a too-long context at a new chat or /compact", () => {
    expect(resolver()("context_too_long")).toEqual({ body: "Start a new chat, or run /compact to shorten this one." });
  });
});
