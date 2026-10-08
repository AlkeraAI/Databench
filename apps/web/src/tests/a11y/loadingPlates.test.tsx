// A loading plate names itself with `aria-label`, which ARIA forbids on a role-less `div`: axe flags
// it (`aria-prohibited-attr`) and the label is announced as nothing at all. Each plate therefore has
// to carry a role that admits a name — and `status` is the one that also announces the busy state.

import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SettingsSkeleton } from "@/pages/organization/settings/fields";
import { StatStripSkeleton, TableSkeleton } from "@/pages/platform/admin/shared/states";

afterEach(cleanup);

describe("loading plates", () => {
  it("names the settings skeleton through a role that admits a name", () => {
    render(<SettingsSkeleton />);
    expect(screen.getByRole("status", { name: "Loading settings" })).toHaveAttribute("aria-busy", "true");
  });

  it("names the admin register skeleton through a role that admits a name", () => {
    render(<TableSkeleton rows={1} cols={2} />);
    expect(screen.getByRole("status", { name: "Loading" })).toHaveAttribute("aria-busy", "true");
  });

  it("names the admin stat-strip skeleton through a role that admits a name", () => {
    render(<StatStripSkeleton tiles={2} />);
    expect(screen.getByRole("status", { name: "Loading" })).toHaveAttribute("aria-busy", "true");
  });
});
