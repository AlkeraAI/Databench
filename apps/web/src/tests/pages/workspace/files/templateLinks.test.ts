import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { linkFor, linkTargetsFor } from "@/lib/files/links";
// Imported for the registration it performs: without it the registry answers
// the plain Files link for a template, which is exactly the regression this
// file exists to catch.
import "@/pages/workspace/files/templateLinks";

const ORIGIN = "https://app.example.test";

function template(object: Record<string, unknown> = {}): Item {
  return {
    id: "nd_tpl",
    ino: 3,
    driveId: "dr_1",
    kind: "folder",
    name: "Monthly revenue.alkerachat.template",
    nameDisplay: "Monthly revenue.alkerachat.template",
    etag: "et_1",
    object: {
      type: "chat_template",
      id: "tpl_4",
      title: "Monthly revenue",
      web_url: "/templates/tpl_4",
      ...object,
    },
  } as unknown as Item;
}

describe("the links a chat template offers", () => {
  it("offers the template's page first, then its files", () => {
    expect(linkTargetsFor(template(), ORIGIN)).toEqual([
      { id: "page", label: "Link to template", href: "/templates/tpl_4" },
      { id: "files", label: "Link to files", href: `${ORIGIN}/files/nd_tpl` },
    ]);
  });

  it("points the files link at the working folder the server named", () => {
    const targets = linkTargetsFor(
      template({ metadata: { files_node_id: "nd_scratch" } }),
      ORIGIN,
    );
    expect(targets.find((target) => target.id === "files")?.href).toBe(
      `${ORIGIN}/files/nd_scratch`,
    );
  });

  it("copies the page link when nobody chose between them", () => {
    expect(linkFor(template(), ORIGIN)).toBe("/templates/tpl_4");
  });

  it("offers only the files when the deployment configured no page URL", () => {
    const targets = linkTargetsFor(template({ web_url: null }), ORIGIN);
    expect(targets).toEqual([
      { id: "files", label: "Link to files", href: `${ORIGIN}/files/nd_tpl` },
    ]);
  });
});
