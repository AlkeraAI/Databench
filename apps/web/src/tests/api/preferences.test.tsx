// The reader's preferences as the browser's editor holds them.
//
// The server answers the Default Chat Model resolved: a pick that no longer
// resolves, or none at all, reads as the platform default and is flagged
// `chat_model_defaulted`. The editor must hold that as "no preference" — the
// Settings page PATCHes every field it edits, so holding the answered model
// would save it back as a pick and pin a reader who never chose to today's
// default, out of reach of the next change of default.

import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { useMyPreferences, useSetMyPreferences } from "@/api/preferences";
import { createQueryClient } from "@/api/queryClient";

function answer(body: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
}

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient({ retry: false })}>{children}</QueryClientProvider>;
}

const RESOLVED = {
  default_permission_mode: "plan",
  default_chat_model: "claude-sonnet-5.5",
  default_chat_effort: "medium",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useMyPreferences", () => {
  it("holds a defaulted model as no preference, and keeps every other field", async () => {
    answer({ preferences: RESOLVED, chat_model_defaulted: true });

    const { result } = renderHook(() => useMyPreferences(), { wrapper });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual({
      default_permission_mode: "plan",
      default_chat_model: null,
      default_chat_effort: null,
    });
  });

  it("keeps the reader's own pick as their pick", async () => {
    answer({ preferences: RESOLVED, chat_model_defaulted: false });

    const { result } = renderHook(() => useMyPreferences(), { wrapper });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data).toEqual(RESOLVED);
  });
});

describe("useSetMyPreferences", () => {
  it("answers a save the same way, so a stale pick cleared by the server stays cleared", async () => {
    answer({ preferences: RESOLVED, chat_model_defaulted: true });

    const { result } = renderHook(() => useSetMyPreferences(), { wrapper });
    const saved = await result.current.mutateAsync({ default_permission_mode: "plan" });

    expect(saved.default_chat_model).toBeNull();
    expect(saved.default_chat_effort).toBeNull();
    expect(saved.default_permission_mode).toBe("plan");
  });
});
