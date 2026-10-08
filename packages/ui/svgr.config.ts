import jsx from "@svgr/plugin-jsx";
import svgo from "@svgr/plugin-svgo";
import svgr from "vite-plugin-svgr";

// The `?react` SVG importer for every build that compiles @alkera/ui sources, meaning the
// web app's Vite and both vitest configs. The svgo pass exists for correctness. The vendor
// connector marks are Illustrator exports whose <style> classes (.st0) and gradient ids
// collide once many marks inline into one document. inlineStyles moves every rule onto its
// element and prefixIds namespaces ids per file. Plugin functions rather than names so
// pnpm resolves them from this package, where they are declared.
export function alkeraSvgr() {
  return svgr({
    include: /\.svg\?(.*&)?react/,
    svgrOptions: {
      plugins: [svgo, jsx],
      svgoConfig: {
        plugins: [
          {
            name: "preset-default",
            params: {
              overrides: {
                // A mark must scale to whatever box renders it.
                removeViewBox: false,
                // The default inlines only single-use classes and would leave the
                // shared .st0 rules behind in a <style> block.
                inlineStyles: { onlyMatchedOnce: false },
              },
            },
          },
          "prefixIds",
        ],
      },
    },
  });
}
