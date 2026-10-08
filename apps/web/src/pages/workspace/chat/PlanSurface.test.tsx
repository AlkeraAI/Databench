// PlanSurface renders an approval plan (the markdown carried by a plan_present
// question part) in a stacked editor page, reached via the plan tool card's
// "Open in New Tab" action.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const { ds } = vi.hoisted(() => ({
  ds: { getChatTurns: vi.fn(async () => [] as unknown[]) },
}));

vi.mock("./data", () => ({
  chatData: () => ds,
  chatCaps: () => ({ opencodeActive: false }),
  errorText: (err: unknown) =>
    typeof err === "object" && err !== null && typeof (err as { message?: unknown }).message === "string"
      ? (err as { message: string }).message
      : String(err),
}));

import { PlanSurface } from "./PlanSurface";

const planTurns = [
  {
    id: "a1",
    author: "assistant",
    status: "complete",
    parts: [
      {
        id: "plan-part-1",
        kind: "question",
        requestId: "req-1",
        questionKind: "plan_approval",
        status: "pending",
        questions: [
          {
            header: "alkera:plan-approval",
            question: "## Migration plan\n\n- Add the `lineage` column\n- Backfill rows",
            custom: true,
            multiple: false,
            options: [{ label: "Accept — run normally (ask before each change)" }],
          },
        ],
      },
    ],
  },
];

function renderPlan(initialEntry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/editor/plan/:chatId/:partId" element={<PlanSurface />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  ds.getChatTurns.mockResolvedValue([]);
});

describe("PlanSurface", () => {
  it("renders the plan markdown from the matching question part", async () => {
    ds.getChatTurns.mockResolvedValue(planTurns);
    const { container } = renderPlan("/editor/plan/chat-1/plan-part-1");

    // The plan markdown becomes real nodes — the "## Migration plan" heading
    // is an <h2>, not raw text.
    await waitFor(() => {
      expect(container.querySelector(".alk-markdown h2")).toHaveTextContent("Migration plan");
    });
    expect(screen.queryByText("## Migration plan")).not.toBeInTheDocument();
    // Inline code is preserved as a <code> node.
    expect(container.querySelector(".alk-markdown code")).toHaveTextContent("lineage");
    expect(ds.getChatTurns).toHaveBeenCalledWith("chat-1");
  });

  it("shows a not-found message when the part id has no matching plan", async () => {
    ds.getChatTurns.mockResolvedValue(planTurns);
    renderPlan("/editor/plan/chat-1/does-not-exist");

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Plan not found.");
    });
  });
});
