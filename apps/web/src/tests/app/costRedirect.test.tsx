import { render, screen } from "@testing-library/react";
import { MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

// Spend is the Budget metric of the Usage page, so /analytics/cost lands on
// that reading and carries any extra query a bookmark or a mail link was holding. This pins the redirect's own rule (the App route
// table wires the same LegacyRedirect every other retired path uses).

/** The App's LegacyRedirect, verbatim: params in `to` win over same-named ones
 *  already on the URL, and the hash rides along. */
function LegacyRedirect({ to }: { to: string }) {
  const location = useLocation();
  const [path, toQuery] = to.split("?");
  const params = new URLSearchParams(location.search);
  for (const [key, value] of new URLSearchParams(toQuery ?? "")) params.set(key, value);
  const search = params.toString();
  return <Navigate to={`${path}${search ? `?${search}` : ""}${location.hash}`} replace />;
}

function Landing() {
  const location = useLocation();
  return <div data-testid="landed">{`${location.pathname}${location.search}`}</div>;
}

function renderAt(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/analytics/cost"
          element={<LegacyRedirect to="/analytics/usage?metric=budget&scope=org" />}
        />
        <Route path="/analytics/usage" element={<Landing />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("/analytics/cost", () => {
  it("lands on the Usage page's budget metric at organization scope", () => {
    renderAt("/analytics/cost");
    const landed = screen.getByTestId("landed").textContent ?? "";
    expect(landed.startsWith("/analytics/usage")).toBe(true);
    const params = new URLSearchParams(landed.split("?")[1]);
    expect(params.get("metric")).toBe("budget");
    expect(params.get("scope")).toBe("org");
  });

  it("carries an extra query through", () => {
    renderAt("/analytics/cost?window=7d");
    const params = new URLSearchParams(
      (screen.getByTestId("landed").textContent ?? "").split("?")[1],
    );
    expect(params.get("window")).toBe("7d");
    expect(params.get("metric")).toBe("budget");
  });

  it("does not let a stale query override the metric it redirects to", () => {
    renderAt("/analytics/cost?metric=requests");
    const params = new URLSearchParams(
      (screen.getByTestId("landed").textContent ?? "").split("?")[1],
    );
    expect(params.get("metric")).toBe("budget");
  });
});
