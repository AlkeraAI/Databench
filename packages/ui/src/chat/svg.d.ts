/* The canonical package imports SVG components directly, so every ambient
 * module shape packages/ui declares must be visible here too -- including the
 * `?react` component import its connector marks use. */
declare module "*.svg?react" {
  import type { FunctionComponent, SVGProps } from "react";
  const ReactComponent: FunctionComponent<SVGProps<SVGSVGElement> & { title?: string }>;
  export default ReactComponent;
}

/* Bundlers resolve a bare `.svg` import to the emitted asset URL. */
declare module "*.svg" {
  const src: string;
  export default src;
}

/* A `?raw` import resolves to the file's text, bundled into the chunk itself. */
declare module "*.svg?raw" {
  const markup: string;
  export default markup;
}
