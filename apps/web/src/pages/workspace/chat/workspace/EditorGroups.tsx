// The editor groups beside a chat, laid out the way the reader split them.
//
// The layout is a tree (`editorLayout`): a split draws its children along one
// axis with a divider between each pair, and a leaf draws a group: its own
// strip over its own panel, with the tab in front of it mounted and nothing
// else. Every rule about what a split, a move or a close does lives in the
// store; this file turns pointer and keyboard gestures into those calls.
//
// A tab is dragged with the platform's own drag and drop. While a drag is on,
// every group's panel is covered by a drop surface: the panel may be a framed
// document that would otherwise swallow the drag, and the surface is what says
// where it would land: the middle moves the tab into the group, an edge splits
// the group and puts the tab on that side.

import { IconDots, IconLayoutColumns, IconLayoutRows } from "@tabler/icons-react";
import {
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type PointerEvent,
  type ReactElement,
} from "react";

import { Button, Dropdown, DropdownItem, EmptyState, Tooltip, type TabStripDrag } from "@alkera/ui";

import type { Item } from "@/api/files";

import { MAX_GROUPS, groupOrder, nudgeShares, type LayoutNode, type LayoutPath, type Side } from "./editorLayout";
import { tabKindFor, UNKNOWN_TAB_KIND_NOTICE, type WorkspaceCtx, type WorkspaceTab } from "./tabKinds";
import {
  FILES_TAB_ID,
  groupById,
  tabsOf,
  useWorkspaceStore,
  type ChatWorkspaceEntry,
  type TabDrop,
} from "./workspaceStore";
import { WorkspaceTabs } from "./WorkspaceTabs";

/** The data type a dragged workspace tab carries. Only drops of this type are
 *  taken, so a file dragged in from the desktop is never read as a tab. */
export const TAB_DRAG_TYPE = "application/x-alkera-workspace-tab";

/** How far one arrow key moves a divider, as a share of its split. */
const DIVIDER_STEP = 0.05;

/** Where a drop on a group's panel lands, by where the pointer is: the outer
 *  quarter on each side splits, the middle moves the tab in. */
export type DropZone = "center" | Side;

export function dropZoneAt(x: number, y: number, width: number, height: number): DropZone {
  if (width <= 0 || height <= 0) return "center";
  const fx = x / width;
  const fy = y / height;
  // The edge the pointer is nearest wins where two quarters overlap.
  const edges: [Side, number][] = [
    ["left", fx],
    ["right", 1 - fx],
    ["up", fy],
    ["down", 1 - fy],
  ];
  const [side, distance] = edges.reduce((best, edge) => (edge[1] < best[1] ? edge : best));
  return distance < 0.25 ? side : "center";
}

interface DragPayload {
  chat: string;
  tab: string;
}

function readPayload(raw: string, chatId: string): DragPayload | null {
  try {
    const parsed = JSON.parse(raw) as Partial<DragPayload>;
    if (parsed.chat !== chatId || typeof parsed.tab !== "string") return null;
    return { chat: parsed.chat, tab: parsed.tab };
  } catch {
    return null;
  }
}

export interface EditorGroupsProps {
  chatId: string;
  entry: ChatWorkspaceEntry;
  ctx: WorkspaceCtx;
  itemOf: (tab: WorkspaceTab) => Item | undefined;
}

export function EditorGroups({ chatId, entry, ctx, itemOf }: EditorGroupsProps): ReactElement {
  const [dragging, setDragging] = useState(false);

  // A tab that is dropped somewhere else is unmounted from where it started,
  // and the element that began a drag never hears its end once it is gone. So
  // the end of any drag on the page ends this one, heard on the way OUT of the
  // event, never on the way in: a browser runs pending work between one
  // listener and the next, and taking the drop surfaces down in the capture
  // phase would remove the very surface the drop is about to land on.
  useEffect(() => {
    if (!dragging) return;
    const stop = (): void => setDragging(false);
    window.addEventListener("dragend", stop);
    window.addEventListener("drop", stop);
    return () => {
      window.removeEventListener("dragend", stop);
      window.removeEventListener("drop", stop);
    };
  }, [dragging]);

  const order = groupOrder(entry.layout);
  return (
    <div className="alk-ws-groups" data-dragging={dragging || undefined}>
      <LayoutView
        node={entry.layout}
        path={[]}
        shared={{ chatId, entry, ctx, itemOf, order, dragging, setDragging }}
      />
    </div>
  );
}

interface Shared {
  chatId: string;
  entry: ChatWorkspaceEntry;
  ctx: WorkspaceCtx;
  itemOf: (tab: WorkspaceTab) => Item | undefined;
  order: string[];
  dragging: boolean;
  setDragging: (on: boolean) => void;
}

function keyOf(node: LayoutNode): string {
  return node.kind === "group" ? node.id : `split:${groupOrder(node)[0] ?? ""}`;
}

function LayoutView({ node, path, shared }: { node: LayoutNode; path: LayoutPath; shared: Shared }): ReactElement {
  const box = useRef<HTMLDivElement | null>(null);
  if (node.kind === "group") return <EditorGroupView groupId={node.id} shared={shared} />;
  return (
    <div className="alk-ws-split" data-axis={node.axis} ref={box}>
      {node.children.map((child, index) => (
        <Fragment key={keyOf(child)}>
          {index > 0 ? (
            <SplitDivider
              chatId={shared.chatId}
              axis={node.axis}
              path={path}
              index={index - 1}
              sizes={node.sizes}
              box={box}
            />
          ) : null}
          <div className="alk-ws-split__cell" style={{ flexGrow: node.sizes[index] ?? 1 }}>
            <LayoutView node={child} path={[...path, index]} shared={shared} />
          </div>
        </Fragment>
      ))}
    </div>
  );
}

interface DividerProps {
  chatId: string;
  axis: "row" | "column";
  path: LayoutPath;
  index: number;
  sizes: readonly number[];
  box: React.RefObject<HTMLDivElement | null>;
}

/** The boundary between two neighbours of a split: dragged with a pointer, or
 *  moved with the arrow keys once it has focus. */
function SplitDivider({ chatId, axis, path, index, sizes, box }: DividerProps): ReactElement {
  const resizeSplit = useWorkspaceStore((state) => state.resizeSplit);
  const nudgeSplit = useWorkspaceStore((state) => state.nudgeSplit);
  const start = useRef<{ at: number; sizes: readonly number[]; length: number } | null>(null);
  const row = axis === "row";

  const onPointerDown = (event: PointerEvent<HTMLDivElement>): void => {
    const rect = box.current?.getBoundingClientRect();
    const length = rect ? (row ? rect.width : rect.height) : 0;
    if (length <= 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    start.current = { at: row ? event.clientX : event.clientY, sizes, length };
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>): void => {
    const from = start.current;
    if (from === null) return;
    const delta = ((row ? event.clientX : event.clientY) - from.at) / from.length;
    resizeSplit(chatId, path, nudgeShares(from.sizes, index, delta));
  };
  const onPointerUp = (event: PointerEvent<HTMLDivElement>): void => {
    start.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const back = row ? "ArrowLeft" : "ArrowUp";
    const forward = row ? "ArrowRight" : "ArrowDown";
    if (event.key !== back && event.key !== forward) return;
    event.preventDefault();
    nudgeSplit(chatId, path, index, event.key === forward ? DIVIDER_STEP : -DIVIDER_STEP);
  };

  const share = sizes[index] ?? 0;
  const pair = share + (sizes[index + 1] ?? 0);
  return (
    <div
      className="alk-ws-split__divider"
      data-axis={axis}
      role="separator"
      tabIndex={0}
      aria-label="Resize editor groups"
      aria-orientation={row ? "vertical" : "horizontal"}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pair > 0 ? Math.round((share / pair) * 100) : 50}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={onKeyDown}
    />
  );
}

function EditorGroupView({ groupId, shared }: { groupId: string; shared: Shared }): ReactElement | null {
  const { chatId, entry, ctx, itemOf, order, dragging, setDragging } = shared;
  const activate = useWorkspaceStore((state) => state.activate);
  const closeTab = useWorkspaceStore((state) => state.closeTab);
  const pinTab = useWorkspaceStore((state) => state.pinTab);
  const moveTab = useWorkspaceStore((state) => state.moveTab);
  const focusGroup = useWorkspaceStore((state) => state.focusGroup);
  const splitTab = useWorkspaceStore((state) => state.splitTab);

  const group = groupById(entry, groupId);
  const tabs = tabsOf(entry, groupId);
  const position = order.indexOf(groupId);
  const several = order.length > 1;
  const isActive = entry.activeGroupId === groupId;

  const drag: TabStripDrag = {
    type: TAB_DRAG_TYPE,
    payload: (id) => JSON.stringify({ chat: chatId, tab: id }),
    onDragStart: () => setDragging(true),
    onDragEnd: () => setDragging(false),
    onDrop: (raw, index) => {
      setDragging(false);
      const payload = readPayload(raw, chatId);
      if (!payload) return;
      // The index counts the dragged tab where it still stands; once it is
      // lifted out, everything after it moves up one.
      const from = tabs.findIndex((tab) => tab.id === payload.tab);
      moveTab(chatId, payload.tab, { group: groupId, index: from >= 0 && from < index ? index - 1 : index });
    },
  };

  const front = tabs.find((tab) => tab.id === group?.activeTabId) ?? tabs[0];
  const kind = front ? tabKindFor(front.kind) : undefined;
  const Panel = kind?.Component;
  const canSplit = order.length < MAX_GROUPS && front?.kind === "file";
  const neighbourAt = (step: number): string | undefined => order[position + step];

  const enter = (): void => {
    if (!isActive) focusGroup(chatId, groupId);
  };

  if (!group) return null;
  const label = several ? `Editor group ${position + 1}` : "Editor";
  return (
    <section
      className="alk-ws-group"
      data-active={isActive && several ? "" : undefined}
      aria-label={label}
      onFocusCapture={enter}
      onPointerDownCapture={enter}
    >
      <WorkspaceTabs
        tabs={tabs}
        activeId={front?.id ?? null}
        updated={entry.updated}
        gone={entry.gone}
        itemOf={itemOf}
        label={several ? `Open files, group ${position + 1}` : "Open files"}
        onActivate={(id) => activate(chatId, id)}
        onClose={(id) => closeTab(chatId, id)}
        onPin={(id) => pinTab(chatId, id)}
        drag={drag}
        trailing={
          <GroupActions
            canSplit={canSplit}
            onSplit={(side) => splitTab(chatId, side, front?.id)}
            moves={
              front
                ? [
                    ...(neighbourAt(-1)
                      ? [{ id: "prev", label: "Move to previous group", to: neighbourAt(-1) as string }]
                      : []),
                    ...(neighbourAt(1) ? [{ id: "next", label: "Move to next group", to: neighbourAt(1) as string }] : []),
                  ]
                : []
            }
            onMove={(to) => front && moveTab(chatId, front.id, { group: to })}
            onCloseGroup={
              several && !tabs.some((tab) => tab.id === FILES_TAB_ID)
                ? () => tabs.forEach((tab) => closeTab(chatId, tab.id))
                : undefined
            }
          />
        }
      />
      {front ? (
        <div
          className="alk-ws__panel"
          role="tabpanel"
          id={`panel-${front.id}`}
          aria-labelledby={`tab-${front.id}`}
        >
          {Panel ? (
            <Panel tab={front} ctx={ctx} />
          ) : (
            <EmptyState size="md" title={UNKNOWN_TAB_KIND_NOTICE} />
          )}
          {dragging ? <DropSurface chatId={chatId} groupId={groupId} onDrop={(drop) => moveTab(chatId, drop.tab, drop.to)} /> : null}
        </div>
      ) : null}
    </section>
  );
}

interface GroupActionsProps {
  canSplit: boolean;
  onSplit: (side: Side) => void;
  moves: { id: string; label: string; to: string }[];
  onMove: (to: string) => void;
  onCloseGroup?: () => void;
}

function GroupActions({ canSplit, onSplit, moves, onMove, onCloseGroup }: GroupActionsProps): ReactElement {
  return (
    <div className="alk-ws-group__actions" role="group" aria-label="Group actions">
      <Tooltip label="Split right">
        {(tip) => (
          <Button
            {...tip}
            iconOnly
            aria-label="Split right"
            variant="secondary"
            fill="ghost"
            size="sm"
            disabled={!canSplit}
            onClick={() => onSplit("right")}
          >
            <IconLayoutColumns size={15} stroke={1.8} aria-hidden />
          </Button>
        )}
      </Tooltip>
      <Tooltip label="Split down">
        {(tip) => (
          <Button
            {...tip}
            iconOnly
            aria-label="Split down"
            variant="secondary"
            fill="ghost"
            size="sm"
            disabled={!canSplit}
            onClick={() => onSplit("down")}
          >
            <IconLayoutRows size={15} stroke={1.8} aria-hidden />
          </Button>
        )}
      </Tooltip>
      {moves.length > 0 || onCloseGroup ? (
        <Dropdown
          label="Group actions"
          trigger={{
            kind: "icon",
            icon: <IconDots size={15} stroke={1.8} aria-hidden />,
            ariaLabel: "More group actions",
            variant: "secondary",
            fill: "ghost",
            size: "sm",
          }}
          tickSide="right"
        >
          {moves.map((move) => (
            <DropdownItem key={move.id} onSelect={() => onMove(move.to)}>
              {move.label}
            </DropdownItem>
          ))}
          {onCloseGroup ? <DropdownItem onSelect={onCloseGroup}>Close group</DropdownItem> : null}
        </Dropdown>
      ) : null}
    </div>
  );
}

/** What covers a group's panel while a tab is being dragged, and where a drop
 *  on it would land. */
function DropSurface({
  chatId,
  groupId,
  onDrop,
}: {
  chatId: string;
  groupId: string;
  onDrop: (drop: { tab: string; to: TabDrop }) => void;
}): ReactElement {
  const [zone, setZone] = useState<DropZone | null>(null);
  const zoneOf = useCallback((event: DragEvent<HTMLDivElement>): DropZone => {
    const rect = event.currentTarget.getBoundingClientRect();
    return dropZoneAt(event.clientX - rect.left, event.clientY - rect.top, rect.width, rect.height);
  }, []);
  const carries = (event: DragEvent<HTMLDivElement>): boolean =>
    Array.from(event.dataTransfer?.types ?? []).includes(TAB_DRAG_TYPE);

  return (
    <div
      className="alk-ws-drop"
      data-testid={`drop-${groupId}`}
      onDragOver={(event) => {
        if (!carries(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        const next = zoneOf(event);
        if (next !== zone) setZone(next);
      }}
      onDragLeave={() => setZone(null)}
      onDrop={(event) => {
        if (!carries(event)) return;
        event.preventDefault();
        const where = zoneOf(event);
        setZone(null);
        const payload = readPayload(event.dataTransfer.getData(TAB_DRAG_TYPE), chatId);
        if (!payload) return;
        onDrop({ tab: payload.tab, to: where === "center" ? { group: groupId } : { group: groupId, split: where } });
      }}
    >
      {zone ? <div className="alk-ws-drop__target" data-zone={zone} aria-hidden /> : null}
    </div>
  );
}
