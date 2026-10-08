// The notebook output frame in a real browser, under the real policy.
//
// The bootstrap page and its headers come from the backend itself (the content
// app is driven in-process by a short Python script), and are served verbatim
// from a second origin. A stand-in notebook tab on a first origin mounts
// outputs through the real FrameHost. What this proves, that jsdom cannot:
// the frame modules render under the content origin's HTML policy with no
// violation, the handshake works with and without `location.ancestorOrigins`,
// and a script inside an HTML output can reach nothing on the network.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

import { buildFrameModule, bundleIife } from "../../../packages/notebook-ui/vite/frameModules";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "../../..");
const BUILD_HASH = "0123abcd";
// The same page with `location.ancestorOrigins` taken away, as Firefox has it:
// Chromium's is unforgeable, so the page text is the only place to remove it.
const NO_ANCESTORS_HASH = "fff0fff0";
const ASSET = `alkera-asset:sha256:${"ab".repeat(32)}`;

// A stand-in for the widget manager with the shape of the real bundle: it
// registers its exports as `alkera-widgets` and is started once and handed
// every message. It draws one button per replayed model, sends on click, and
// records what it is handed.
const STUB_WIDGETS = `window.__alkRegister("alkera-widgets", "0.1.0", { start: function (o) { return { handle: function (m) {
  if (m.type === "init") {
    var b = document.createElement("button"); b.id = "send"; b.textContent = "models " + m.opens.length;
    b.onclick = function () { o.post({ type: "comm.send", comm_id: m.opens[0].comm_id, msg_id: "m-1",
      content: { comm_id: m.opens[0].comm_id, data: { method: "update", state: { value: 2 } } }, buffers: [] }); };
    var a = document.createElement("button"); a.id = "asset"; a.textContent = "asset";
    a.onclick = function () { o.post({ type: "need_module", name: "${ASSET}", version: "asset" }); };
    o.root.append(b, a);
  } else if (m.type === "comm.status") { o.root.dataset.idle = m.msg_id; }
  else if (m.type === "module") { o.root.dataset.asset = m.code; }
} }; } });`;

// A machine whose cached browser is not the build this Playwright pins can name
// one it has.
if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) {
  test.use({ launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } });
}

const DUMP = `
import json, sys
from starlette.applications import Starlette
from starlette.testclient import TestClient
from backend.content_app import CONTENT_MOUNT_PATH, build_content_app
host = Starlette()
host.mount(CONTENT_MOUNT_PATH, build_content_app(routers=[]))
response = TestClient(host, base_url=sys.argv[1]).get(sys.argv[2])
with open(sys.argv[3], "w") as out:
    json.dump({"status": response.status_code, "headers": dict(response.headers), "body": response.text}, out)
`;

interface Dumped {
  status: number;
  headers: Record<string, string>;
  body: string;
}

let appServer: Server;
let contentServer: Server;
let appOrigin = "";
let contentOrigin = "";
let bootstrap: Dumped;
const beacons: string[] = [];
const modules = new Map<string, string>();
let parentScript = "";

function withoutAncestors(body: string): string {
  const line = "const ancestors = location.ancestorOrigins;";
  if (!body.includes(line)) throw new Error("the bootstrap no longer reads ancestorOrigins where this check expects");
  return body.replace(line, "const ancestors = undefined;");
}

function listen(handler: (req: IncomingMessage, res: ServerResponse) => void): Promise<[Server, number]> {
  const server = createServer(handler);
  return new Promise((done) => server.listen(0, "127.0.0.1", () => done([server, (server.address() as AddressInfo).port])));
}

test.beforeAll(async () => {
  test.setTimeout(240_000);
  let appPort = 0;
  [appServer, appPort] = await listen((req, res) => {
    const url = req.url ?? "/";
    if (url.startsWith("/beacon")) {
      beacons.push(url);
      res.writeHead(204).end();
      return;
    }
    if (url === "/parent.html") {
      res.writeHead(200, { "Content-Type": "text/html", "Cross-Origin-Opener-Policy": "same-origin" });
      res.end(`<!doctype html><html><body><script>${parentScript}</script></body></html>`);
      return;
    }
    const module = /^\/modules\/(.+)\.js$/.exec(url);
    const code = module ? modules.get(decodeURIComponent(module[1])) : undefined;
    if (code !== undefined) {
      res.writeHead(200, { "Content-Type": "text/plain" }).end(code);
      return;
    }
    res.writeHead(404).end();
  });
  appOrigin = `http://127.0.0.1:${appPort}`;

  let contentPort = 0;
  [contentServer, contentPort] = await listen((req, res) => {
    if (req.url === `/c/nb-output/${BUILD_HASH}`) {
      res.writeHead(bootstrap.status, bootstrap.headers).end(bootstrap.body);
    } else if (req.url === `/c/nb-output/${NO_ANCESTORS_HASH}`) {
      res.writeHead(bootstrap.status, bootstrap.headers).end(withoutAncestors(bootstrap.body));
    } else {
      res.writeHead(404).end();
    }
  });
  // A different host name, so the two are different origins for the browser.
  contentOrigin = `http://localhost:${contentPort}`;

  // The access log writes to stdout, so the response goes to a file.
  const dumpPath = join(mkdtempSync(join(tmpdir(), "nb-frame-")), "bootstrap.json");
  execFileSync("uv", ["run", "--frozen", "python", "-c", DUMP, contentOrigin, `/c/nb-output/${BUILD_HASH}`, dumpPath], {
    cwd: REPO,
    stdio: ["ignore", "ignore", "inherit"],
    env: {
      ...process.env,
      FILES_ENABLED: "true",
      FILES_CONTENT_BASE_URL: contentOrigin,
      API_CORS_ORIGINS: appOrigin,
      FRONTEND_BASE_URL: appOrigin,
    },
  });
  bootstrap = JSON.parse(readFileSync(dumpPath, "utf-8")) as Dumped;
  // Only the headers a browser acts on travel; the length is recomputed.
  delete bootstrap.headers["content-length"];

  for (const name of ["nb-html", "nb-svg", "nb-vega", "nb-plotly"]) modules.set(name, (await buildFrameModule(name)).code);
  modules.set("@alkera/widgets", STUB_WIDGETS);
  // Not a script: injected by the bootstrap it would be a syntax error.
  modules.set(ASSET, "export default {render() {}} // (");
  parentScript = (await bundleIife(resolve(HERE, "_nbFrameParent.ts"))).code;
});

test.afterAll(() => {
  appServer?.close();
  contentServer?.close();
});

/** CSP reports Chromium writes to the console, from any frame of the page. */
function watchViolations(page: Page): string[] {
  const violations: string[] = [];
  page.on("console", (message) => {
    const text = message.text();
    if (/Content Security Policy|Refused to/i.test(text)) violations.push(text);
  });
  return violations;
}

async function mount(
  page: Page,
  mime: string,
  data: unknown,
  options: { hash?: string; readonly?: boolean; height?: number } = {},
): Promise<number> {
  return page.evaluate(
    ([m, d, url, readonly, height]) => window.mountOutput(m, d, url, { readonly, height }),
    [mime, data, `${contentOrigin}/c/nb-output/${options.hash ?? BUILD_HASH}`, options.readonly ?? true, options.height ?? 40] as const,
  );
}

async function settled(page: Page, id: number): Promise<{ ready: boolean; sizes: number[]; errors: string[]; links: string[] }> {
  await expect.poll(() => page.evaluate((i) => window.outputs[i].sizes.length, id), { timeout: 20_000 }).toBeGreaterThan(0);
  return page.evaluate((i) => {
    const o = window.outputs[i];
    return { ready: o.ready, sizes: o.sizes, errors: o.errors, links: o.links };
  }, id);
}

test("the bootstrap is served under the content origin's HTML policy, framable only by the app", () => {
  expect(bootstrap.status).toBe(200);
  expect(bootstrap.headers["content-security-policy"]).toBe(
    "default-src 'none'; script-src 'unsafe-inline'; object-src 'none'; style-src 'unsafe-inline'; " +
      `img-src data:; font-src data:; media-src data:; frame-ancestors ${appOrigin}; base-uri 'none'; ` +
      "form-action 'none'; sandbox allow-scripts",
  );
});

test("an HTML output renders and runs its scripts with no policy violation", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "text/html", '<p id="t">waiting</p><script>document.getElementById("t").textContent = "ran";</script>');
  const state = await settled(page, id);
  expect(state.ready).toBe(true);
  await expect(page.frameLocator(`#out-${id}`).locator("#t")).toHaveText("ran");
  expect(state.errors).toEqual([]);
  expect(violations).toEqual([]);
});

test("a Vega-Lite output renders with the interpreter and no policy violation", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "application/vnd.vegalite.v6+json", {
    data: { values: [{ a: "x", b: 2 }, { a: "y", b: 5 }, { a: "z", b: 3 }] },
    transform: [{ calculate: "datum.b * 2", as: "c" }],
    mark: "bar",
    encoding: { x: { field: "a", type: "nominal" }, y: { field: "c", type: "quantitative" } },
  });
  await settled(page, id);
  await expect(page.frameLocator(`#out-${id}`).locator(".mark-rect path")).toHaveCount(3);
  expect(await page.evaluate((i) => window.outputs[i].errors, id)).toEqual([]);
  expect(violations).toEqual([]);
});

test("an Altair output with expression filters, calculates and conditions renders under the policy", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  // As Altair 6 writes `transform_calculate`, `transform_filter(alt.datum ...)`
  // and `alt.condition(alt.datum ..., ...)`, under the v5 MIME it publishes.
  const id = await mount(page, "application/vnd.vegalite.v5+json", {
    $schema: "https://vega.github.io/schema/vega-lite/v6.4.1.json",
    config: { view: { continuousWidth: 300, continuousHeight: 300 } },
    data: { name: "data-1" },
    datasets: { "data-1": [{ a: "x", b: 2 }, { a: "y", b: 5 }, { a: "z", b: 3 }, { a: "w", b: 1 }] },
    transform: [{ calculate: "(sqrt(datum.b) * 2)", as: "c" }, { filter: "((datum.b > 1) && (datum.a !== 'z'))" }],
    mark: { type: "bar" },
    encoding: {
      x: { field: "a", type: "nominal" },
      y: { field: "c", type: "quantitative" },
      color: { condition: { test: "(datum.b > 4)", value: "crimson" }, value: "gray" },
    },
  });
  await settled(page, id);
  const bars = page.frameLocator(`#out-${id}`).locator(".mark-rect path");
  await expect(bars).toHaveCount(2);
  await expect(bars.and(page.frameLocator(`#out-${id}`).locator('[fill="crimson"]'))).toHaveCount(1);
  expect(await page.evaluate((i) => window.outputs[i].errors, id)).toEqual([]);
  expect(violations).toEqual([]);
});

test("a Vega-Lite output whose expression reaches outside the allowlist is refused before Vega runs", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "application/vnd.vegalite.v5+json", {
    data: { values: [{ a: "x", b: 2 }] },
    transform: [{ calculate: "datum.b.constructor", as: "c" }],
    mark: "bar",
    encoding: { x: { field: "a", type: "nominal" }, y: { field: "b", type: "quantitative" } },
  });
  await expect.poll(() => page.evaluate((i) => window.outputs[i].errors.length, id), { timeout: 20_000 }).toBe(1);
  const [error] = await page.evaluate((i) => window.outputs[i].errors, id);
  expect(error).toContain("transform[0].calculate");
  expect(error).not.toContain("constructor");
  await expect(page.frameLocator(`#out-${id}`).locator("svg")).toHaveCount(0);
  expect(violations).toEqual([]);
});

test("a Plotly output renders with the strict bundle and no policy violation", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "application/vnd.plotly.v1+json", {
    data: [{ type: "bar", x: ["a", "b", "c"], y: [1, 3, 2] }],
    layout: { title: { text: "Bars" } },
  });
  await expect(page.frameLocator(`#out-${id}`).locator(".main-svg .point")).toHaveCount(3, { timeout: 30_000 });
  const titles = await page
    .frameLocator(`#out-${id}`)
    .locator(".modebar-btn")
    .evaluateAll((buttons) => buttons.map((b) => b.getAttribute("data-title") ?? ""));
  expect(titles.length).toBeGreaterThan(0);
  expect(titles.filter((t) => /download|png|image/i.test(t))).toEqual([]);
  expect(await page.evaluate((i) => window.outputs[i].errors, id)).toEqual([]);
  expect(violations).toEqual([]);
});

test("the frame is as tall as its content, growing and shrinking with it", async ({ page }) => {
  await page.goto(`${appOrigin}/parent.html`);
  // Drawn far taller than its content (a browser's default frame is 150 px):
  // the frame reports the content's height, not its own.
  const id = await mount(page, "text/html", '<div id="box" style="height: 30px; background: #ccc"></div>', { height: 300 });
  const frame = page.frameLocator(`#out-${id}`);
  await expect(frame.locator("#box")).toHaveCount(1);
  const last = () => page.evaluate((i) => window.outputs[i].sizes.at(-1), id);
  const shown = () => page.evaluate((i) => document.getElementById(`out-${i}`)!.getBoundingClientRect().height, id);
  await expect.poll(last).toBe(30);
  await expect.poll(shown).toBe(30);
  await frame.locator("#box").evaluate((el) => ((el as HTMLElement).style.height = "200px"));
  await expect.poll(last).toBe(200);
  await frame.locator("#box").evaluate((el) => ((el as HTMLElement).style.height = "20px"));
  await expect.poll(last).toBe(20);
  await expect.poll(shown).toBe(20);
});

test("an SVG output renders with no policy violation", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "image/svg+xml", '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect id="r" width="80" height="40"/></svg>');
  await settled(page, id);
  await expect(page.frameLocator(`#out-${id}`).locator("#r")).toHaveCount(1);
  expect(violations).toEqual([]);
});

test("a script inside an HTML output cannot reach the network", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  const html = `
    <p id="fetch">pending</p><p id="xhr">pending</p>
    <img src="${appOrigin}/beacon/img">
    <script>
      fetch("${appOrigin}/beacon/fetch").then(
        () => { document.getElementById("fetch").textContent = "reached"; },
        () => { document.getElementById("fetch").textContent = "blocked"; });
      try {
        const x = new XMLHttpRequest();
        x.open("GET", "${appOrigin}/beacon/xhr");
        x.onload = () => { document.getElementById("xhr").textContent = "reached"; };
        x.onerror = () => { document.getElementById("xhr").textContent = "blocked"; };
        x.send();
      } catch (e) { document.getElementById("xhr").textContent = "blocked"; }
      try { new WebSocket("${appOrigin.replace("http", "ws")}/beacon/ws"); } catch (e) {}
      try { navigator.sendBeacon("${appOrigin}/beacon/send"); } catch (e) {}
    </script>`;
  const id = await mount(page, "text/html", html);
  const frame = page.frameLocator(`#out-${id}`);
  await expect(frame.locator("#fetch")).toHaveText("blocked");
  await expect(frame.locator("#xhr")).toHaveText("blocked");
  // Give anything that slipped through time to land.
  await page.waitForTimeout(500);
  expect(beacons).toEqual([]);
  const errors = await page.evaluate((i) => window.outputs[i].errors, id);
  expect(errors.some((e) => e.startsWith("Blocked by the output sandbox"))).toBe(true);
  // The same watcher that finds none in the tests above does see the frame's.
  expect(violations.some((v) => v.includes("connect-src"))).toBe(true);
});

test("a link in an output goes to the page for confirmation and the frame stays put", async ({ page }) => {
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "text/html", '<a id="go" href="https://example.com/report">open</a>');
  await settled(page, id);
  const popups: unknown[] = [];
  page.on("popup", (popup) => popups.push(popup));
  await page.frameLocator(`#out-${id}`).locator("#go").click();
  await expect.poll(() => page.evaluate((i) => window.outputs[i].links, id)).toEqual(["https://example.com/report"]);
  expect(popups).toEqual([]);
  await expect(page.frameLocator(`#out-${id}`).locator("#go")).toHaveCount(1);
});

test("without ancestorOrigins the frame learns its parent from the first message", async ({ page }) => {
  const violations = watchViolations(page);
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "text/html", '<p id="t">waiting</p><script>document.getElementById("t").textContent = "ran";</script>', {
    hash: NO_ANCESTORS_HASH,
  });
  await expect(page.frameLocator(`#out-${id}`).locator("#t")).toHaveText("ran");
  // Its `ready` and `size` were held until the parent's first message named
  // the origin to post to, then delivered.
  const state = await settled(page, id);
  expect(state.ready).toBe(true);
  expect(violations).toEqual([]);
});

test("a widget view runs the manager bundle, sends through the bridge and gets its idle back", async ({ page }) => {
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "application/vnd.jupyter.widget-view+json", { model_id: "slider", version_major: 2 }, { readonly: false });
  const frame = page.frameLocator(`#out-${id}`);
  await expect(frame.locator("#send")).toHaveText("models 1");
  await frame.locator("#send").click();
  await expect.poll(() => page.evaluate(() => window.commSent.map((s) => [s.comm_id, s.msg_id]))).toEqual([["slider", "m-1"]]);
  await expect(frame.locator("#out")).toHaveAttribute("data-idle", "m-1");
  // An anywidget asset reaches the manager as text; the bootstrap does not run it.
  await frame.locator("#asset").click();
  await expect(frame.locator("#out")).toHaveAttribute("data-asset", "export default {render() {}} // (");
  expect(await page.evaluate((i) => window.outputs[i].errors, id)).toEqual([]);
});

test("a read-only widget view cannot send", async ({ page }) => {
  await page.goto(`${appOrigin}/parent.html`);
  const id = await mount(page, "application/vnd.jupyter.widget-view+json", { model_id: "slider", version_major: 2 });
  const frame = page.frameLocator(`#out-${id}`);
  await frame.locator("#send").click();
  await expect.poll(() => page.evaluate((i) => window.outputs[i].drops.readonly, id)).toBe(1);
  expect(await page.evaluate(() => window.commSent)).toEqual([]);
});

test("a page not on the app's list cannot frame the bootstrap", async ({ page }) => {
  // The content origin itself is not an app origin, so frame-ancestors refuses it.
  const violations = watchViolations(page);
  await page.goto(`${contentOrigin}/nope`).catch(() => undefined);
  await page.setContent(`<iframe id="f" sandbox="allow-scripts" src="${contentOrigin}/c/nb-output/${BUILD_HASH}#n=x"></iframe>`);
  await page.waitForTimeout(1_000);
  const frame = page.frames().find((f) => f.url().includes("/c/nb-output/"));
  const rendered = frame ? await frame.locator("#out").count().catch(() => 0) : 0;
  expect(rendered).toBe(0);
  expect(violations.some((v) => v.includes("frame-ancestors"))).toBe(true);
});
