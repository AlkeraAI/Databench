import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UploadTray } from "@/pages/workspace/files/UploadTray";
import {
  traySettled,
  type BatchProgress,
  type ConflictPrompt,
  type UploadRow,
} from "@/pages/workspace/files/useUploads";

// A re-upload onto a name the folder already holds is decided inside a QUEUED
// commit, which on a real deployment takes tens of seconds. For all of it the
// row sits in `completing`. A bare "Finishing" there reads as an upload that is
// nearly done, then a question arrives twenty seconds later with nothing
// connecting the two, and the upload looks as if it vanished.
//
// Two things must hold while a question is open: the row says it is the one
// being asked about, and nothing takes it off the screen.

function row(over: Partial<UploadRow> = {}): UploadRow {
  return {
    uploadId: "up_1",
    name: "notes.md",
    sent: 120,
    total: 120,
    partsDone: 1,
    partsTotal: 1,
    state: "completing",
    ...over,
  };
}

function prompt(over: Partial<ConflictPrompt> = {}): ConflictPrompt {
  return { uploadId: "up_1", name: "notes.md", parentId: "nd_home", ...over };
}

function batch(over: Partial<BatchProgress> = {}): BatchProgress {
  return { total: 1, done: 1, failed: 0, current: null, stopped: null, ...over };
}

function tray(props: { rows: UploadRow[]; conflicts: ConflictPrompt[] }) {
  return render(
    <UploadTray
      rows={props.rows}
      skippedSidecars={0}
      identicalCopies={0}
      alreadyInFiles={0}
      finished={0}
      refusal={null}
      batch={null}
      conflicts={props.conflicts}
      resumable={[]}
      onPause={() => undefined}
      onResume={() => undefined}
      onCancel={() => undefined}
      onAnswerConflict={() => undefined}
    />,
  );
}

describe("a row whose upload is waiting for an answer", () => {
  it("says it is waiting rather than that it is finishing", () => {
    tray({ rows: [row()], conflicts: [prompt()] });

    // "Finishing" is what a commit nobody has to do anything about says. This
    // one is waiting on the person, and the question is right above it.
    expect(screen.queryByText("Finishing")).not.toBeInTheDocument();
    expect(screen.getByText("Waiting for your answer")).toBeInTheDocument();
  });

  it("leaves every other row saying what it is doing", () => {
    // The question is about `up_1`; the second file's commit is nobody's to
    // answer and must not borrow the sentence.
    tray({
      rows: [row(), row({ uploadId: "up_2", name: "other.md" })],
      conflicts: [prompt()],
    });

    expect(screen.getByText("Waiting for your answer")).toBeInTheDocument();
    expect(screen.getByText("Finishing")).toBeInTheDocument();
  });

  it("says Finishing again once the question has been answered", () => {
    tray({ rows: [row()], conflicts: [] });

    expect(screen.getByText("Finishing")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for your answer")).not.toBeInTheDocument();
  });

  it("does not relabel a row that already settled under a question about another", () => {
    // A Skip answered elsewhere leaves a cancelled row; a stale prompt must not
    // make a settled row claim it is waiting.
    tray({ rows: [row({ state: "done" })], conflicts: [prompt()] });

    expect(screen.getByText("Uploaded")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for your answer")).not.toBeInTheDocument();
  });
});

describe("whether the tray may clear itself", () => {
  const done = row({ state: "done" });

  it("does once every row finished cleanly and the drop is over", () => {
    expect(traySettled([done], batch(), [])).toBe(true);
  });

  it("does not while a question is unanswered", () => {
    // The linger the previous file armed would otherwise carry the question's
    // own row off the screen while it is still being asked.
    expect(traySettled([done], batch(), [prompt()])).toBe(false);
  });

  it("does not while a drop it accepted is still running", () => {
    // The linger belongs to the batch that armed it. Firing it over a later
    // drop erases that drop's progress and leaves an empty tray over an upload
    // that is still going.
    expect(traySettled([done], batch({ total: 4, done: 1 }), [])).toBe(false);
  });

  it("does not while a row is still moving, or has failed", () => {
    expect(traySettled([row()], batch(), [])).toBe(false);
    expect(traySettled([row({ state: "failed" })], batch(), [])).toBe(false);
    expect(traySettled([done, row({ uploadId: "up_2", state: "failed" })], batch(), [])).toBe(false);
  });

  it("does not when there is nothing on it to read", () => {
    expect(traySettled([], null, [])).toBe(false);
  });

  it("does when no batch is recorded at all", () => {
    // A resumed session produces a row without a drop behind it.
    expect(traySettled([done], null, [])).toBe(true);
  });
});
