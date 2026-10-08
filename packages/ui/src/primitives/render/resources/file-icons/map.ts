/* Ported from vendor/opencode/packages/ui/src/components/file-icon.tsx (the file half of its
 * ICON_MAPS and chooseIconName). That vendor file is the source of truth: vendor/opencode is not a
 * pnpm workspace member, so the mapping is copied rather than imported. When an icon is missing or
 * wrong, re-port from the vendor file instead of hand-extending this one. */

import type { IconName } from "./types";

const fileNames: Record<string, IconName> = {
  // A directory is not a file name, but it reaches the map by the same door:
  // `folder` is the hint a tree row hands us for a node that has children.
  folder: "Folder",
  // Documentation files
  "readme.md": "Readme",
  "changelog.md": "Changelog",
  "contributing.md": "Contributing",
  "conduct.md": "Conduct",
  license: "Certificate",
  authors: "Authors",
  credits: "Credits",
  install: "Installation",

  // Node.js files
  "package.json": "Nodejs",
  "package-lock.json": "Nodejs",
  "yarn.lock": "Yarn",
  "pnpm-lock.yaml": "Pnpm",
  "bun.lock": "Bun",
  "bun.lockb": "Bun",
  "bunfig.toml": "Bun",
  ".nvmrc": "Nodejs",
  ".node-version": "Nodejs",

  // Docker files
  dockerfile: "Docker",
  "docker-compose.yml": "Docker",
  "docker-compose.yaml": "Docker",
  ".dockerignore": "Docker",

  // Config files
  "jest.config.js": "Jest",
  "jest.config.ts": "Jest",
  "jest.config.mjs": "Jest",
  "vitest.config.js": "Vitest",
  "vitest.config.ts": "Vitest",
  "tailwind.config.js": "Tailwindcss",
  "tailwind.config.ts": "Tailwindcss",
  "turbo.json": "Turborepo",
  "tsconfig.json": "Tsconfig",
  "jsconfig.json": "Jsconfig",
  ".eslintrc": "Eslint",
  ".eslintrc.js": "Eslint",
  ".eslintrc.json": "Eslint",
  ".prettierrc": "Prettier",
  ".prettierrc.js": "Prettier",
  ".prettierrc.json": "Prettier",
  "vite.config.js": "Vite",
  "vite.config.ts": "Vite",
  "webpack.config.js": "Webpack",
  "rollup.config.js": "Rollup",
  "astro.config.mjs": "AstroConfig",
  "astro.config.js": "AstroConfig",
  "next.config.js": "Next",
  "next.config.mjs": "Next",
  "nuxt.config.js": "Nuxt",
  "nuxt.config.ts": "Nuxt",
  "svelte.config.js": "Svelte",
  "gatsby-config.js": "Gatsby",
  "remix.config.js": "Remix",
  "prisma.schema": "Prisma",
  ".gitignore": "Git",
  ".gitattributes": "Git",
  makefile: "Makefile",
  cmake: "Cmake",
  "cargo.toml": "Rust",
  "go.mod": "GoMod",
  "go.sum": "GoMod",
  "requirements.txt": "Python",
  "pyproject.toml": "Python",
  pipfile: "Python",
  "poetry.lock": "Poetry",
  gemfile: "Gemfile",
  rakefile: "Ruby",
  "composer.json": "Php",
  "build.gradle": "Gradle",
  "pom.xml": "Maven",
  "deno.json": "Deno",
  "deno.jsonc": "Deno",
  "vercel.json": "Vercel",
  "netlify.toml": "Netlify",
  ".env": "Tune",
  ".env.local": "Tune",
  ".env.development": "Tune",
  ".env.production": "Tune",
  ".env.example": "Tune",
  ".editorconfig": "Editorconfig",
  "robots.txt": "Robots",
  "favicon.ico": "Favicon",
  browserlist: "Browserlist",
  ".babelrc": "Babel",
  "babel.config.js": "Babel",
  "gulpfile.js": "Gulp",
  "gruntfile.js": "Grunt",
  "capacitor.config.json": "Capacitor",
  "ionic.config.json": "Ionic",
  "angular.json": "Angular",
  ".storybook": "Storybook",
  "storybook.config.js": "Storybook",
  "cypress.config.js": "Cypress",
  "playwright.config.js": "Playwright",
  "puppeteer.config.js": "Puppeteer",
  "wrangler.toml": "Wrangler",
  "firebase.json": "Firebase",
  supabase: "Supabase",
  terraform: "Terraform",
  kubernetes: "Kubernetes",
  ".gitpod.yml": "Gitpod",
  ".devcontainer": "Vscode",
  "travis.yml": "Travis",
  "appveyor.yml": "Appveyor",
  ".circleci": "Circleci",
  "renovate.json": "Renovate",
  "dependabot.yml": "Dependabot",
  "lerna.json": "Lerna",
  "nx.json": "Nx",
};

const fileExtensions: Record<string, IconName> = {
  // Test files
  "spec.ts": "TestTs",
  "test.ts": "TestTs",
  "spec.tsx": "TestJsx",
  "test.tsx": "TestJsx",
  "spec.js": "TestJs",
  "test.js": "TestJs",
  "spec.jsx": "TestJsx",
  "test.jsx": "TestJsx",

  // JavaScript/TypeScript
  "js.map": "JavascriptMap",
  "d.ts": "TypescriptDef",
  ts: "Typescript",
  tsx: "React_ts",
  js: "Javascript",
  jsx: "React",
  mjs: "Javascript",
  cjs: "Javascript",

  // Web languages
  html: "Html",
  htm: "Html",
  css: "Css",
  scss: "Sass",
  sass: "Sass",
  less: "Less",
  styl: "Stylus",

  // Data formats
  json: "Json",
  xml: "Xml",
  yml: "Yaml",
  yaml: "Yaml",
  toml: "Toml",
  hjson: "Hjson",

  // Documentation
  md: "Markdown",
  mdx: "Mdx",
  tex: "Tex",

  // Programming languages
  // An Alkera notebook is a Python file underneath; the longest suffix wins,
  // so it is matched before "py".
  "alknb.py": "AlkeraNotebook",
  py: "Python",
  pyx: "Python",
  pyw: "Python",
  rs: "Rust",
  go: "Go",
  java: "Java",
  kt: "Kotlin",
  scala: "Scala",
  php: "Php",
  rb: "Ruby",
  cs: "Csharp",
  vb: "Visualstudio",
  cpp: "Cpp",
  cc: "Cpp",
  cxx: "Cpp",
  c: "C",
  h: "H",
  hpp: "Hpp",
  swift: "Swift",
  m: "ObjectiveC",
  mm: "ObjectiveCpp",
  dart: "Dart",
  lua: "Lua",
  pl: "Perl",
  r: "R",
  jl: "Julia",
  hs: "Haskell",
  elm: "Elm",
  ml: "Ocaml",
  clj: "Clojure",
  cljs: "Clojure",
  erl: "Erlang",
  ex: "Elixir",
  exs: "Elixir",
  nim: "Nim",
  zig: "Zig",
  v: "Vlang",
  odin: "Odin",
  gleam: "Gleam",
  grain: "Grain",
  roc: "Rocket",
  fs: "Fsharp",

  // Shell scripts
  sh: "Console",
  bash: "Console",
  zsh: "Console",
  fish: "Console",
  ps1: "Powershell",

  // Config/build files
  cfg: "Settings",
  ini: "Settings",
  conf: "Settings",
  properties: "Settings",

  // Media files
  svg: "Svg",
  png: "Image",
  jpg: "Image",
  jpeg: "Image",
  gif: "Image",
  webp: "Image",
  bmp: "Image",
  ico: "Favicon",
  mp4: "Video",
  mov: "Video",
  avi: "Video",
  webm: "Video",
  mp3: "Audio",
  wav: "Audio",
  flac: "Audio",

  // Archive files
  zip: "Zip",
  tar: "Zip",
  gz: "Zip",
  rar: "Zip",
  "7z": "Zip",

  // Document files
  pdf: "Pdf",
  doc: "Word",
  docx: "Word",
  ppt: "Powerpoint",
  pptx: "Powerpoint",
  xls: "Document",
  xlsx: "Document",

  // Database files
  sql: "Database",
  db: "Database",
  sqlite: "Database",

  // Other
  env: "Tune",
  log: "Log",
  lock: "Lock",
  key: "Key",
  pem: "Certificate",
  crt: "Certificate",
  proto: "Proto",
  graphql: "Graphql",
  gql: "Graphql",
  wasm: "Webassembly",
  dockerfile: "Docker",
};

/* Fence tokens a markdown block can carry that name a language rather than an extension. Only the
 * spellings the extension table misses belong here. */
const languageAliases: Record<string, string> = {
  python: "py",
  typescript: "ts",
  javascript: "js",
  markdown: "md",
  shell: "sh",
  golang: "go",
  rust: "rs",
  ruby: "rb",
  csharp: "cs",
  kotlin: "kt",
};

/** The sprite symbol for a file with no recognized name or extension. */
export const DEFAULT_FILE_ICON: IconName = "Document";

const basenameOf = (p: string) => p.split("\\").join("/").split("/").filter(Boolean).pop() ?? "";

const dottedSuffixesDesc = (name: string) => {
  const n = name.toLowerCase();
  const idxs: number[] = [];
  for (let i = 0; i < n.length; i++) if (n[i] === ".") idxs.push(i);
  const out = new Set<string>();
  out.add(n); // allow exact whole-name "extensions" like "dockerfile"
  for (const i of idxs) if (i + 1 < n.length) out.add(n.slice(i + 1));
  return Array.from(out).sort((a, b) => b.length - a.length); // longest first
};

/**
 * The sprite symbol for a path, a bare file name, or a bare language token such as `sql` or
 * `python`. Null when nothing matches, which leaves the caller free to pick its own fallback.
 */
export function iconNameForFile(source: string): IconName | null {
  const base = basenameOf(source).toLowerCase();
  if (!base) return null;

  const byName = fileNames[base];
  if (byName) return byName;

  for (const ext of dottedSuffixesDesc(base)) {
    const icon = fileExtensions[ext];
    if (icon) return icon;
  }

  const aliased = languageAliases[base];
  if (aliased) {
    const icon = fileExtensions[aliased];
    if (icon) return icon;
  }

  return null;
}
