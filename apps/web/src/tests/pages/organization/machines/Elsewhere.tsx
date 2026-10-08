import type { ReactElement } from "react";
import { useLocation } from "react-router-dom";

/** Any page but the one under test: says where the reader was sent. */
export function Elsewhere(): ReactElement {
  return (
    <>
      <p>Elsewhere</p>
      <p data-testid="location">{useLocation().pathname}</p>
    </>
  );
}
