/// <reference types="vite-plugin-svgr/client" />

/* svgr's client types cover the `?react` component import. The two shapes below are
 * this package's own: a bare import for the emitted asset URL, and `?raw` for the
 * file's text bundled into the chunk (the file-icon sprite loads that way). */
declare module "*.svg" {
  const src: string;
  export default src;
}

declare module "*.svg?raw" {
  const markup: string;
  export default markup;
}
