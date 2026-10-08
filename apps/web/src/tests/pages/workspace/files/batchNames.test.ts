import { describe, expect, it, vi } from "vitest";

import { planNames } from "@/pages/workspace/files/batchNames";
import type { DroppedFile } from "@/pages/workspace/files/dropHandlers";

// The planner is the whole of the drop's answer to "two files, one name": what
// it returns is what gets sent, so every case here is a case where sending the
// wrong thing loses bytes or uploads them twice.

function dropped(directory: string, name: string, body: string): DroppedFile {
  return {
    file: new File([body], name, { lastModified: 1 }),
    relativePath: directory ? `${directory}/${name}` : name,
    directory,
  };
}

/** A digest of the bytes, which is what the real one is. */
const byBody: (file: File) => Promise<string> = async (file) => {
  const text = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("unreadable"));
    reader.readAsText(file);
  });
  return `digest:${text}`;
};

describe("planning a drop's own name collisions", () => {
  it("leaves a drop with no collisions alone and reads nothing", async () => {
    const read = vi.fn(byBody);
    const plan = await planNames(
      [dropped("", "a.txt", "one"), dropped("", "b.txt", "two")],
      read,
    );
    expect(plan.identicalCopies).toBe(0);
    expect(plan.files.map((entry) => entry.conflictBehavior)).toEqual(["fail", "fail"]);
    // Hashing a drop that has no collision would charge every upload for a
    // problem it does not have.
    expect(read).not.toHaveBeenCalled();
  });

  it("sends the second of two different bodies under rename", async () => {
    const plan = await planNames(
      [dropped("", "collide.txt", "dupA"), dropped("", "collide.txt", "dupB!")],
      byBody,
    );
    expect(plan.identicalCopies).toBe(0);
    expect(plan.files).toHaveLength(2);
    expect(plan.files.map((entry) => entry.conflictBehavior)).toEqual(["fail", "rename"]);
  });

  it("folds a copy away only when the bytes are the same", async () => {
    const plan = await planNames(
      [dropped("", "n.md", "same"), dropped("", "n.md", "same"), dropped("", "n.md", "diff")],
      byBody,
    );
    expect(plan.identicalCopies).toBe(1);
    expect(plan.files).toHaveLength(2);
    expect(plan.files.map((entry) => entry.conflictBehavior)).toEqual(["fail", "rename"]);
  });

  it("does not hash two files that cannot hold the same bytes", async () => {
    const read = vi.fn(byBody);
    // Same name, different lengths: the cheap fact already answers it.
    await planNames([dropped("", "n.md", "short"), dropped("", "n.md", "a longer body")], read);
    expect(read).not.toHaveBeenCalled();
  });

  it("keeps a case-only difference as two names, because the folder does", async () => {
    const plan = await planNames([dropped("", "N.txt", "up"), dropped("", "n.txt", "lo")], byBody);
    expect(plan.identicalCopies).toBe(0);
    expect(plan.files.map((entry) => entry.conflictBehavior)).toEqual(["fail", "fail"]);
  });

  it("keeps one name per folder rather than one name per drop", async () => {
    const plan = await planNames(
      [dropped("a", "n.txt", "same"), dropped("b", "n.txt", "same")],
      byBody,
    );
    expect(plan.identicalCopies).toBe(0);
    expect(plan.files.map((entry) => entry.conflictBehavior)).toEqual(["fail", "fail"]);
  });

  it("uploads a file it could not read rather than calling it a copy", async () => {
    const plan = await planNames(
      [dropped("", "n.txt", "same"), dropped("", "n.txt", "same")],
      () => Promise.reject(new Error("the drive went away")),
    );
    expect(plan.identicalCopies).toBe(0);
    expect(plan.files.map((entry) => entry.conflictBehavior)).toEqual(["fail", "rename"]);
  });
});
