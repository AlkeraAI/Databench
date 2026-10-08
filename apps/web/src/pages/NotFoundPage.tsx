import { Link } from "react-router-dom";

import { useSpecificTitle } from "../app/documentTitle";
import { Icon } from "../app/icons";

/** The catch-all route inside the app shell. */
export function NotFoundPage() {
  // The nav claims no such path, so nothing else would name the tab.
  useSpecificTitle("Not found");
  return (
    <div className="alk-notfound">
      <span className="alk-notfound__mark" aria-hidden="true">
        <Icon name="overview" size={30} />
      </span>
      <h1 className="alk-notfound__title">This page doesn't exist</h1>
      <p className="alk-notfound__note">
        Check the address, or head back to your workspace.
      </p>
      <Link className="alk-link" to="/">
        Go to Overview
      </Link>
    </div>
  );
}
