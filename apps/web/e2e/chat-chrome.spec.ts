// The chat chrome, on both surfaces that wear it.
//
// The settings action is a menu, not a jump straight to preferences, and the
// home surface carries the same header the chat does -- same title slot, same
// quick links, same settings menu, same new-chat. These tests drive the real
// webview and read the rendered menu, so a wiring change that drops an action
// shows up here rather than in a bug report.
//
// The menu is pinned by ACTION ID, not by wording: which doors the menu offers
// and in what order is the contract, and rewording a row is not a regression.

import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

import { AUTHED, CHAT_ID, CHAT_TITLE, gotoSurface, installFixture, postedMessages } from "./_chatHarness";

const SCREENSHOT_DIR = "e2e/__screenshots__/chat-chrome";

/** The chrome's own header element: present on both surfaces. */
const CHROME = ".chat-chrome";

/** Every settings action the host wired, in the order the package renders it. */
const SETTINGS_ACTIONS = ["plugins", "jobs", "preferences", "logout"];

async function openSettings(page: Page): Promise<void> {
  const trigger = page.getByRole("button", { name: /account and settings/i });
  await expect(trigger).toBeVisible();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  // `toBeVisible` passes the moment the panel has a box, which is the first
  // frame of its 140ms rise -- still near transparent. Wait it out, so a
  // screenshot shows the menu the user reads rather than its entrance.
  await expect
    .poll(() => menu.evaluate((el) => Number(getComputedStyle(el).opacity)))
    .toBeGreaterThan(0.99);
}

/** The ids of the open menu's rows, in render order. */
async function settingsIds(page: Page): Promise<(string | null)[]> {
  return page.getByRole("menuitem").evaluateAll((rows) => rows.map((row) => row.getAttribute("data-menu-id")));
}

test.beforeAll(() => {
  mkdirSync(SCREENSHOT_DIR, { recursive: true });
});

test("the chat's settings action opens a menu", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page);
  await gotoSurface(page, `/chat/${CHAT_ID}`, CHROME);

  await openSettings(page);
  expect(await settingsIds(page)).toEqual(SETTINGS_ACTIONS);
  // The menu names the account it acts for.
  await expect(page.getByText(AUTHED.email)).toBeVisible();
  // Every row is a real door: none renders without a label.
  for (const text of await page.getByRole("menuitem").allInnerTexts()) {
    expect(text.trim().length).toBeGreaterThan(0);
  }
  // The whole page, not the header: the menu hangs below the chrome's own box.
  await page.screenshot({ path: `${SCREENSHOT_DIR}/chat-settings-open.png` });

  // Escape hands focus back to the trigger, the way the chrome's other menu does.
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /account and settings/i })).toBeFocused();
});

test("home wears the same chrome as the chat", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page);
  await gotoSurface(page, "/sidecar", CHROME);

  const chrome = page.locator(CHROME);
  await expect(chrome).toBeVisible();
  await expect(chrome.getByRole("heading")).toBeVisible();
  // The same quick-links rail the chat carries.
  const rail = chrome.getByRole("navigation", { name: "Project" });
  await expect(rail).toBeVisible();
  await expect(rail.getByRole("button")).toHaveCount(3);
  await expect(page.getByRole("button", { name: "New chat" })).toBeVisible();
  await page.locator(CHROME).screenshot({ path: `${SCREENSHOT_DIR}/home-chrome.png` });

  // Same menu, same actions, on the editor surface.
  await openSettings(page);
  expect(await settingsIds(page)).toEqual(SETTINGS_ACTIONS);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/home-settings-open.png` });
});

test("home keeps the chat list and composer under the chrome", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page);
  await gotoSurface(page, "/sidecar", CHROME);

  // The chrome must not have eaten the body: below it the home still lists
  // the host's chats, and its composer still takes a message.
  const list = page.locator(".chat-home");
  await expect(list).toBeVisible();
  await expect(list.getByText(CHAT_TITLE)).toBeVisible();
  const field = page.getByRole("textbox").first();
  await field.fill("the composer still takes a message");
  await expect(field).toHaveValue("the composer still takes a message");
  await page.screenshot({ path: `${SCREENSHOT_DIR}/home-full.png`, fullPage: false });
});

const WEB_APP = /open web app/i;

test("the web app is a standalone key beside the settings gear", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page);
  await gotoSurface(page, `/chat/${CHAT_ID}`, CHROME);

  // A key of its own, immediately left of the gear -- never a menu row (the
  // settingsIds cases above pin the menu to the four settings actions).
  const key = page.getByRole("button", { name: WEB_APP });
  await expect(key).toBeVisible();
  const keyBox = await key.boundingBox();
  const gearBox = await page.getByRole("button", { name: /account and settings/i }).boundingBox();
  if (!keyBox || !gearBox) throw new Error("key or gear has no box");
  expect(keyBox.x + keyBox.width).toBeLessThanOrEqual(gearBox.x + 0.5);
  expect(gearBox.x - (keyBox.x + keyBox.width)).toBeLessThan(16);

  // Pressing it hands the host the account's own frontend URL to open.
  await key.click();
  const opens = (await postedMessages(page)).filter(
    (message) => (message as { type?: string }).type === "openBrowser",
  );
  // pins-source: the webview->host wire envelope (host/vscode.ts), a frozen shape both sides must agree on.
  expect(opens).toEqual([{ type: "openBrowser", url: AUTHED.frontend_url }]);

  // Home carries the same key: the door out is not the chat's alone.
  await installFixture(page);
  await gotoSurface(page, "/sidecar", CHROME);
  await expect(page.getByRole("button", { name: WEB_APP })).toBeVisible();
});

test("no known frontend URL, no web-app key", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page, { authed: { ...AUTHED, frontend_url: null as unknown as string } });
  await gotoSurface(page, `/chat/${CHAT_ID}`, CHROME);

  // The key is a door only once the host knows where it goes; without the
  // URL it does not render, while the rest of the actions stand.
  await expect(page.getByRole("button", { name: /account and settings/i })).toBeVisible();
  await expect(page.getByRole("button", { name: WEB_APP })).toHaveCount(0);
});

test("home and chat keep an iconless identity at the same vertical position", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page);
  await gotoSurface(page, "/sidecar", CHROME);

  await expect(page.locator(".chat-chrome__title-icon")).toHaveCount(0);
  const homeBar = await page.locator(".chat-chrome__bar").boundingBox();
  const homeRail = await page.locator(".chat-chrome__rail").boundingBox();

  // Opening a chat replaces the title with the trail. Those controls must fit inside the
  // same reserved row, or the quick-link rail and every chrome action visibly jump downward.
  await installFixture(page);
  await gotoSurface(page, `/chat/${CHAT_ID}`, CHROME);
  const trail = page.locator(CHROME).getByRole("navigation", { name: "Chat trail" });
  await expect(trail.getByRole("heading", { name: CHAT_TITLE })).toBeVisible();
  await expect(page.locator(".chat-chrome__title-icon")).toHaveCount(0);
  const chatBar = await page.locator(".chat-chrome__bar").boundingBox();
  const chatRail = await page.locator(".chat-chrome__rail").boundingBox();
  if (!homeBar || !homeRail || !chatBar || !chatRail) throw new Error("chat chrome has no box");
  expect(chatBar.height).toBe(homeBar.height);
  expect(chatRail.y).toBe(homeRail.y);
});

test("the sign-out row wears the danger ink at rest", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 760 });
  await installFixture(page);
  await gotoSurface(page, `/chat/${CHAT_ID}`, CHROME);
  await openSettings(page);

  // Computed styles, not class names: the row must READ as the consequence it
  // is before the pointer reaches it. The danger token is resolved through a
  // probe inside the row, so the assertion holds in either theme.
  const inks = await page.getByRole("menu").evaluate((menu) => {
    const row = (id: string) => menu.querySelector<HTMLElement>(`[data-menu-id="${id}"]`);
    const logout = row("logout");
    const prefs = row("preferences");
    if (!logout || !prefs) throw new Error("menu rows missing");
    const probe = document.createElement("span");
    probe.style.color = "var(--chat-danger)";
    probe.style.backgroundColor = "var(--chat-danger-wash)";
    logout.appendChild(probe);
    const resolved = getComputedStyle(probe);
    const result = {
      danger: resolved.color,
      wash: resolved.backgroundColor,
      label: getComputedStyle(logout.querySelector(".chat-menu__label")!).color,
      icon: getComputedStyle(logout.querySelector(".chat-menu__icon")!).color,
      ground: getComputedStyle(logout).backgroundColor,
      plainLabel: getComputedStyle(prefs.querySelector(".chat-menu__label")!).color,
    };
    probe.remove();
    return result;
  });
  // At rest the text AND the icon carry the danger ink, on a quiet ground.
  expect(inks.label).toBe(inks.danger);
  expect(inks.icon).toBe(inks.danger);
  expect(inks.ground).toBe("rgba(0, 0, 0, 0)"); // pins-source: the CSS OM's serialization of a transparent background, a platform constant.
  // The ink is the row's own, not the menu's: a plain row does not wear it.
  expect(inks.plainLabel).not.toBe(inks.danger);

  // Approaching the row brings the ground along.
  const logoutRow = page.locator('[data-menu-id="logout"]');
  await logoutRow.hover();
  await expect
    .poll(() => logoutRow.evaluate((el) => getComputedStyle(el).backgroundColor))
    .toBe(inks.wash);
});
