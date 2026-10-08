import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PathDisplay } from "./PathDisplay";
import { WorkspacePathsProvider } from "./WorkspacePaths";

afterEach(cleanup);

const ABS = "/home/dev/warehouse/models/staging/x.sql";
const ROOT = "/home/dev/warehouse";

function parts(container: HTMLElement) {
  const start = container.querySelector(".alk-path-ellipsis__start")?.textContent ?? "";
  const end = container.querySelector(".alk-path-ellipsis__end")?.textContent ?? "";
  const copy = container.querySelector(".alk-path-ellipsis__copy")?.textContent ?? "";
  const aria = container.querySelector(".alk-path-ellipsis")?.getAttribute("aria-label") ?? "";
  return { visible: start + end, copy, aria };
}

describe("PathDisplay under WorkspacePathsProvider", () => {
  it("shows the workspace-relative form while copy + aria keep the absolute path", () => {
    const { container } = render(
      <WorkspacePathsProvider root={ROOT}>
        <PathDisplay path={ABS} />
      </WorkspacePathsProvider>,
    );
    const { visible, copy, aria } = parts(container);
    expect(visible).toBe("models/staging/x.sql");
    expect(copy).toBe(ABS);
    expect(aria).toBe(ABS);
  });

  it("lets an explicit displayPath override the context", () => {
    const { container } = render(
      <WorkspacePathsProvider root={ROOT}>
        <PathDisplay path={ABS} displayPath="x.sql" />
      </WorkspacePathsProvider>,
    );
    expect(parts(container).visible).toBe("x.sql");
    expect(parts(container).copy).toBe(ABS);
  });

});

describe("PathDisplay without a provider", () => {
  it("renders the path verbatim — the browser-portal fallback", () => {
    const { container } = render(<PathDisplay path={ABS} />);
    expect(parts(container).visible).toBe(ABS);
    expect(parts(container).copy).toBe(ABS);
  });
});
