// The cloud source's half of a model switch: the server's verdicts, read
// verbatim, and the PUTs that carry the model the reader switched from.

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const READ = {
  current: { model_id: "claude-opus-5.5", effort: "high" },
  can_switch: true,
  applies: "after_reopen",
  billed_to_owner: true,
  options: [
    {
      model: { id: "claude-opus-5.5", display_name: "Claude Opus 5.5", wire: "anthropic", efforts: ["low", "high"], default_effort: "high" },
      state: "current",
      reason_code: null,
      message: null,
      escape_new_chat_model: null,
    },
    {
      model: { id: "gpt-5.5", display_name: "GPT-5.5", wire: "openai", efforts: [], default_effort: null },
      state: "unavailable",
      reason_code: "reasoning_not_readable",
      message: "This chat has reasoning from Claude Opus 5.5 that GPT-5.5 can't read. Start a new chat to use GPT-5.5.",
      group_message: "This chat has reasoning from Claude Opus 5.5 that these models can't read. Use them in a new chat.",
      escape_new_chat_model: "gpt-5.5",
    },
  ],
};

const STORED = {
  id: "c1",
  title: "Ops",
  updated_at: "2026-10-04T12:00:00Z",
  permission_mode: "default",
  model: {
    id: "claude-opus-5.5",
    display_name: "Claude Opus 5.5",
    wire: "anthropic",
    efforts: ["low", "high"],
    effort: "high",
  },
};

function source() {
  const rest = {
    chatModelOptions: vi.fn(async () => READ),
    setChatModel: vi.fn(async () => STORED),
    listAllChats: vi.fn(async () => ({ items: [STORED] })),
  };
  return { rest, source: new CloudDataSource({ rest: rest as never }) };
}

describe("the cloud model switch", () => {
  it("reads every verdict as the server decided it", async () => {
    const { source: cloud, rest } = source();

    const options = await cloud.modelOptions("c1");

    expect(rest.chatModelOptions).toHaveBeenCalledWith("c1");
    expect(options.currentModelId).toBe("claude-opus-5.5");
    expect(options.applies).toBe("after_reopen");
    expect(options.billedToOwner).toBe(true);
    expect(options.options.map((o) => [o.model.id, o.state, o.escapeNewChatModel])).toEqual([
      ["claude-opus-5.5", "current", null],
      ["gpt-5.5", "unavailable", "gpt-5.5"],
    ]);
    expect(options.options[1]?.message).toContain("can't read");
    expect(options.options[1]?.groupMessage).toContain("these models can't read");
    expect(options.options[0]?.groupMessage).toBeNull();
  });

  it("puts a switch with the model it was made from", async () => {
    const { source: cloud, rest } = source();

    await cloud.setModel("c1", "claude-fable-5.1", null, "claude-opus-5.5");

    expect(rest.setChatModel).toHaveBeenCalledWith("c1", "claude-fable-5.1", null, "claude-opus-5.5");
  });

  it("puts an effort change on the model the chat is already on", async () => {
    const { source: cloud, rest } = source();
    await cloud.listChats();

    await cloud.setEffort("c1", "low");

    expect(rest.setChatModel).toHaveBeenCalledWith("c1", "claude-opus-5.5", "low", "claude-opus-5.5");
  });

  it("refuses an effort change on a chat it holds no model for", async () => {
    const { source: cloud, rest } = source();

    await expect(cloud.setEffort("c-unknown", "low")).rejects.toThrow();
    expect(rest.setChatModel).not.toHaveBeenCalled();
  });
});
