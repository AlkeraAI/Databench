// The portal's theme when nobody has chosen one.
//
// The dark palette is the token layer's `:root` base and light keys on
// `data-alkera-color-scheme="light"`, so the attribute on `<html>` is the one fact the whole
// token layer reads — that is what every assertion here looks at. Two things decide it: the
// pre-hydration boot script, which runs before the first paint, and the React hook, which owns
// it from mount onward. They must agree for every input, or an unconfigured visitor sees one
// theme flash into the other.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { act, render, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useColorScheme } from "@/app/useColorScheme";
import { AuthFrame } from "@/pages/auth/ui/AuthFrame";

const STORAGE_KEY = "alk-color-scheme";

/** The pre-hydration script, read from the artifact the browser actually loads. A test that
 *  inlined a copy of this logic would pass with the real file deleted. */
const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const BOOT_SCRIPT = readFileSync(resolve(webRoot, "public/theme-boot.js"), "utf8");

/** jsdom has no media engine, and no listener plumbing either. This is the OS preference the
 *  page is supposed to follow, plus the `change` event a person flipping their machine emits. */
function osPrefersDark(dark: boolean): (next: boolean) => void {
  let matches = dark;
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  vi.stubGlobal("matchMedia", (query: string): MediaQueryList => {
    const isSchemeQuery = query.includes("prefers-color-scheme: dark");
    const mql = {
      get matches() {
        return isSchemeQuery ? matches : false;
      },
      media: query,
      onchange: null,
      addEventListener: (_type: string, fn: (e: MediaQueryListEvent) => void) => {
        if (isSchemeQuery) listeners.add(fn);
      },
      removeEventListener: (_type: string, fn: (e: MediaQueryListEvent) => void) => {
        listeners.delete(fn);
      },
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    };
    return mql as unknown as MediaQueryList;
  });
  return (next: boolean) => {
    matches = next;
    for (const fn of listeners) fn({ matches: next } as MediaQueryListEvent);
  };
}

function scheme(): string | null {
  return document.documentElement.getAttribute("data-alkera-color-scheme");
}

/** Run the shipped boot script the way a `<script src>` in `<head>` runs it. */
function runBootScript(): void {
  new Function(BOOT_SCRIPT)();
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-alkera-color-scheme");
});

describe("the portal's colour scheme", () => {
  it("follows a dark browser when nobody has chosen", () => {
    osPrefersDark(true);
    renderHook(() => useColorScheme());
    // No attribute IS the dark scheme — it is the `:root` base the tokens define.
    expect(scheme()).toBeNull();
  });

  it("still paints light for a light browser", () => {
    osPrefersDark(false);
    renderHook(() => useColorScheme());
    expect(scheme()).toBe("light");
  });

  it("obeys a stored choice over the browser's preference", () => {
    osPrefersDark(true);
    window.localStorage.setItem(STORAGE_KEY, "light");
    renderHook(() => useColorScheme());
    expect(scheme()).toBe("light");
  });

  it("re-themes a mounted page when the machine switches", () => {
    const switchOs = osPrefersDark(true);
    renderHook(() => useColorScheme());
    expect(scheme()).toBeNull();

    act(() => switchOs(false));
    expect(scheme()).toBe("light");

    act(() => switchOs(true));
    expect(scheme()).toBeNull();
  });

  it("leaves a mounted page alone when the machine switches under an explicit choice", () => {
    const switchOs = osPrefersDark(true);
    window.localStorage.setItem(STORAGE_KEY, "light");
    renderHook(() => useColorScheme());
    expect(scheme()).toBe("light");

    act(() => switchOs(false));
    act(() => switchOs(true));
    expect(scheme()).toBe("light");
  });

  it("persists an explicit pick and stops following the machine", () => {
    const switchOs = osPrefersDark(true);
    const { result } = renderHook(() => useColorScheme());

    act(() => result.current.setScheme("light"));
    expect(scheme()).toBe("light");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("light");

    act(() => switchOs(false));
    act(() => switchOs(true));
    expect(scheme()).toBe("light");
  });

  it("goes back to following the machine when the reader picks Auto", () => {
    const switchOs = osPrefersDark(true);
    window.localStorage.setItem(STORAGE_KEY, "light");
    const { result } = renderHook(() => useColorScheme());
    expect(scheme()).toBe("light");

    act(() => result.current.setScheme("system"));
    expect(scheme()).toBeNull();
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("system");

    act(() => switchOs(false));
    expect(scheme()).toBe("light");
  });

  it("reports Auto as the standing choice when nothing is stored", () => {
    osPrefersDark(true);
    const { result } = renderHook(() => useColorScheme());
    // What the account menu's Light / Dark / Auto radios check against.
    expect(result.current.scheme).toBe("system");
  });

  it("resolves the scheme a control should paint itself as, live", () => {
    const switchOs = osPrefersDark(true);
    const { result } = renderHook(() => useColorScheme());
    expect(result.current.resolved).toBe("dark");

    act(() => switchOs(false));
    expect(result.current.resolved).toBe("light");
  });
});

describe("the signed-out pages", () => {
  it("take the machine's scheme with no session and nothing stored", () => {
    osPrefersDark(false);
    render(
      <AuthFrame vine={false}>
        <p>sign in</p>
      </AuthFrame>,
    );
    expect(scheme()).toBe("light");
  });

  it("follow the machine switching while the sign-in page is open", () => {
    const switchOs = osPrefersDark(true);
    render(
      <AuthFrame vine={false}>
        <p>sign in</p>
      </AuthFrame>,
    );
    expect(scheme()).toBeNull();

    act(() => switchOs(false));
    expect(scheme()).toBe("light");
  });

  it("offer the reader the scheme they are NOT on", () => {
    const switchOs = osPrefersDark(true);
    const { getByRole } = render(
      <AuthFrame vine={false}>
        <p>sign in</p>
      </AuthFrame>,
    );
    expect(getByRole("button", { name: /switch to light theme/i })).toBeTruthy();

    // The control tracked a render-time read of the machine, so a flip left it advertising the
    // theme the page was already painting.
    act(() => switchOs(false));
    expect(getByRole("button", { name: /switch to dark theme/i })).toBeTruthy();
  });
});

describe("the pre-hydration boot", () => {
  it("paints light for a light machine before React ever renders", () => {
    osPrefersDark(false);
    runBootScript();
    expect(scheme()).toBe("light");
  });

  it("paints dark for a dark machine before React ever renders", () => {
    osPrefersDark(true);
    document.documentElement.setAttribute("data-alkera-color-scheme", "light");
    runBootScript();
    expect(scheme()).toBeNull();
  });

  it("honours a stored pick before React ever renders", () => {
    osPrefersDark(true);
    window.localStorage.setItem(STORAGE_KEY, "light");
    runBootScript();
    expect(scheme()).toBe("light");
  });

  it("falls back to the machine when the browser refuses storage", () => {
    osPrefersDark(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(() => runBootScript()).not.toThrow();
    expect(scheme()).toBe("light");
    vi.restoreAllMocks();
  });

  // The whole point of the boot script: whatever it paints, the hook must keep. Any drift
  // between the two — a renamed key, a different default — is a flash from one theme to the
  // other on every load, so pin them against each other for every input.
  const cases: { stored: string | null; osDark: boolean }[] = [
    { stored: null, osDark: true },
    { stored: null, osDark: false },
    { stored: "system", osDark: true },
    { stored: "system", osDark: false },
    { stored: "light", osDark: true },
    { stored: "light", osDark: false },
    { stored: "dark", osDark: true },
    { stored: "dark", osDark: false },
    { stored: "nonsense", osDark: true },
    { stored: "nonsense", osDark: false },
  ];

  it.each(cases)(
    "hands React the same scheme it painted (stored=$stored osDark=$osDark)",
    ({ stored, osDark }) => {
      osPrefersDark(osDark);
      if (stored !== null) window.localStorage.setItem(STORAGE_KEY, stored);

      runBootScript();
      const painted = scheme();

      renderHook(() => useColorScheme());
      expect(scheme()).toBe(painted);
    },
  );
});

describe("the boot script's place in the document", () => {
  const indexHtml = readFileSync(resolve(webRoot, "index.html"), "utf8");
  const scriptTag = /<script\b[^>]*\bsrc="\/theme-boot\.js"[^>]*>/.exec(indexHtml)?.[0];

  it("is loaded by the SPA entry document", () => {
    expect(scriptTag).toBeTruthy();
  });

  it("blocks the first paint rather than deferring past it", () => {
    // `defer`/`async`/`type="module"` all run AFTER the document is parsed, which is after the
    // browser is free to paint the body ground — exactly the flash this script prevents.
    expect(scriptTag).not.toMatch(/\bdefer\b|\basync\b|type="module"/);
  });

  it("runs before the stylesheet that paints the ground", () => {
    const script = indexHtml.indexOf('src="/theme-boot.js"');
    const body = indexHtml.indexOf("<body");
    expect(script).toBeGreaterThan(-1);
    expect(script).toBeLessThan(body);
  });
});
