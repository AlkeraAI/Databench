// A failed upload has to say WHY on the tray row.
//
// The Files routes answer a flat `{code, message}` envelope, so the row's
// `ApiError` carries the server's own sentence ("part checksum mismatch …").
// Showing the bare code — or worse, the generic "Failed" — throws away the one
// fact that tells the person whether to retry or to stop.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { UploadReadError } from "@/api/filesUpload";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import type { UploadRow } from "@/pages/workspace/files/useUploads";

function tray(rows: readonly UploadRow[], onRetry?: (uploadId: string) => void) {
  return render(
    <UploadTray
      rows={rows}
      {...(onRetry ? { onRetry } : {})}
      skippedSidecars={0}
      identicalCopies={0}
      alreadyInFiles={0}
      finished={0}
      refusal={null}
      conflicts={[]}
      resumable={[]}
      onPause={() => {}}
      onResume={() => {}}
      onCancel={() => {}}
      onAnswerConflict={() => {}}
    />,
  );
}

function failedRow(error: ApiError | undefined): UploadRow {
  return {
    uploadId: "u1",
    name: "report.csv",
    sent: 1024,
    total: 4096,
    partsDone: 1,
    partsTotal: 4,
    state: "failed",
    ...(error ? { error } : {}),
  };
}

describe("the upload tray's failure row", () => {
  it("shows the server's sentence for a 422 part-checksum mismatch", () => {
    const error = new ApiError(422, {
      code: "files.part_checksum_mismatch",
      message: "part checksum mismatch: declared 0xaa, computed 0xbb",
    });
    tray([failedRow(error)]);

    expect(
      screen.getByText("part checksum mismatch: declared 0xaa, computed 0xbb"),
    ).toBeInTheDocument();
  });

  it("falls back to a plain word when the refusal carried no sentence", () => {
    tray([failedRow(undefined)]);
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("shows a file it could not read by name, with the way back", () => {
    // The client's whole retry policy for an unreadable file rests on this row:
    // the sentence is where the person learns to plug the drive back in, and
    // Retry is the only way to act on it. Neither is optional.
    tray([failedRow(new UploadReadError("report.csv"))], () => {});

    expect(
      screen.getByText(
        "report.csv could not be read. It may have been moved, deleted, or on a drive that is no longer connected.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry report.csv" })).toBeInTheDocument();
  });
});
