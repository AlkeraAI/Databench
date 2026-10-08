import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Terminal } from "./Terminal";
import { tokenizeShell } from "../highlight";

describe("tokenizeShell", () => {
  it.each([
    {
      line: 'git commit -m "fix bug"',
      kinds: { git: "command", commit: "arg", "-m": "flag", '"fix bug"': "string" },
    },
    { line: "sleep 5 $HOME # wait", kinds: { "5": "number", $HOME: "variable", "# wait": "comment" } },
  ])("classifies the tokens of $line", ({ line, kinds }) => {
    const byText = new Map(tokenizeShell(line).filter((s) => s.kind).map((s) => [s.text, s.kind]));
    for (const [text, kind] of Object.entries(kinds)) expect(byText.get(text)).toBe(kind);
  });

  it("restarts the command after a pipe operator", () => {
    const segs = tokenizeShell("cat f | grep x").filter((s) => s.kind);
    const commands = segs.filter((s) => s.kind === "command").map((s) => s.text);
    expect(commands).toEqual(["cat", "grep"]);
    expect(segs.find((s) => s.text === "|")?.kind).toBe("operator");
  });

});

describe("Terminal", () => {
  it("renders a prompt sigil, the cwd, and the highlighted command", () => {
    render(<Terminal cwd="~/proj" command="ls -la" />);
    expect(screen.getByText("~/proj")).toBeInTheDocument();
    expect(screen.getByText("$")).toBeInTheDocument();
    const cmd = screen.getByText("ls");
    expect(cmd).toHaveClass("alk-terminal__sh--command");
  });

  it("omits the prompt line when no command is given", () => {
    const { container } = render(<Terminal output="stray output" />);
    expect(container.querySelector(".alk-terminal__line--command")).toBeNull();
    expect(screen.getByText("stray output")).toBeInTheDocument();
  });
});
