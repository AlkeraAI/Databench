/**
 * The Files context menu, built from what the caller may actually do.
 *
 * The menu is a projection of two things and nothing else: the item's
 * `capabilities` (which decide whether a row is selectable and, when it is not,
 * carry the server's own refusal as the reason a person reads) and the shortcut
 * table in `state/shortcuts.ts` (which supplies every caption). Keeping both
 * outside this module is what stops the menu drifting from the keyboard: a
 * binding changed in one place changes the caption here, and a capability the
 * server stops granting greys the row without a second permission rule on the
 * client.
 *
 * Two kinds of "no", answered differently:
 *
 *  * **It does not apply.** Leasing a file, saving a plain folder as a template,
 *    renaming three rows at once, releasing a folder nobody holds. The row is
 *    not in the menu. Explaining on every right-click that a file is not a chat
 *    teaches nothing and buries the rows that do apply.
 *  * **It applies and is refused.** The caller may not rename this row, a
 *    machine holds the folder, the clipboard is empty. The row stays, disabled,
 *    carrying the reason — that is a fact about this row, and it changes.
 */

import type { ContextMenuItem } from "@alkera/ui";

import type { Item } from "@/api/files";
import { isChatFolder, isFolderObject, isTemplateFolder } from "@/lib/files/chatFolder";
import { BROWSE_FILES, openLabelOf, secondPageDoorOf } from "@/lib/files/openTarget";
import { capabilityRefusal, type CapName } from "./refusalCopy";
import { pastePlan, type ClipboardState } from "./state/clipboard";
import { shortcutLabel, type FilesAction, type Platform } from "./state/shortcuts";

/** Everything the menu can ask for. A superset of the keyboard table: some rows
 *  (Move to…, Duplicate, the lease four, Details) have no shortcut at all. */
export type MenuActionId =
  /** Open the row the way every gesture opens it (`openTargetOf`): a chat,
   *  a workspace or a template on its page, a folder listed, a file viewed. */
  | "open"
  /** List a folder-object's own files rather than open its page. */
  | "view-files"
  /** Open the page of a folder-object whose Open lists its files: a workspace. */
  | "open-page"
  | "open-new-tab"
  | "rename"
  | "share"
  /** Put a link to this row on the clipboard. Sharing and linking are the same
   *  errand — handing one thing to someone else — so the row sits beside Share…
   *  rather than being reachable only from inside the dialog. */
  | "copy-link"
  | "move-to"
  | "copy-to"
  | "copy"
  | "cut"
  | "paste"
  | "duplicate"
  | "download"
  | "new-folder"
  /** Create a notebook in the listed folder. Offered only where a notebook
   *  can be opened (a workspace's Files pane). */
  | "new-notebook"
  | "upload-files"
  | "upload-folder"
  | "release"
  | "request-release"
  | "force-release"
  | "star"
  /** Open the file's history. A read, so it is offered to anyone who may open
   *  the row; restoring one of them is refused inside the dialog. */
  | "versions"
  | "details"
  | "save-as-template"
  | "new-chat-from-template"
  | "trash"
  | "restore"
  | "delete-forever";

/** Which keyboard action a menu row shares its caption with, where there is one. */
const MENU_SHORTCUTS: Readonly<Partial<Record<MenuActionId, FilesAction>>> = {
  open: "open",
  rename: "rename",
  share: "share",
  copy: "copy",
  cut: "cut",
  paste: "paste",
  "new-folder": "new-folder",
  trash: "trash",
  details: "quick-look",
};

export interface ContextMenuInput {
  readonly platform: Platform;
  /** The rows the menu acts on: the selection, or the row that was right-clicked. */
  readonly targets: readonly Item[];
  /** The folder currently listed — where a paste, a new folder and an upload land. */
  readonly currentFolderId: string | undefined;
  /** True when the caller may create in the listed folder. */
  readonly canWriteHere: boolean;
  /** True when a workspace machine holds the listed folder under a lease that
   *  does not admit writes from the web. The caller's grant is untouched; the
   *  listing is read-only until the lease ends: nothing is added to it, pasted
   *  into it or duplicated inside it. A lease that admits them is not this. */
  readonly leasedHere?: boolean;
  /** Whether a row is inside a folder a machine is holding. Asked per target
   *  rather than read off `leasedHere`, because a feed lists rows from many
   *  folders and a menu on one of them must answer for that row's own lease.
   *  Absent, no row is held. */
  readonly isHeld?: (item: Item) => boolean;
  readonly clipboard: ClipboardState;
  /** The trash view has its own two rows and none of the others. */
  readonly inTrash?: boolean;
  /** A manager may take a lease away; nobody else sees Force release enabled. */
  readonly canForceRelease?: boolean;
  /** The surface can open a new notebook, so the menu offers to create one. */
  readonly canNewNotebook?: boolean;
  readonly onAction: (action: MenuActionId, targets: readonly Item[]) => void;
}

/** The reason a whole selection refuses a capability: the first row that says no,
 *  so a mixed selection names the item that is holding the action back. */
function refuseAll(targets: readonly Item[], cap: CapName, verb?: string): string | undefined {
  for (const item of targets) {
    const reason = capabilityRefusal(item, cap, verb);
    if (reason !== undefined) return reason;
  }
  return undefined;
}

const NO_SELECTION = "Select an item first.";
/** Every other row in the drive already IS its files; a chat, a workspace and
 *  a template keep a set of their own behind a page. */
const NOT_A_FOLDER_OBJECT = "Only a chat, a workspace or a template keeps files of its own.";
/** Open already lists a workspace's files, so a second row saying so is noise. */
const OPEN_IS_FILES = "Open lists these files.";
/** Only a row whose Open lists its files has a page that needs a row of its own. */
const NO_SECOND_PAGE = "Open already goes to this row's page.";
/** The reusable unit is the chat itself, so nothing else can be saved as one. */
const NOT_A_CHAT = "Only a chat can be saved as a template.";
/** A chat starts from a template and from nothing else: the brief and the files
 *  a new chat is handed only exist on one. */
const NOT_A_TEMPLATE = "Only a chat template can start a new chat.";
/** Versions are a history of BYTES. A folder keeps none of its own — what
 *  changed inside it is its children's history, not a list this row could show. */
const NOT_A_FILE = "Only a file keeps versions.";
const ONE_ONLY = "Select a single item.";
/** Why nothing is written into a folder a workspace machine holds under a
 *  lease that does not admit writes from the web: the server refuses them all
 *  until the lease ends. A lease that admits them never reaches this sentence.
 *  Shared with the toolbar's three create buttons, which say the same thing in
 *  their tooltip, so the menu and the bar cannot drift. */
export const LEASED_HERE = "This folder is leased and read-only until the lease ends.";
/** Why a row inside such a folder is not renamed, moved, trashed or cut. Said
 *  of the item rather than the folder, because a feed lists it away from home. */
export const IN_LEASED_FOLDER =
  "This item is in a leased folder and is read-only until the lease ends.";

interface RowOptions {
  /** Why the action does not apply to what was clicked. Present leaves the row
   *  out of the menu rather than drawing it dead with an explanation. */
  hide?: string | undefined;
}

function row(
  input: ContextMenuInput,
  id: MenuActionId,
  label: string,
  disabled: string | undefined,
  options: RowOptions = {},
): ContextMenuItem | null {
  if (options.hide !== undefined) return null;
  const action = MENU_SHORTCUTS[id];
  const shortcut = action === undefined ? null : shortcutLabel(action, input.platform);
  const item: ContextMenuItem = {
    id,
    label,
    onSelect: () => input.onAction(id, input.targets),
  };
  if (shortcut !== null) item.shortcut = shortcut;
  if (disabled !== undefined) item.disabled = disabled;
  return item;
}

/** What the menu says about one action for this selection, whichever way the
 *  action is asked for: `null` when its row is not in the menu (it does not
 *  apply — Rename on three rows), the row's reason when it is refused, and
 *  `undefined` when it may run. A keystroke and a toolbar button ask this, so
 *  they refuse exactly what the menu greys and in the menu's own words. */
export function menuVerdict(
  input: Omit<ContextMenuInput, "onAction">,
  action: MenuActionId,
): string | null | undefined {
  const found = buildContextMenuItems({ ...input, onAction: () => undefined }).find(
    (entry) => entry.id === action,
  );
  if (found === undefined) return null;
  return typeof found.disabled === "string" ? found.disabled : undefined;
}

/** The rows that survived: the menu is built as a full list and the ones that
 *  do not apply drop out of it. */
function present(rows: readonly (ContextMenuItem | null)[]): ContextMenuItem[] {
  return rows.filter((entry): entry is ContextMenuItem => entry !== null);
}

/** The lease facet of the single target, when there is exactly one. */
function soleLease(targets: readonly Item[]): Item["lease"] | undefined {
  return targets.length === 1 ? targets[0]?.lease : undefined;
}

/** Refused because of the selection's size, or `undefined` when the size is right. */
function sizeReason(targets: readonly Item[], one: boolean): string | undefined {
  if (targets.length === 0) return NO_SELECTION;
  return one && targets.length > 1 ? ONE_ONLY : undefined;
}

/**
 * The menu for one right-click, in a fixed order. A row the caller may not use
 * is present and disabled with its reason — a person has to be able to see that
 * Rename exists and learn why it is refused. A row that does not apply to what
 * was clicked is not built at all.
 */
export function buildContextMenuItems(input: ContextMenuInput): ContextMenuItem[] {
  const { targets } = input;
  const one = targets.length === 1;

  if (input.inTrash === true) {
    return present([
      row(input, "restore", "Restore", undefined, { hide: sizeReason(targets, false) }),
      row(input, "delete-forever", "Delete forever", refuseAll(targets, "can_delete"), {
        hide: sizeReason(targets, false),
      }),
    ]);
  }

  const plan =
    input.currentFolderId === undefined ? null : pastePlan(input.clipboard, input.currentFolderId);
  const pasteReason =
    input.clipboard.mode === null || input.clipboard.items.length === 0
      ? "Nothing has been copied or cut."
      : !input.canWriteHere
        ? "You cannot add to this folder."
        : input.leasedHere === true
          ? LEASED_HERE
          : plan === null
            ? "The cut items are already in this folder."
            : undefined;
  const createHere = input.canWriteHere ? undefined : "You cannot add to this folder.";
  // Writing into a folder a machine is holding: the grant is there, the writer
  // is not. A missing grant is the older and more basic refusal, so it is named
  // first when both apply.
  const addHere = createHere ?? (input.leasedHere === true ? LEASED_HERE : undefined);
  // Changing a row a machine is holding — its name, its place, its existence.
  // Asked of the rows themselves, so a feed's row answers for its own folder.
  const isHeld = input.isHeld ?? (() => false);
  const heldRow = targets.some(isHeld) ? IN_LEASED_FOLDER : undefined;

  const lease = soleLease(targets);
  const isFolder = one && targets[0]?.kind === "folder";
  const heldByMe = lease != null && lease.mine === true;
  const heldByOther = lease != null && lease.mine !== true;
  const notInUse = "This folder is not in use by anyone else.";
  // Exactly one of the four lease rows applies at a time, and which one is a
  // question about the folder rather than about the caller: a folder nobody
  // holds can be leased, the one I hold can be released, one somebody else holds
  // can be asked for or taken back. The other three are not in the menu, and
  // none of the four is on a file.
  const notLeasable = sizeReason(targets, true) ?? (isFolder ? undefined : "Not a folder.");

  // A chat, a workspace or a template answers to two intentions, so the menu
  // offers both: Open goes where a double-click goes and is named for it, and
  // Browse files lists what the object keeps.
  const chat = isChatFolder(targets[0]);
  const template = isTemplateFolder(targets[0]);
  const folderObject = isFolderObject(targets[0]);
  const pageDoor = secondPageDoorOf(targets[0]);
  /** Refused because there is nothing selected, or too much. Hides the row. */
  const needsOne = sizeReason(targets, true);
  const needsAny = sizeReason(targets, false);
  return present([
    row(input, "open", openLabelOf(targets[0]), refuseAll(targets, "can_read"), {
      hide: needsOne,
    }),
    // Every other row in the drive already IS its files, so there is nothing for
    // this to mean on one.
    row(input, "view-files", BROWSE_FILES, refuseAll(targets, "can_read"), {
      hide:
        needsOne ??
        (!folderObject ? NOT_A_FOLDER_OBJECT : pageDoor !== null ? OPEN_IS_FILES : undefined),
    }),
    row(input, "open-page", pageDoor?.label ?? "", refuseAll(targets, "can_read"), {
      hide: needsOne ?? (pageDoor === null ? NO_SECOND_PAGE : undefined),
    }),
    row(input, "open-new-tab", "Open in new tab", refuseAll(targets, "can_read"), {
      hide: needsOne,
    }),
    row(input, "rename", "Rename", refuseAll(targets, "can_rename") ?? heldRow, { hide: needsOne }),
    row(input, "share", "Share…", refuseAll(targets, "can_share"), { hide: needsOne }),
    // A link is to ONE thing, and reading it is all a link takes: somebody who
    // may open a row may hand its address on, whether or not they may change
    // who else can reach it. The link is not the access.
    row(input, "copy-link", "Copy link", refuseAll(targets, "can_read"), { hide: needsOne }),
    // Beside Share, because all three hand this work to someone else: one to a
    // person, one to a future self, one to a fresh agent.
    row(input, "save-as-template", "Save as template…", refuseAll(targets, "can_read"), {
      hide: needsOne ?? (chat ? undefined : NOT_A_CHAT),
    }),
    row(input, "new-chat-from-template", "New chat from template", refuseAll(targets, "can_read"), {
      hide: needsOne ?? (template ? undefined : NOT_A_TEMPLATE),
    }),
    row(input, "move-to", "Move to…", refuseAll(targets, "can_write", "move") ?? heldRow, {
      hide: needsAny,
    }),
    // Reading is all a copy takes from its source; where it lands is the
    // picker's business, so a row a person may only read still offers it.
    row(input, "copy-to", "Copy to…", refuseAll(targets, "can_read"), { hide: needsAny }),
    row(input, "copy", "Copy", refuseAll(targets, "can_read"), { hide: needsAny }),
    row(input, "cut", "Cut", refuseAll(targets, "can_write", "move") ?? heldRow, {
      hide: needsAny,
    }),
    // Paste acts on the folder, not on a row, so it is in the menu whatever is
    // selected — an empty clipboard fills again, and a staple that came and went
    // would move every row under it on every right-click.
    row(input, "paste", "Paste", pasteReason),
    // A duplicate lands beside its source, so it is refused by the folder it
    // lands in (its grant, then its lease) and by the row being held.
    row(input, "duplicate", "Duplicate", refuseAll(targets, "can_read") ?? addHere ?? heldRow, {
      hide: needsAny,
    }),
    row(input, "download", "Download", refuseAll(targets, "can_download"), { hide: needsAny }),
    row(input, "new-folder", "New folder", addHere),
    row(input, "new-notebook", "New notebook", addHere, {
      hide: input.canNewNotebook === true ? undefined : "Notebooks open in a workspace.",
    }),
    row(input, "upload-files", "Upload files", addHere),
    row(input, "upload-folder", "Upload folder", addHere),
    // Taking a folder out needs a machine and a purpose, and no surface collects
    // them any more. A row whose only possible answer is "not available" is
    // noise on every right-click, so it is not offered at all; the three rows
    // below write straight away and stay.
    row(input, "release", "Release", undefined, {
      hide: notLeasable ?? (heldByMe ? undefined : "You are not holding this folder."),
    }),
    row(input, "request-release", "Ask for it back", undefined, {
      hide: notLeasable ?? (heldByOther ? undefined : notInUse),
    }),
    // Taking a folder back from somebody else is a manager's to do: it applies
    // to every leased folder, so it is named rather than hidden.
    row(
      input,
      "force-release",
      "Take back",
      input.canForceRelease === true ? undefined : "Only a manager can take a folder back.",
      { hide: notLeasable ?? (heldByOther ? undefined : notInUse) },
    ),
    // Beside Details, because both look at ONE row rather than change it: what
    // this file is, and what it has been.
    row(input, "versions", "Versions", refuseAll(targets, "can_read"), {
      hide: needsOne ?? (targets[0]?.kind === "file" ? undefined : NOT_A_FILE),
    }),
    row(input, "details", "Details", undefined, { hide: needsOne }),
    // A chat is deleted from the chat itself; what this row reaches in the
    // drive is the folder of files, so it says which of the two it means.
    row(
      input,
      "trash",
      chat ? "Move the chat's files to trash" : "Move to trash",
      refuseAll(targets, "can_delete", "trash") ?? heldRow,
      { hide: needsAny },
    ),
  ]);
}
