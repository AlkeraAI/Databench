// The Files pane re-reads its folders the moment a chat wakes, and only then.

import { renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import type { SessionState as LifeState } from "@/pages/workspace/workspaces/useRefreshFilesOnWake";
import { useRefreshFilesOnWake } from "@/pages/workspace/workspaces/useRefreshFilesOnWake";

function setup(first: LifeState | null) {
  const client = createQueryClient();
  client.setQueryData(keys.files.item("root"), { id: "root" });
  const stale = () => client.getQueryState(keys.files.item("root"))?.isInvalidated ?? false;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(({ life }: { life: LifeState | null }) => useRefreshFilesOnWake(life), {
    wrapper,
    initialProps: { life: first },
  });
  return { hook, stale };
}

describe("useRefreshFilesOnWake", () => {
  it.each<[LifeState, LifeState]>([
    ["asleep", "awake"],
    ["waking", "awake"],
    ["waking", "working"],
    ["queued", "working"],
  ])("re-reads the Files family going from %s to %s", (from, to) => {
    const { hook, stale } = setup(from);
    expect(stale()).toBe(false);
    hook.rerender({ life: to });
    expect(stale()).toBe(true);
  });

  it.each<[LifeState | null, LifeState | null]>([
    ["awake", "working"],
    ["awake", "asleep"],
    ["asleep", "waking"],
    ["waking", "queued"],
    [null, "awake"],
  ])("leaves the Files family alone going from %s to %s", (from, to) => {
    const { hook, stale } = setup(from);
    hook.rerender({ life: to });
    expect(stale()).toBe(false);
  });
});
