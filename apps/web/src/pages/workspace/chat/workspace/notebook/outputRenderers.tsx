// What a notebook's outputs are drawn with, wherever one is shown: the
// editor tab and the read-only preview. Importing this module registers them.

import { registerChartEngine, registerNotebookRenderers } from "@alkera/notebook-ui";
import { AlkeraChart } from "@alkera/ui";

registerNotebookRenderers();
// Notebook charts draw through the platform's chart renderer, the one every
// other surface uses for the Alkera chart profile.
// The chart reads the app's chart tokens off the page when it mounts, so it is
// keyed on the theme: a theme change draws it again in the new palette.
registerChartEngine(({ spec, theme }) => <AlkeraChart key={theme} spec={spec} />);
