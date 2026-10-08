// A register row whose account has no display name still has to be reachable and announceable:
// the anchor is the row's only doorway into the detail, so it must never render nameless.

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { LinkCell } from "@/pages/platform/admin/shared/chrome";

function renderCell(node: React.ReactNode) {
  return render(<MemoryRouter>{node}</MemoryRouter>);
}

describe("LinkCell", () => {
  it("names the link with the row's own name when it has one", () => {
    renderCell(<LinkCell to="/admin/users/abc">Ada Lovelace</LinkCell>);
    expect(screen.getByRole("link", { name: "Ada Lovelace" })).toHaveAttribute("href", "/admin/users/abc");
  });

  it("falls back to the email when the name is the profile-incomplete empty string", () => {
    renderCell(
      <LinkCell to="/admin/users/8b1f0c22-0000-4000-8000-000000000001" fallback="ada@example.com">
        {""}
      </LinkCell>,
    );
    expect(screen.getByRole("link", { name: /ada@example\.com/ })).toBeInTheDocument();
  });

  it("falls back to the row's short id when there is neither a name nor an email", () => {
    renderCell(<LinkCell to="/admin/orgs/8b1f0c22-0000-4000-8000-000000000001">{""}</LinkCell>);
    expect(screen.getByRole("link", { name: /8b1f0c22/ })).toBeInTheDocument();
  });

  it("treats a whitespace-only name as no name", () => {
    renderCell(
      <LinkCell to="/admin/users/8b1f0c22-0000-4000-8000-000000000001" fallback="ada@example.com">
        {"   "}
      </LinkCell>,
    );
    expect(screen.getByRole("link", { name: /ada@example\.com/ })).toBeInTheDocument();
  });

  it("treats a blank fallback as no fallback and still names the link", () => {
    renderCell(
      <LinkCell to="/admin/users/8b1f0c22-0000-4000-8000-000000000001" fallback="  ">
        {null}
      </LinkCell>,
    );
    expect(screen.getByRole("link", { name: /8b1f0c22/ })).toBeInTheDocument();
  });

  it("never renders an anchor with no accessible name", () => {
    const { container } = renderCell(<LinkCell to="/admin/users/8b1f0c22-0000-4000-8000-000000000001">{undefined}</LinkCell>);
    const anchor = container.querySelector("a");
    expect(anchor?.textContent?.trim()).toBeTruthy();
  });
});
