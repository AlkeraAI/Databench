import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CostLedgerEntryView, CostStateView, DecisionView } from "@alkera/chat-model";
import { ChatActivity } from "./ChatActivity";

const decision: DecisionView = {
  at: 1_700_000_000,
  source: "harness",
  capability: "sql",
  effect: "destroy",
  operation: "DROP TABLE events",
  targets: ["events"],
  mode: "auto",
  decision: "reject",
  decidedBy: "judge",
  reasons: ["irreversible", "prod target"],
};

const ledgerEntry: CostLedgerEntryView = {
  entryId: "e1",
  connection: "snowflake",
  operation: "SELECT *",
  estimateUsd: 0.1,
  actualUsd: null,
  chargedUsd: 0.1,
  walletCurrency: "usd",
  at: 1_700_000_000,
};

const costState: CostStateView = {
  spent: { chat: 0.9, day: 6, week: 2 },
  caps: { per_query: 1, chat: 1, day: 5, week: 10 },
  orgManaged: false,
  unknownKeys: [],
};

function baseProps(overrides: Partial<Parameters<typeof ChatActivity>[0]> = {}) {
  return {
    decisions: { data: [decision] },
    safety: { data: [decision] },
    cost: { data: costState },
    ledger: { data: [ledgerEntry] },
    onSaveCaps: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

describe("ChatActivity", () => {
  it("defaults to Decisions and switches tabs", () => {
    render(<ChatActivity {...baseProps()} />);
    expect(screen.getByRole("tab", { name: "Decisions" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("sql")).toBeInTheDocument();
    expect(screen.getByText("destroy")).toBeInTheDocument();
    expect(screen.getAllByText("reject")[0]).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Cost" }));
    expect(screen.getByText("This chat")).toBeInTheDocument();
  });

  it.each([
    ["exec", "danger"],
    ["destroy", "danger"],
    ["egress", "danger"],
    ["write", "warning"],
    ["read", "neutral"],
  ])("tones a %s decision %s", (effect, tone) => {
    const row: DecisionView = { ...decision, effect, decision: "prompt" };
    render(<ChatActivity {...baseProps({ decisions: { data: [row] } })} />);
    expect(screen.getByText(effect)).toHaveAttribute("data-tone", tone);
  });

  it("the cost gauge tones and clamps each meter", () => {
    const { container } = render(<ChatActivity {...baseProps({ initialTab: "cost" })} />);
    const rows = container.querySelectorAll(".alk-chatact__gauge-row");
    // chat 0.9/1 = 90% → warn; day 6/5 = over; week 2/10 → ok.
    expect(rows[0]).toHaveAttribute("data-tone", "warn");
    expect(rows[1]).toHaveAttribute("data-tone", "over");
    expect(rows[2]).toHaveAttribute("data-tone", "ok");
    expect(screen.getByRole("progressbar", { name: "This chat spend vs cap" })).toHaveAttribute("aria-valuenow", "90");
    // An overspend clamps to a full bar rather than overflowing the track.
    expect(screen.getByRole("progressbar", { name: "Today spend vs cap" })).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByRole("progressbar", { name: "This week spend vs cap" })).toHaveAttribute("aria-valuenow", "20");
    // Two decimals, the same reading as every other amount in the product.
    expect(screen.getByText("$0.90 / $1.00")).toBeInTheDocument();
  });

  it.each([
    [0.0042, "<$0.01"],
    [0.125, "$0.13"],
    [1234.5, "$1,234.50"],
  ])("the ledger reads a %s charge as %s", (chargedUsd, reads) => {
    render(
      <ChatActivity
        {...baseProps({ initialTab: "cost", ledger: { data: [{ ...ledgerEntry, chargedUsd, actualUsd: chargedUsd }] } })}
      />,
    );
    expect(screen.getByText(reads)).toBeInTheDocument();
  });

  it("a capless window shows spend with no cap", () => {
    const capless: CostStateView = { ...costState, caps: { per_query: 1, chat: 1, day: 5 } };
    const { container } = render(<ChatActivity {...baseProps({ cost: { data: capless }, initialTab: "cost" })} />);
    const weekRow = container.querySelectorAll(".alk-chatact__gauge-row")[2];
    expect(weekRow).toHaveAttribute("data-tone", "none");
    expect(weekRow).toHaveTextContent("This week$2.00");
    expect(weekRow).not.toHaveTextContent("/");
  });

  // A cleared field is a REMOVED cap, not a zero: it must drop out of the
  // payload entirely rather than save as an unspendable limit.
  it.each([
    ["3", { per_query: 1, chat: 3, day: 5, week: 10 }],
    ["", { per_query: 1, day: 5, week: 10 }],
  ])("saves the chat cap edited to %s", async (value, expected) => {
    const onSaveCaps = vi.fn().mockResolvedValue(undefined);
    render(<ChatActivity {...baseProps({ onSaveCaps, initialTab: "cost" })} />);
    fireEvent.change(screen.getByLabelText(/This chat cap/i), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    await waitFor(() => expect(onSaveCaps).toHaveBeenCalledTimes(1));
    expect(onSaveCaps.mock.calls[0][0]).toEqual(expected);
  });

  it("rejects an invalid cap before calling onSaveCaps", () => {
    const onSaveCaps = vi.fn();
    render(<ChatActivity {...baseProps({ onSaveCaps })} />);
    fireEvent.click(screen.getByRole("tab", { name: "Cost" }));
    fireEvent.change(screen.getByLabelText(/Today cap/i), { target: { value: "-5" } });
    fireEvent.click(screen.getByRole("button", { name: /save limits/i }));
    expect(onSaveCaps).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/valid amount/i);
  });

  it("surfaces a save failure and re-enables the button", async () => {
    const onSaveCaps = vi.fn().mockRejectedValue(new Error("daemon offline"));
    render(<ChatActivity {...baseProps({ onSaveCaps, initialTab: "cost" })} />);
    const button = screen.getByRole("button", { name: /save limits/i });
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("daemon offline");
    expect(button).toBeEnabled();
  });

  it("makes caps read-only when org-managed", () => {
    render(<ChatActivity {...baseProps({ cost: { data: { ...costState, orgManaged: true } } })} />);
    fireEvent.click(screen.getByRole("tab", { name: "Cost" }));
    expect(screen.getByText(/managed by your organization/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save limits/i })).not.toBeInTheDocument();
  });

  it("marks an estimated charge until the actual lands", () => {
    const { rerender } = render(<ChatActivity {...baseProps({ initialTab: "cost" })} />);
    expect(screen.getByText(/\(est\.\)/)).toBeInTheDocument();
    rerender(
      <ChatActivity {...baseProps({ initialTab: "cost", ledger: { data: [{ ...ledgerEntry, actualUsd: 0.1 }] } })} />,
    );
    expect(screen.queryByText(/\(est\.\)/)).not.toBeInTheDocument();
  });

  it("the Safety tab foregrounds the judge's reason", () => {
    render(<ChatActivity {...baseProps()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Safety" }));
    expect(screen.getByText("irreversible")).toBeInTheDocument();
  });

  it("empty decision fields fall back to placeholders", () => {
    const blank: DecisionView = { ...decision, capability: "", effect: "", operation: "", decision: "", decidedBy: "" };
    render(<ChatActivity {...baseProps({ decisions: { data: [blank] } })} />);
    expect(screen.getAllByText("?")).toHaveLength(2);
    expect(screen.getByText(/action/)).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders per-tab loading / error / empty states", () => {
    render(
      <ChatActivity
        decisions={{ loading: true }}
        safety={{ data: [] }}
        cost={{ error: "cost is down" }}
        ledger={{ data: [] }}
        onSaveCaps={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/loading/i);

    fireEvent.click(screen.getByRole("tab", { name: "Cost" }));
    expect(screen.getByRole("alert")).toHaveTextContent("cost is down");

    fireEvent.click(screen.getByRole("tab", { name: "Safety" }));
    expect(screen.getByText(/no safety-judge verdicts/i)).toBeInTheDocument();
  });
});
