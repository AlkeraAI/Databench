/**
 * The guard in front of `/files/:nodeId`.
 *
 * Whatever is in the address bar arrives here as the route's parameter, and an id the
 * drive cannot even parse is answered by the server's validator rather than by the
 * drive — a 422 the page had no reading for, so `/files/not-a-uuid` drew "Opening…"
 * and stayed there. The shape is therefore decided before the read: an address that
 * cannot name a node is an address this app does not have, and it lands on the same
 * not-found page every other unrouted address does.
 *
 * It stands in FRONT of the page rather than inside it so a malformed id costs no
 * request at all — the drive, the listing and the account are reads the page makes on
 * mount, and none of them are owed to an address that is not a node's.
 */

import type { ReactElement } from "react";
import { useParams } from "react-router-dom";

import { NotFoundPage } from "@/pages/NotFoundPage";

/** Every item in the drive is addressed by uuid. */
const NODE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Whether `value` could name a node in the drive at all. */
export function isFilesNodeId(value: string): boolean {
  return NODE_ID.test(value);
}

export interface FilesRouteProps {
  /** The Files screen, rendered only for an address that could name a node. */
  children: ReactElement;
}

export function FilesRoute({ children }: FilesRouteProps): ReactElement {
  const { nodeId } = useParams<{ nodeId: string }>();
  if (nodeId !== undefined && !isFilesNodeId(nodeId)) return <NotFoundPage />;
  return children;
}

export default FilesRoute;
