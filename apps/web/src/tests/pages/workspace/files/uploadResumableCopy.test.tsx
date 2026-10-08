import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ResumableUpload } from "@/api/filesUpload";
import { resumableLine, UploadTray } from "@/pages/workspace/files/UploadTray";

// The line the tray greets a returning reader with.
//
// It names the files, so the reader can tell which of a batch survived, and it does
// not prescribe a drop, since most people start an upload from the Upload files button. The rendered sentence is asserted, not the pieces, because
// the sentence is what a person reads.

function resumable(name: string): ResumableUpload {
  return { uploadId: `up_${name}`, name, size: 1024, lastModified: 0, parentId: "nd_home", partSize: 512 };
}

function tray(names: string[]) {
  return render(
    <UploadTray
      rows={[]}
      skippedSidecars={0}
      identicalCopies={0}
      alreadyInFiles={0}
      finished={0}
      refusal={null}
      conflicts={[]}
      resumable={names.map(resumable)}
      onPause={() => undefined}
      onResume={() => undefined}
      onCancel={() => undefined}
      onAnswerConflict={() => undefined}
    />,
  );
}

describe("the upload tray's returning-visit line", () => {
  it("names the one file, and both ways of handing it over", () => {
    tray(["quarterly-report.csv"]);

    expect(
      screen.getByText(
        "quarterly-report.csv from your last visit can continue. Drop or choose the same file.",
      ),
    ).toBeInTheDocument();
  });

  it("names three files, and pluralises the action", () => {
    tray(["a.txt", "b.txt", "c.txt"]);

    expect(
      screen.getByText(
        "a.txt, b.txt and c.txt from your last visit can continue. Drop or choose the same files.",
      ),
    ).toBeInTheDocument();
  });

  it("counts the rest past three, so the line stays a sentence", () => {
    expect(resumableLine(["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"])).toBe(
      "a.txt, b.txt, c.txt and 2 more from your last visit can continue. Drop or choose the same files.",
    );
  });

  it("says nothing at all when nothing carried over", () => {
    const { container } = tray([]);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/from your last visit/)).toBeNull();
  });

  it("keeps the old sentence out of the tray", () => {
    tray(["a.txt", "b.txt"]);

    // Neither an unnamed count nor an instruction to repeat a gesture the reader
    // may never have made.
    expect(screen.queryByText(/drop the same file/)).toBeNull();
    expect(screen.queryByText(/^\d+ uploads? from your last visit/)).toBeNull();
    expect(screen.getByText(/^a\.txt and b\.txt from your last visit/)).toBeInTheDocument();
  });
});
