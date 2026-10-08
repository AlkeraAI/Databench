// The shape of the project knowledge browse read, held apart from the provider
// that consumes it so a second reader can share the cache entry rather than
// fetch the catalogue again. The chat chrome's Knowledge count is that second
// reader: it wants only `total`, which this page already carries.
//
// It sits beside that second reader because the chat composition is shared by
// both shells and may not import the webview; the webview's knowledge provider
// — which owns the read — imports the spelling from here.

/** How much of the catalogue one browse page holds. */
export const CONTEXT_LIST_LIMIT = 200;

/** The one cache entry for that page. Every reader uses it verbatim. */
export const CONTEXT_LIST_KEY = ["ide", "context", "list"] as const;
