// A `.alknb.py` file opens as a notebook, with its source a toggle away;
// every other Python file stays a text file.

import { describe, expect, it } from "vitest";

import { fileViewerFor, openingView, counterpart } from "@/pages/workspace/chat/workspace/fileViewers";
import { isNotebookName } from "@/pages/workspace/chat/workspace/notebook/register";

const facts = (name: string) => ({ name, mime: "text/x-python", size: 10, synced: true }) as Parameters<typeof fileViewerFor>[0];

describe("the notebook viewer", () => {
  it.each(["analysis.alknb.py", "Weekly.ALKNB.PY"])("opens %s in the notebook view", (name) => {
    const viewer = fileViewerFor(facts(name));
    expect(viewer.id).toBe("notebook");
    const opening = openingView(viewer);
    expect(opening.label).toBe("Notebook");
    expect(opening.draw.kind).toBe("custom");
    const source = counterpart(viewer, opening);
    expect(source?.label).toBe("Source");
    expect(source?.draw.kind).toBe("live-editor");
  });

  it.each(["script.py", "alknb.py", "notes.alknb.txt"])("leaves %s alone", (name) => {
    expect(isNotebookName(name)).toBe(false);
    expect(fileViewerFor(facts(name)).id).not.toBe("notebook");
  });
});
