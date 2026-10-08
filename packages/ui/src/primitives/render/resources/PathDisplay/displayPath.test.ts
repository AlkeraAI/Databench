import { describe, expect, it } from "vitest";

import { displayPath } from "./displayPath";

const ROOT = "/home/dev/warehouse";
const HOME = "/home/dev";

describe("displayPath", () => {
  // The terminal client's display rule, case for case, so the two cannot drift.
  it.each([
    ["inside the root → relative", `${ROOT}/models/staging/x.sql`, ROOT, HOME, "models/staging/x.sql"],
    ["one level under the root", `${ROOT}/models`, ROOT, HOME, "models"],
    ["the root itself → ~/<root-name>", ROOT, ROOT, HOME, "~/warehouse"],
    ["trailing slash on the path", `${ROOT}/`, ROOT, HOME, "~/warehouse"],
    ["trailing slash on the root", ROOT, `${ROOT}/`, HOME, "~/warehouse"],
    // The single most important guard: a STRING prefix that is not a path
    // SEGMENT prefix — `/a/warehouse-2` is a sibling of root `/a/warehouse`.
    ["prefix-but-not-a-segment stays put", "/a/warehouse-2", "/a/warehouse", null, "/a/warehouse-2"],
    ["same-prefix real subdir is relative", "/a/warehouse/models", "/a/warehouse", null, "models"],
    ["a parent of the root stays absolute", "/a", "/a/b", null, "/a"],
    ["under home, outside root → ~/<rest>", `${HOME}/scratch/tmp`, ROOT, HOME, "~/scratch/tmp"],
    ["home itself → ~", HOME, ROOT, HOME, "~"],
    // A sibling of home sharing its string prefix is NOT under home.
    ["home prefix-sibling stays absolute", "/home/developer/x", "/srv/root", HOME, "/home/developer/x"],
    ["outside root and home → verbatim", "/var/log", ROOT, HOME, "/var/log"],
    ["no root: home abbreviation still fires", `${HOME}/warehouse`, null, HOME, "~/warehouse"],
    ["no root, no home → identity", "/srv/data", null, null, "/srv/data"],
    // Root identity outranks the home branch when they coincide.
    ["path == home == root → root form wins", HOME, HOME, HOME, "~/dev"],
    // POSIX comparisons stay case-sensitive.
    ["case-sensitive compare", "/HOME/DEV/warehouse", ROOT, HOME, "/HOME/DEV/warehouse"],
    ["relative input resolves against the root", "models/x.sql", ROOT, HOME, "models/x.sql"],
    ["relative input with .. collapses", "models/../lib/y.py", ROOT, HOME, "lib/y.py"],
    // An escaping relative input lands on its clean absolute form, never `../..`.
    ["escaping relative input → outside form", "../../other/y.py", ROOT, HOME, "/home/other/y.py"],
    ["dot input with a root → the root itself", ".", ROOT, HOME, "~/warehouse"],
    ["relative input without a root → verbatim", "models/x.sql", null, null, "models/x.sql"],
    ["empty in / empty out", "", ROOT, HOME, ""],
    // Pathological bases are ignored rather than relativizing everything.
    ['root "/" is treated as no root', "/etc/passwd", "/", null, "/etc/passwd"],
    ['root "" is treated as no root', "/etc/passwd", "", null, "/etc/passwd"],
    ['home "/" is treated as no home', "/etc/passwd", null, "/", "/etc/passwd"],
    ["a relative root is unusable", "/etc/passwd", "relative/root", null, "/etc/passwd"],
    // URLs and scheme-carrying strings pass through untouched.
    ["a URL is identity", "https://github.com/acme/repo", ROOT, HOME, "https://github.com/acme/repo"],
    // Backslash inputs normalize to forward slashes for display; drive letters
    // compare case-insensitively (Windows fsPaths arrive as `C:\…` or `c:\…`).
    ["windows path under a windows root", "C:\\work\\repo\\src\\a.ts", "C:\\work\\repo", null, "src/a.ts"],
    ["drive-letter case mismatch still matches", "c:\\work\\repo\\src\\a.ts", "C:/work/repo", null, "src/a.ts"],
    // NTFS is case-insensitive: segment-casing differences on a drive path
    // still relativize, and the remainder keeps the path's own casing.
    ["drive segments fold case against the root", "C:\\Work\\Repo\\Src\\A.ts", "c:\\work\\repo", null, "Src/A.ts"],
    ["drive segments fold case against home", "C:\\USERS\\Dev\\notes.md", null, "c:\\users\\dev", "~/notes.md"],
    ["windows root itself", "C:\\work\\repo", "C:\\work\\repo", null, "~/repo"],
    ["windows path under home", "C:\\Users\\dev\\notes.md", null, "C:\\Users\\dev", "~/notes.md"],
    ["outside a windows root → normalized absolute", "D:\\data\\x.csv", "C:\\work\\repo", null, "D:/data/x.csv"],
    ['drive root "C:/" is treated as no root', "C:\\data\\x.csv", "C:\\", null, "C:/data/x.csv"],
    // A path outside the root comes back absolute, never a chain of parent hops.
    ["outside the root never escapes with ..", "/home/dev/other/y.py", ROOT, null, "/home/dev/other/y.py"],
  ])("%s", (_label, path, root, home, expected) => {
    expect(displayPath(path, { root, home })).toBe(expected);
  });
});
