import { defineConfig, devices, type Project } from "@playwright/test";

const port = Number(process.env.ALKERA_WEB_E2E_PORT ?? 5173);
const baseURL = `http://localhost:${port}`;

/** The live stack the `live-stack` project drives, when the operator opts in.
 *  Its presence is the whole opt-in: without it the project is not declared at
 *  all, and the Vite dev server IS booted as usual. With it, no dev server is
 *  started — the stack already serves the portal, and a second one would be a
 *  different build. */
const livePortalURL = process.env.LIVE_PORTAL_URL;

/** The opt-in project that drives an already-running portal, API and machine.
 *
 *  It boots nothing and owns no fixtures: the URLs, the credentials, the chat
 *  and the questions all arrive as env vars (see `e2e/live/_live.ts`). */
function liveProject(url: string): Project {
  return {
    name: "live-stack",
    testMatch: /live\/.*\.spec\.ts/,
    // A live agent turn is minutes, not seconds: the per-turn waits inside the
    // specs carry their own 120 s budget, and this only has to be larger than
    // the sum of them so the harness never truncates a turn mid-stream.
    timeout: 600_000,
    use: {
      ...devices["Desktop Chrome"],
      baseURL: url,
      colorScheme: "dark",
      viewport: { width: 1440, height: 900 },
      acceptDownloads: true,
      // A selector that no longer matches must fail in seconds, not sit on the
      // turn-sized test timeout: the point of the suite is a readable failure,
      // and a 10-minute hang reads as nothing at all.
      actionTimeout: 30_000,
      navigationTimeout: 60_000,
    },
  };
}

/**
 * Playwright config for the Alkera IDE webview surfaces. Boots the Vite
 * dev server, drives `vscode.html` at the standard responsive breakpoints, captures PNGs to `e2e/__screenshots__/`.
 *
 * The `live-stack` project is the exception: it drives an ALREADY-RUNNING
 * portal + API + machine over the network. It is excluded from the default run
 * by both halves — the mock project ignores `e2e/live/`, and `live-stack` is
 * only declared when `LIVE_PORTAL_URL` names a stack to drive.
 */
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./e2e/__results__",
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL,
    headless: true,
    colorScheme: "dark",
  },
  projects: [
    {
      name: "chromium-dark",
      testIgnore: /live\//,
      use: { ...devices["Desktop Chrome"], colorScheme: "dark" },
    },
    ...(livePortalURL ? [liveProject(livePortalURL)] : []),
  ],
  // The live project drives a stack that is already up; every other project
  // needs the dev server this boots.
  webServer: livePortalURL
    ? undefined
    : {
        command: `pnpm dev --host 127.0.0.1 --port ${port}`,
        url: `${baseURL}/vscode.html`,
        reuseExistingServer: !process.env.CI && !process.env.ALKERA_WEB_E2E_PORT,
        timeout: 60_000,
      },
});
