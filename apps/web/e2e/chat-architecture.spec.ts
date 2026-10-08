// The canonical chat surface, driven end to end in a real browser. These specs
// pin what a reader actually gets there -- the transcript's
// folded tool run, the interrupt that replaces the composer while a permission
// waits, and the empty chat that invents nothing.
//
// Each contract is stated against the one shipping chat DOM.

import { mkdirSync } from "node:fs";
import { expect, test } from "@playwright/test";

import { CHAT_ID, gotoSurface, installFixture, pushHarnessEvent } from "./_chatHarness";

const SCREENSHOT_DIR = "e2e/__screenshots__/chat-architecture";

/** One finished turn: a user ask, a write enriched by its file.edited diff, a
 *  terminal run, then the closing prose. Fixture input, not a copy of any wire
 *  capture. */
const FINISHED_RUN_EVENTS = [
  { event_type: "message.created", message_id: "u1", role: "user" },
  {
    event_type: "part.created",
    message_id: "u1",
    part: { part_id: "u1-text", message_id: "u1", type: "text", text: "Refactor the chat UX packages." },
  },
  { event_type: "turn.started", turn_id: "turn-done" },
  { event_type: "message.created", message_id: "a1", role: "assistant" },
  {
    event_type: "tool.call",
    message_id: "a1",
    tool_call_id: "write-1",
    tool_name: "write",
    tool_kind: "write",
    status: "running",
    input: { path: "scratch/fib.py", content: "def fib(n):\n    return n" },
  },
  { event_type: "tool.call_update", tool_call_id: "write-1", status: "completed", output: { path: "scratch/fib.py" } },
  {
    event_type: "file.edited",
    event_id: "edit-1",
    path: "scratch/fib.py",
    insertions: 42,
    deletions: 11,
    preview: { kind: "diff", title: "fib.py", content: "- old\n+ new" },
  },
  {
    event_type: "tool.call",
    message_id: "a1",
    tool_call_id: "bash-1",
    tool_name: "bash",
    tool_kind: "terminal",
    status: "running",
    input: { command: "pnpm --filter @alkera/ui test" },
  },
  { event_type: "tool.call_update", tool_call_id: "bash-1", status: "completed", output: "17 tests passed" },
  {
    event_type: "part.created",
    message_id: "a1",
    part: { part_id: "a1-text", message_id: "a1", type: "text", text: "All green." },
  },
  { event_type: "turn.finished", event_id: "sum-1", stop_reason: "complete", summary: "Repackaged the chat resources." },
];

// A live permission request. It arrives AFTER the replay (through the real
// host channel), so the replay's stale-interrupt settle cannot resolve it.
const PERMISSION_EVENT = {
  event_type: "permission.request",
  request_id: "perm-e2e",
  permission_kind: "bash",
  canonical_kind: "shell",
  prompting: true,
  patterns: ["pnpm --filter @alkera/web e2e"],
  options: [
    { option_id: "allow_once", name: "Allow once" },
    { option_id: "allow_always", name: "Allow always" },
    { option_id: "reject_once", name: "Reject" },
  ],
};

test.beforeAll(() => {
  mkdirSync(SCREENSHOT_DIR, { recursive: true });
});

test("a finished run loads folded and opens down to the file", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page, { events: FINISHED_RUN_EVENTS });
  await gotoSurface(page, `/chat/${CHAT_ID}`, ".chat-tape");

  // The turn's frame: the ask as an order ticket, the closing prose after the run.
  await expect(page.locator(".chat-ticket__body")).toHaveText("Refactor the chat UX packages.");
  await expect(page.getByText("All green.")).toBeVisible();

  // A group mounted already-finished is history: its digest reads the run's
  // size and loads collapsed, no step rows on screen.
  const digest = page.getByRole("button", { name: /2 tool calls/i });
  await expect(digest).toBeVisible();
  await expect(digest).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".chat-activity-step")).toHaveCount(0);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-folded.png`, fullPage: false });

  // Opening the digest lays out the ledger: the write with its diff figure
  // and worded disclosure, the terminal run beside it.
  await digest.click();
  await expect(digest).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(".chat-activity-step")).toHaveCount(2);
  const writeHead = page.getByRole("button", { name: /Wrote.*fib\.py/i });
  await expect(writeHead).toBeVisible();
  await expect(writeHead).toContainText("+42");
  await expect(writeHead).toContainText("Show file");
  await expect(page.getByRole("button", { name: /Ran.*pnpm/i })).toBeVisible();

  // History seeds nothing open: the file waits behind its word, and the well
  // that opens is the file itself -- path band over the authored lines.
  await writeHead.click();
  const well = page.locator('[data-tool="write"]');
  await expect(well).toBeVisible();
  await expect(well.getByText("def fib")).toBeVisible();
  await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-write-open.png`, fullPage: false });

  // The terminal well holds what the run printed.
  await page.getByRole("button", { name: /Ran.*pnpm/i }).click();
  await expect(page.locator('[data-tool="bash"]')).toContainText("17 tests passed");
});

test("a live permission request swaps the composer for the interrupt", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page, { events: FINISHED_RUN_EVENTS });
  await gotoSurface(page, `/chat/${CHAT_ID}`, ".chat-tape");

  const composer = page.getByRole("textbox");
  await expect(composer).toBeVisible();

  await pushHarnessEvent(page, CHAT_ID, PERMISSION_EVENT);

  // The dock's content IS the card now: the ask names the consequence, the
  // subject is the exact command, and the decisions are on screen.
  const card = page.getByRole("region", { name: /run this command/i });
  await expect(card).toBeVisible();
  await expect(card).toContainText(PERMISSION_EVENT.patterns[0]);
  await expect(card.getByRole("button", { name: /allow once/i })).toBeVisible();
  await expect(card.getByRole("button", { name: /reject/i })).toBeVisible();
  // The interrupt REPLACES the composer rather than disabling it under the card.
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await page.locator(".chat-dock").screenshot({ path: `${SCREENSHOT_DIR}/chat-permission-dock.png` });
});

test("an empty chat invents no transcript", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page, { events: [] });
  await gotoSurface(page, `/chat/${CHAT_ID}`, ".chat-tape");

  // The opener and its one-press suggestions stand in for the tape...
  await expect(page.getByRole("heading", { name: /what should alkera build/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /explain this codebase/i })).toBeVisible();
  await expect(page.getByRole("textbox")).toBeVisible();

  // ...and nothing else does: no fabricated tickets, runs, or step rows.
  await expect(page.locator(".chat-ticket")).toHaveCount(0);
  await expect(page.locator(".chat-activity-group")).toHaveCount(0);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-empty.png`, fullPage: false });
});
