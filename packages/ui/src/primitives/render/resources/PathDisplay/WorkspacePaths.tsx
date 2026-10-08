import { createContext, type ReactNode, useContext, useMemo } from "react";
import { displayPath, type WorkspacePathContext } from "./displayPath";

const WorkspacePathsContext = createContext<WorkspacePathContext>({});

export interface WorkspacePathsProviderProps extends WorkspacePathContext {
  children: ReactNode;
}

/** Scope path rendering to a workspace: descendants (`PathDisplay`, tool-card
 * chips) show paths relative to `root` — `~`-abbreviated via `home` when outside
 * it — while tooltips/copy keep the absolute form. Without a provider (the
 * browser portal, chats viewed off-host) display stays verbatim. Multi-root
 * hosts pass their primary folder; paths under a second root just render
 * `~`-abbreviated. */
export function WorkspacePathsProvider({ root, home, children }: WorkspacePathsProviderProps) {
  const value = useMemo(() => ({ root, home }), [root, home]);
  return <WorkspacePathsContext.Provider value={value}>{children}</WorkspacePathsContext.Provider>;
}

/** A formatter from raw path to its workspace-relative display form — identity
 * when no `WorkspacePathsProvider` is above. */
export function useDisplayPath(): (path: string) => string {
  const ctx = useContext(WorkspacePathsContext);
  return useMemo(() => (path: string) => displayPath(path, ctx), [ctx]);
}
