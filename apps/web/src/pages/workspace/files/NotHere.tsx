// The id in the URL names nothing this reader may have.
//
// Two different truths land here — the node was deleted, and the node exists but
// is not shared with the reader — and they are drawn as the SAME card on
// purpose. A page that told them apart would answer "does this exist?" for
// anyone holding a guessed id, which is the one question an unshared node must
// not answer.

import { Link } from "react-router-dom";

export const NOT_HERE = "This isn't here, or isn't shared with you.";

export function NotHere() {
  return (
    <div className="alk-files-solo" role="status">
      <p className="alk-files-solo__line">{NOT_HERE}</p>
      <Link className="alk-files-solo__way" to="/files">
        Home
      </Link>
    </div>
  );
}

export default NotHere;
