import { describe, expect, it } from "vitest";
import { commandPartFromOutcome } from "./commandCard";

describe("commandPartFromOutcome", () => {
  it("renders /title set + show variants from the payload", () => {
    expect(commandPartFromOutcome("title", "ok", { action: "set", title: "Data sweep" }, null)).toEqual({
      command: "title",
      label: "Title updated",
      detail: "Data sweep",
    });
    expect(commandPartFromOutcome("title", "ok", { action: "show", title: "Data sweep" }, null)).toEqual({
      command: "title",
      label: "Chat title",
      detail: "Data sweep",
    });
  });

  // With no extension installed the card reads nothing off the payload's credits.
  it("renders /usage with no plan or credit rows", () => {
    const credits = { tier_name: "Pro", pct_used: 12, prepaid_credits: 12345 };
    const card = commandPartFromOutcome("usage", "ok", { credits, chat_credits: 678 }, null);
    expect(card).toEqual({ command: "usage", label: "Usage", detail: undefined, stats: [] });
  });

  it("renders a /usage error payload as an error card", () => {
    expect(
      commandPartFromOutcome("usage", "ok", { error: "gateway unreachable" }, null),
    ).toEqual({ command: "usage", label: "Usage", detail: "gateway unreachable", tone: "error" });
  });

  it("renders cli_only as the editor-equivalent hint", () => {
    expect(commandPartFromOutcome("mode", "cli_only", {}, "Use the mode picker.")).toEqual({
      command: "mode",
      label: "/mode is a CLI command",
      detail: "Use the mode picker.",
    });
  });

  it("renders error outcomes with error tone and their own labels", () => {
    expect(commandPartFromOutcome("xyz", "unknown", {}, "unknown command `/xyz`")).toEqual({
      command: "xyz",
      label: "Unknown command",
      detail: "unknown command `/xyz`",
      tone: "error",
    });
    expect(commandPartFromOutcome("usage", "bad_usage", {}, "usage: /usage [(window)]")).toEqual({
      command: "usage",
      label: "/usage",
      detail: "usage: /usage [(window)]",
      tone: "error",
    });
  });

  it("falls back to a generic card for an unmapped ok command", () => {
    expect(commandPartFromOutcome("doctor", "ok", {}, "all good")).toEqual({
      command: "doctor",
      label: "/doctor",
      detail: "all good",
    });
  });

  it("declines a command whose result has its own event or navigates", () => {
    expect(commandPartFromOutcome("clear", "ok", {}, null)).toBeNull();
    expect(commandPartFromOutcome("compact", "ok", {}, null)).toBeNull();
    expect(commandPartFromOutcome("exit", "exit", {}, null)).toBeNull();
  });
});
