// The widget walkthrough: the supported widget set in headless Chromium, in
// output frames under the content app's real CSP, against a real kernel,
// through the engine's widget hub and asset store and the built
// @alkera/widgets bundle. Every scenario asserts; the process exits non-zero
// when any fails.
//
//   pnpm --filter @alkera/widgets build
//   node ./tests/walkthrough/run.mjs [scenario ...]
//
// Starts walkthrough/server.py with uv (Python packages from requirements.txt).
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_PORT = Number(process.env.ALK_WIDGETS_APP_PORT || 8711);
const CONTENT_PORT = Number(process.env.ALK_WIDGETS_CONTENT_PORT || 8712);
const APP = `http://127.0.0.1:${APP_PORT}`;
const RESULTS = join(HERE, "results");
mkdirSync(RESULTS, { recursive: true });
const RUNTIME_CODE_ERROR = "This widget generates code while it runs";

// ------------------------------------------------------------------ helpers

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

async function exec(code) {
  const r = await fetch(`${APP}/exec`, { method: "POST", body: JSON.stringify({ code }) });
  const j = await r.json();
  if (j.errors.length) throw new Error(`kernel: ${j.errors.join("; ")}`);
  return j;
}
const kernelValue = async (expr) => (await exec(`print(repr(${expr}))`)).stdout.trim();

async function until(fn, ms = 15000, label = "condition") {
  const t0 = Date.now();
  let last;
  while (Date.now() - t0 < ms) {
    try {
      last = await fn();
      if (last) return last;
    } catch (e) {
      last = e.message;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}

let lastPage = null;
async function openParent(browser, query = "") {
  const page = (lastPage = await browser.newPage({ viewport: { width: 1000, height: 900 } }));
  await page.goto(`${APP}/${query}`);
  await page.waitForFunction(() => window.walk && window.walk.connected);
  return page;
}
const state = (page) =>
  page.evaluate(() => ({
    frames: window.walk.frames.length,
    errors: window.walk.errors.slice(),
    violations: window.walk.violations.slice(),
    dropped: window.walk.dropped,
    sent: window.walk.sent,
    refused: window.walk.refused.slice(),
    modules: window.walk.modules.slice(),
  }));
const frameAt = (page, i) => page.frameLocator("iframe").nth(i);
async function frames(page, n) {
  await until(async () => (await state(page)).frames >= n, 20000, `${n} frames`);
}
async function noViolations(page, allow = () => false) {
  const st = await state(page);
  const bad = st.violations.filter((v) => !allow(v));
  assert(bad.length === 0, `CSP violations: ${JSON.stringify(bad)}`);
  return st;
}

// ------------------------------------------------------------------ scenarios

const scenarios = {
  async controls(browser) {
    const page = await openParent(browser);
    await exec(`
import ipywidgets as w, zlib, struct
s = w.IntSlider(value=3, min=0, max=10, description='n')
it = w.IntText()
link = w.jslink((s, 'value'), (it, 'value'))
t = w.Text(value='hello', description='text')
b = w.Button(description='Go')
out = w.Output()
clicks = []
def on_click(_):
    clicks.append(1)
    with out:
        print(f'clicked {len(clicks)} n={s.value}')
b.on_click(on_click)
def png(wd, ht):
    raw = b''.join(b'\\x00' + b'\\xff\\x00\\x00' * wd for _ in range(ht))
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))
    return b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', wd, ht, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
img = w.Image(value=png(8, 8), format='png', width=32, height=32)
ui = w.VBox([w.HBox([s, it]), t, b, out, img])
display(ui)
`);
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator(".widget-readout").count()) > 0, 15000, "controls");
    await exec("s.value = 7; t.value = 'from kernel'");
    await until(async () => (await f.locator(".widget-readout").first().textContent()).trim() === "7", 5000, "kernel to frame");
    assert((await f.locator("input[type=number]").inputValue()) === "7", "jslink in the frame");
    await f.locator("input[type=text]").fill("typed in frame");
    await until(async () => (await kernelValue("t.value")) === "'typed in frame'", 5000, "frame to kernel");
    await f.locator("button.jupyter-button").click();
    await until(async () => (await f.locator(".alk-output pre").count()) > 0, 5000, "Output widget");
    assert((await f.locator(".alk-output pre").first().textContent()).includes("clicked 1 n=7"), "Output text");
    const src = await f.locator("img").first().getAttribute("src");
    assert(src.startsWith("data:image/png;base64,"), `Image as a data URL, got ${src.slice(0, 30)}`);
    const natural = await f.locator("img").first().evaluate((el) => el.naturalWidth);
    assert(natural === 8, `Image decoded (${natural})`);
    // A second frame joins from the hub's replays and stays in sync.
    await exec("display(ui)");
    await frames(page, 2);
    const g = frameAt(page, 1);
    await until(async () => (await g.locator(".alk-output pre").count()) > 0, 15000, "late joiner");
    assert((await g.locator("input[type=text]").inputValue()) === "typed in frame", "late joiner state");
    await g.locator("input[type=text]").fill("from frame two");
    await until(async () => (await f.locator("input[type=text]").inputValue()) === "from frame two", 5000, "echo to frame one");
    // A burst ends on the last value (the idle relay releases held changes).
    for (let i = 0; i < 5; i++) await f.locator(".noUi-handle").press("ArrowLeft");
    await until(async () => (await kernelValue("s.value")) === "2", 5000, "kernel after a burst of five");
    await page.screenshot({ path: join(RESULTS, "controls.png"), fullPage: true });
    return noViolations(page);
  },

  async burst_typing(browser) {
    const page = await openParent(browser);
    await exec("import ipywidgets as w\nbt = w.Text(value='', continuous_update=True)\ndisplay(bt)");
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator("input[type=text]").count()) > 0, 15000, "text box");
    await f.locator("input[type=text]").pressSequentially("hello", { delay: 5 });
    await until(async () => (await kernelValue("bt.value")) === "'hello'", 5000, "kernel ends on hello");
    return noViolations(page);
  },

  async kernel_clamp_wins(browser) {
    const page = await openParent(browser);
    await exec("import ipywidgets as w\nbx = w.BoundedIntText(value=1, min=0, max=10)\ndisplay(bx)");
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator("input[type=number]").count()) > 0, 15000, "bounded text");
    await f.locator("input[type=number]").fill("99");
    await f.locator("input[type=number]").press("Enter");
    await until(async () => (await kernelValue("bx.value")) === "10", 5000, "kernel clamps");
    await until(async () => (await f.locator("input[type=number]").inputValue()) === "10", 5000, "frame shows the kernel's value");
    return noViolations(page);
  },

  async readonly(browser) {
    const page = await openParent(browser, "?readonly=1");
    await exec("import ipywidgets as w\nro = w.Text(value='fixed')\ndisplay(ro)");
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator("input[type=text]").count()) > 0, 15000, "text box");
    assert(await f.locator("input[type=text]").isDisabled(), "inputs disabled");
    await f.locator("input[type=text]").evaluate((el) => {
      el.disabled = false;
      el.value = "forced";
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    await new Promise((r) => setTimeout(r, 500));
    const st = await state(page);
    assert(st.sent === 0, `a read-only frame sent ${st.sent} messages`);
    assert((await kernelValue("ro.value")) === "'fixed'", "kernel unchanged");
    return noViolations(page);
  },

  async alkera_ui(browser) {
    const page = await openParent(browser);
    await exec("import alkera.ui as ui\nrows = ui.slider(0, 50, step=5, value=10, label='Rows')\ndisplay(rows)");
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator(".alk-ui-slider input[type=range]").count()) > 0, 15000, "alkera.ui slider");
    assert((await f.locator(".alk-ui-label").textContent()) === "Rows", "label");
    await f.locator("input[type=range]").evaluate((el) => {
      el.value = "35";
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await until(async () => (await kernelValue("rows.value")) === "35", 5000, "slider value in the kernel");
    await exec("rows.value = 999");
    await until(async () => (await f.locator(".alk-ui-readout").textContent()) === "50", 5000, "kernel value, clamped, in the frame");
    // A password reaches only the client that typed it.
    await exec("pw = ui.text(kind='password', label='Token')\ndisplay(pw)");
    await frames(page, 2);
    const other = await openParent(browser);
    await exec("display(pw)");
    await frames(other, 1);
    const mine = frameAt(page, 1);
    const theirs = frameAt(other, 0);
    await until(async () => (await mine.locator("input[type=password]").count()) > 0, 15000, "password box");
    await until(async () => (await theirs.locator("input[type=password]").count()) > 0, 15000, "password box elsewhere");
    await mine.locator("input[type=password]").fill("hunter2");
    await until(async () => (await kernelValue("pw.value")) === "'hunter2'", 5000, "password in the kernel");
    await until(async () => (await theirs.locator("input[type=password]").inputValue()) === "<redacted>", 5000, "redacted elsewhere");
    assert((await mine.locator("input[type=password]").inputValue()) === "hunter2", "the typist keeps their value");
    await other.close();
    return noViolations(page);
  },

  async anywidget(browser) {
    const page = await openParent(browser);
    await exec(`
import anywidget, traitlets
class Counter(anywidget.AnyWidget):
    _esm = """
    function render({ model, el }) {
      const btn = document.createElement("button");
      btn.className = "counter";
      const upd = () => { btn.textContent = "count is " + model.get("value"); };
      btn.addEventListener("click", () => { model.set("value", model.get("value") + 1); model.save_changes(); });
      model.on("change:value", upd);
      upd();
      el.appendChild(btn);
    }
    export default { render };
    """
    _css = ".counter { background: rgb(255, 0, 0); color: white; }"
    value = traitlets.Int(0).tag(sync=True)
c = Counter()
display(c)
`);
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator("button.counter").count()) > 0, 15000, "counter");
    assert((await f.locator("button.counter").evaluate((el) => getComputedStyle(el).backgroundColor)) === "rgb(255, 0, 0)", "_css applied");
    await f.locator("button.counter").click();
    await f.locator("button.counter").click();
    await until(async () => (await kernelValue("c.value")) === "2", 5000, "frame to kernel");
    await exec("c.value = 10");
    await until(async () => (await f.locator("button.counter").textContent()).includes("10"), 5000, "kernel to frame");
    // An anywidget that imports from a CDN fails plainly.
    await exec(`
class Cdn(anywidget.AnyWidget):
    _esm = """
    import confetti from "https://esm.sh/canvas-confetti@1";
    export default { render({ el }) { el.textContent = "loaded"; } };
    """
display(Cdn())
`);
    await frames(page, 2);
    await until(async () => (await state(page)).errors.some((e) => e.message.includes("must bundle its dependencies")), 10000, "CDN import error");
    return noViolations(page);
  },

  async bqplot(browser) {
    const page = await openParent(browser);
    await exec(`
import numpy as np, bqplot as bq
xs, ys = bq.LinearScale(), bq.LinearScale()
line = bq.Lines(x=np.arange(10), y=np.arange(10) ** 2, scales={'x': xs, 'y': ys})
fig = bq.Figure(marks=[line], axes=[bq.Axis(scale=xs), bq.Axis(scale=ys, orientation='vertical')])
display(fig)
`);
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator("g.curve path.line").count()) > 0, 30000, "bqplot svg");
    const before = await f.locator("g.curve path.line").first().getAttribute("d");
    await exec("line.y = np.arange(10)[::-1] ** 2");
    await until(async () => (await f.locator("g.curve path.line").first().getAttribute("d")) !== before, 5000, "redraw");
    const st = await noViolations(page);
    assert(st.modules.includes("bqplot") && st.modules.includes("bqscales"), `environment assets ${st.modules}`);
    await page.screenshot({ path: join(RESULTS, "bqplot.png"), fullPage: true });
    return st;
  },

  async plotly(browser) {
    const page = await openParent(browser);
    await exec(`
import plotly.graph_objects as go
fw = go.FigureWidget(data=[go.Bar(x=['a', 'b', 'c'], y=[1, 3, 2])], layout={'width': 600, 'height': 350})
clicked = []
fw.data[0].on_click(lambda trace, points, state: clicked.append(points.point_inds))
display(fw)
`);
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator("g.point path").count()) === 3, 40000, "plotly bars");
    const heights = async () => f.locator("g.point path").evaluateAll((ps) => ps.map((p) => Math.round(p.getBBox().height)));
    const before = JSON.stringify(await heights());
    await exec("fw.data[0].y = [5, 1, 4]");
    await until(async () => JSON.stringify(await heights()) !== before, 5000, "restyle");
    await f.locator("g.point path").nth(1).click({ force: true });
    await until(async () => (await kernelValue("clicked")) !== "[]", 5000, "click to the kernel");
    const st = await noViolations(page);
    assert(st.modules.some((m) => m.startsWith("alkera-asset:sha256:")), "the 5 MB _esm travelled as an asset reference");
    await page.screenshot({ path: join(RESULTS, "plotly.png"), fullPage: true });
    return st;
  },

  async ipydatagrid(browser) {
    const page = await openParent(browser);
    await exec(`
import pandas as pd
from ipydatagrid import DataGrid, TextRenderer, Expr
df = pd.DataFrame({'a': range(20), 'b': [x * 1.5 for x in range(20)], 'c': list('abcdefghijklmnopqrst')})
grid = DataGrid(df, selection_mode='cell', layout={'height': '250px'},
                renderers={'a': TextRenderer(text_color=Expr("'red' if cell.value > 10 else default_value"))})
display(grid)
`);
    await frames(page, 1);
    const f = frameAt(page, 0);
    await until(async () => (await f.locator("canvas").count()) > 0, 30000, "grid canvas");
    const red = () =>
      f.locator("canvas").first().evaluate((cv) => {
        const d = cv.getContext("2d").getImageData(0, 0, cv.width, cv.height).data;
        let n = 0;
        for (let i = 0; i < d.length; i += 4) if (d[i] > 200 && d[i + 1] < 60 && d[i + 2] < 60) n++;
        return n;
      });
    await until(async () => (await red()) > 0, 10000, "cells drawn by the patched expression (red text)");
    // The grid's header icons load relative to its publicPath: blocked, cosmetic.
    const st = await noViolations(page, (v) => v.directive.startsWith("img-src"));
    assert(st.modules.includes("ipydatagrid"), "environment asset");
    await page.screenshot({ path: join(RESULTS, "ipydatagrid.png"), fullPage: true });
    return st;
  },

  async jscatter_reports_a_clear_error(browser) {
    const page = await openParent(browser);
    await exec(`
import numpy as np, pandas as pd, jscatter
sdf = pd.DataFrame({'x': np.random.rand(200), 'y': np.random.rand(200)})
sc = jscatter.Scatter(data=sdf, x='x', y='y', height=300)
display(sc.widget)
`);
    await frames(page, 1);
    await until(async () => (await state(page)).errors.some((e) => e.message.includes(RUNTIME_CODE_ERROR)), 30000, "the runtime-code error");
    return state(page);
  },
};

// ------------------------------------------------------------------ run

/** A Chromium already on this machine: ALK_CHROMIUM, else the newest headless
 *  shell Playwright installed, else Playwright's own default. */
function chromiumPath() {
  if (process.env.ALK_CHROMIUM) return process.env.ALK_CHROMIUM;
  const cache = join(homedir(), "Library", "Caches", "ms-playwright");
  if (!existsSync(cache)) return undefined;
  const shells = readdirSync(cache).filter((d) => d.startsWith("chromium_headless_shell-")).sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
  for (const dir of shells) {
    for (const sub of readdirSync(join(cache, dir))) {
      const exe = join(cache, dir, sub, "chrome-headless-shell");
      if (existsSync(exe)) return exe;
    }
  }
  return undefined;
}

const server = spawn("uv", ["run", "--no-project", "--with-requirements", join(HERE, "requirements.txt"), "python", join(HERE, "server.py")], {
  env: { ...process.env, ALK_WIDGETS_APP_PORT: String(APP_PORT), ALK_WIDGETS_CONTENT_PORT: String(CONTENT_PORT) },
  stdio: ["ignore", "pipe", "pipe"],
});
let serverLog = "";
server.stdout.on("data", (d) => (serverLog += d));
server.stderr.on("data", (d) => (serverLog += d));
const results = {};
let failed = 0;
try {
  await until(() => serverLog.includes("ready app="), 180000, "server start");
  const browser = await chromium.launch({ headless: true, executablePath: chromiumPath() });
  const wanted = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(scenarios);
  for (const name of wanted) {
    const t0 = Date.now();
    try {
      const st = await scenarios[name](browser);
      results[name] = { ok: true, ms: Date.now() - t0, errors: st?.errors, violations: st?.violations?.length };
      console.log(`ok   ${name} (${Date.now() - t0} ms)`);
    } catch (e) {
      failed++;
      const st = lastPage ? await state(lastPage).catch(() => null) : null;
      results[name] = { ok: false, failure: e.message.split("\n")[0], state: st };
      console.log(`FAIL ${name}: ${e.message.split("\n")[0]} ${JSON.stringify(st).slice(0, 1500)}`);
    }
    for (const ctx of browser.contexts()) for (const pg of ctx.pages()) await pg.close();
    await exec("import ipywidgets as _w; _w.Widget.close_all()").catch(() => {});
  }
  await browser.close();
} finally {
  server.kill("SIGTERM");
  writeFileSync(join(RESULTS, "results.json"), JSON.stringify(results, null, 2));
  if (failed) console.log(serverLog.split("\n").slice(-30).join("\n"));
}
process.exit(failed ? 1 : 0);
