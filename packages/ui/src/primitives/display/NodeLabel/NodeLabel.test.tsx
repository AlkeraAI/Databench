import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NodeLabel } from "./NodeLabel";
import { HOME_FALLBACK_LABEL, nodeLabel, pathLabel } from "./labels";

// A member's home is stored under its owner's id; everything a person reads names
// the owner instead. These pin the label rule and the rendered mark.

afterEach(cleanup);

const OWNER = "6b1f0b1e-0000-4000-8000-000000000002";

describe("nodeLabel", () => {
  it("reads a home as its owner's name, never the id it is stored under", () => {
    expect(nodeLabel({ name: OWNER, home: { owner_name: "Dana Ruiz" } })).toEqual({
      text: "Dana Ruiz",
      home: true,
    });
  });

  it.each([
    ["an empty name", ""],
    ["a blank name", "   "],
    ["no name", null],
  ])("reads a home whose owner has %s as the fallback, not the stored id", (_case, ownerName) => {
    expect(nodeLabel({ name: OWNER, home: { owner_name: ownerName } }).text).toBe(HOME_FALLBACK_LABEL);
  });

  it("reads an object as its current title over its name", () => {
    expect(nodeLabel({ name: "3952c9e2.alkerachat", object: { title: "Kickoff" } })).toEqual({
      text: "Kickoff",
      home: false,
    });
  });

  it("reads an untitled object, and an ordinary node, as its display name then its name", () => {
    expect(nodeLabel({ name: "raw", nameDisplay: "shown", object: { title: " " } }).text).toBe("shown");
    expect(nodeLabel({ name: "raw", nameDisplay: "" }).text).toBe("raw");
    expect(nodeLabel({ name: OWNER }).home).toBe(false);
  });
});

describe("pathLabel", () => {
  const home = { owner_id: OWNER, owner_name: "Dana Ruiz" };

  it.each([
    ["a whole path", `/home/${OWNER}/Chats/x`, "/home/Dana Ruiz/Chats/x"],
    ["a path cut at the home", `${OWNER}/Chats`, "Dana Ruiz/Chats"],
    ["the home itself", `/home/${OWNER}`, "/home/Dana Ruiz"],
  ])("names the home in %s", (_case, path, shown) => {
    expect(pathLabel(path, home)).toBe(shown);
  });

  it("leaves a path with no home, or a segment that only contains the id, alone", () => {
    expect(pathLabel("/Shared/x", home)).toBe("/Shared/x");
    expect(pathLabel(`/Shared/${OWNER}-copy`, home)).toBe(`/Shared/${OWNER}-copy`);
    expect(pathLabel(`/home/${OWNER}/x`, null)).toBe(`/home/${OWNER}/x`);
  });

  it("falls back rather than showing the id when the owner has no name", () => {
    expect(pathLabel(`/home/${OWNER}`, { owner_id: OWNER, owner_name: "" })).toBe(
      `/home/${HOME_FALLBACK_LABEL}`,
    );
  });
});

describe("NodeLabel", () => {
  it("draws the home mark beside the owner's name", () => {
    const { container } = render(<NodeLabel node={{ name: OWNER, home: { owner_name: "Dana Ruiz" } }} />);
    const root = container.querySelector(".alk-node-label");
    expect(root).toHaveAttribute("data-home", "true");
    expect(root?.querySelector("svg")).not.toBeNull();
    expect(root).toHaveTextContent("Dana Ruiz");
    expect(root).not.toHaveTextContent(OWNER);
  });

  it("draws no mark for an ordinary folder", () => {
    const { container } = render(<NodeLabel node={{ name: "Reports" }} />);
    const root = container.querySelector(".alk-node-label");
    expect(root).not.toHaveAttribute("data-home");
    expect(root?.querySelector("svg")).toBeNull();
    expect(root).toHaveTextContent("Reports");
  });

  it("formats the label the way the host escapes names", () => {
    const { container } = render(
      <NodeLabel node={{ name: OWNER, home: { owner_name: "Dana" } }} format={(text) => `«${text}»`} />,
    );
    expect(container).toHaveTextContent("«Dana»");
  });
});
