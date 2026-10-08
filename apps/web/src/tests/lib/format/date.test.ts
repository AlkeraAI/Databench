// One date policy: every portal surface renders an instant in the VIEWER'S locale
// and timezone. The suite pins a non-UTC zone before the formatters are built, so a
// surface that quietly reverts to a UTC-pinned formatter (the deleted teams
// `formatUtcDate`) disagrees on which calendar day the instant falls on and fails here.

import { afterAll, describe, expect, it } from "vitest";
import type { components } from "@alkera/sdk";

const PREVIOUS_TZ = process.env.TZ;
process.env.TZ = "America/New_York";

// Dynamic imports so the TZ pin above lands before date.ts constructs its
// module-level Intl formatters (static imports would hoist past it).
const { formatCalendarDay, formatDate, formatDateTime, formatDateTimeExact } = await import(
  "@/lib/format/date"
);
const { date } = await import("@/pages/platform/admin/shared/format");
const { toTeamsGraph } = await import("@/pages/organization/teams/data/adapt");
const { directRoster } = await import("@/pages/organization/teams/data/model");

afterAll(() => {
  if (PREVIOUS_TZ === undefined) delete process.env.TZ;
  else process.env.TZ = PREVIOUS_TZ;
});

// 02:30 UTC is the previous evening in New York — the day-flip instant that once
// showed one timestamp as two different days on two screens.
const INSTANT = "2026-07-25T02:30:00Z";

// The same day-flip instant with a non-zero seconds field, so the forensic formatter's extra
// resolution is observable rather than a silent ":00".
const SECONDS_INSTANT = "2026-07-25T02:30:43Z";

const mediumDate = (iso: string): string =>
  new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(Date.parse(iso));

describe("formatDate / formatDateTime / formatDateTimeExact", () => {
  it("keeps the probe armed: the pinned instant straddles the UTC day boundary", () => {
    const local = new Intl.DateTimeFormat("en-US", { dateStyle: "medium" }).format(Date.parse(INSTANT));
    const utc = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeZone: "UTC" }).format(Date.parse(INSTANT));
    expect(local).not.toBe(utc);
  });

  it("renders the medium date style in the viewer's locale and timezone", () => {
    expect(formatDate(INSTANT)).toBe(mediumDate(INSTANT));
  });

  it("renders medium date plus short time in the viewer's locale and timezone", () => {
    const expected = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(Date.parse(INSTANT));
    expect(formatDateTime(INSTANT)).toBe(expected);
  });

  it("resolves an audit instant to the second, under the same zone and locale policy", () => {
    // The forensic variant: audit rows get correlated against server logs line by line, so it
    // must carry seconds where formatDateTime stops at minutes — and it must not buy that by
    // slipping back to UTC, the failure the whole module exists to close.
    const expected = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "medium",
    }).format(Date.parse(SECONDS_INSTANT));
    expect(formatDateTimeExact(SECONDS_INSTANT)).toBe(expected);

    // Independent of the options above: one second apart is a different reading here and the
    // same reading in the minute-resolution formatter. A silent downgrade to timeStyle "short"
    // passes the equality if it is re-derived from the wrong options, but never this.
    const oneSecondLater = "2026-07-25T02:30:44Z";
    expect(formatDateTimeExact(oneSecondLater)).not.toBe(formatDateTimeExact(SECONDS_INSTANT));
    expect(formatDateTime(oneSecondLater)).toBe(formatDateTime(SECONDS_INSTANT));

    // Same calendar day as every other surface, not the UTC one the probe above showed differs.
    expect(formatDateTimeExact(SECONDS_INSTANT)).toContain(formatDate(SECONDS_INSTANT));
  });

  it.each([null, undefined, "", "not-a-date", "2026-99-99T00:00:00Z"])(
    "renders %j as an empty string instead of throwing or showing Invalid Date",
    (garbage) => {
      expect(formatDate(garbage)).toBe("");
      expect(formatDateTime(garbage)).toBe("");
      expect(formatDateTimeExact(garbage)).toBe("");
    },
  );
});

describe("date-only values pin to UTC", () => {
  // A bare calendar date names a DAY, not an instant. Date.parse reads it as UTC
  // midnight, so the viewer-zone policy west of UTC would rename it to the prior day.
  const DAY = "2026-07-25";
  const utcDay = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(
    Date.parse(DAY),
  );

  it("keeps the probe armed: the pinned zone reads UTC midnight as the prior day", () => {
    // Guards the case below against a UTC CI runner, where the two renderings agree
    // and a viewer-zone regression would pass unnoticed.
    const local = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(Date.parse(DAY));
    expect(local).not.toBe(utcDay);
  });

  it("renders the named day, never the viewer zone's prior evening", () => {
    expect(formatDate(DAY)).toBe(utcDay);
  });

  it("the time-bearing formatters keep the named day too", () => {
    expect(formatDateTime(DAY)).toContain(utcDay);
    expect(formatDateTimeExact(DAY)).toContain(utcDay);
  });
});

describe("the cross-screen calendar-day invariant", () => {
  // The bug this module exists to close: the teams page and the admin registry once
  // answered "which day?" in different timezones. Both public surfaces must render
  // one instant as the SAME string.

  type TeamWire = components["schemas"]["TeamRead"];
  type MemberWire = components["schemas"]["TeamMemberRead"];

  const root: TeamWire = {
    id: "t-root",
    name: "Org",
    parent_team_id: null,
    is_root: true,
    created_at: "2026-01-01T00:00:00Z",
    member_count: 1,
  };
  const memberRow: MemberWire = {
    user_id: "u1",
    display_name: "Ada Root",
    email: "ada@x.io",
    first_name: "Ada",
    last_name: "Root",
    role: "admin",
    team_id: root.id,
    team_name: root.name,
    created_at: INSTANT,
    role_display: "Admin",
    effective_role: "admin",
    direct_role: "admin",
    descent_role: null,
    descent_from_team_id: null,
    descent_from_team_name: null,
  };

  it("the teams joined label and the admin date column render the same day for one instant", () => {
    const graph = toTeamsGraph({
      teams: [root],
      viewerId: "u1",
      adminTeamIds: [root.id],
      selectedId: root.id,
      members: [memberRow],
      invites: [],
    });
    const joined = directRoster(graph, root.id)[0].joined;
    expect(joined).toBe(date(INSTANT));
    expect(joined).toBe(mediumDate(INSTANT));
  });
});

describe("formatCalendarDay", () => {
  // A recurring grant's end date: the admin typed Sep 29, the form sent UTC midnight.
  const END_DATE = "2027-09-29T00:00:00Z";

  it("reads back the day that was entered, where the viewer-zone reading shows the day before", () => {
    const entered = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(
      Date.UTC(2027, 8, 29),
    );
    expect(formatCalendarDay(END_DATE)).toBe(entered);
    expect(formatDate(END_DATE)).not.toBe(entered);
  });

  it("is empty for a missing or unparsable value", () => {
    expect(formatCalendarDay(null)).toBe("");
    expect(formatCalendarDay(undefined)).toBe("");
    expect(formatCalendarDay("not a date")).toBe("");
  });
});
