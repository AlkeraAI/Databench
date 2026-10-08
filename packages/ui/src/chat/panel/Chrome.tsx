// The chat's printer header: an identity row -- the chat name leading with the
// workspace trailing as a dim breadcrumb (or, for a chat opened from another
// one, the trail through the stack in place of both), then the actions
// (the web app, the account and settings menu, a new chat, the quick links) --
// over the working rail of quick links to the project's work objects. Both
// menus here are the package's one PopMenu, so they frame, dismiss, and read
// alike. A link wears a count only when the host knows one; an unknown count is
// no badge, never a zero. The rail never wraps: labels show while they fit,
// fold to icon + count, then fold out entirely, and only at that last width
// does the chrome's three-dot menu appear to become the links' one remaining
// home.
import { useEffect, useLayoutEffect, useRef, useState, type HTMLAttributes, type ReactElement, type ReactNode } from "react";
import { ContextMenu, Text, Tooltip, useContextMenu, type ContextMenuItem } from "../sharedUi";
import { IconCopy, IconListCheck, IconLogout, IconPencil, IconPlug, IconSettings, IconShare, IconTrash, IconWorld } from "@tabler/icons-react";
import { Breadcrumbs, type Crumb } from "../nav/Breadcrumbs";
import { MenuRow, OverflowGlyph, PopMenu } from "./Menu";
import "./chrome.css";
const SETTINGS = "Account and settings";
const NEW_CHAT = "New chat";
const SHARE_CHAT = "Share";
const COPY_CHAT = "Copy chat";
const DELETE_CHAT = "Delete chat";
const RENAME_CHAT = "Rename";
const WEB_APP = "Open web app";
const MORE = "More";
export type ChromeLinkId = "knowledge" | "lineage" | "artifacts";
/** What the settings menu can offer. The host says which of these it has wired;
 *  the wording and the order are the package's, so every surface carrying this
 *  chrome names the same action the same way. */
export type ChromeSettingsId = "plugins" | "jobs" | "preferences" | "logout";
interface SettingsItem {
  id: ChromeSettingsId;
  label: string;
  icon: ReactNode;
  /** Signing out ends the session; it reads as the consequence it is. */
  danger?: boolean;
}
/** Menu order: the workspace's own doors first, then preferences, then the one
 *  action that ends the session. */
const SETTINGS_ITEMS: SettingsItem[] = [
  { id: "plugins", label: "Plugins and connections", icon: <IconPlug size={13} stroke={1.8} aria-hidden /> },
  { id: "jobs", label: "Background jobs", icon: <IconListCheck size={13} stroke={1.8} aria-hidden /> },
  { id: "preferences", label: "Preferences", icon: <IconSettings size={13} stroke={1.8} aria-hidden /> },
  { id: "logout", label: "Sign out", icon: <IconLogout size={13} stroke={1.8} aria-hidden />, danger: true },
];
export interface ChromeLink {
  id: ChromeLinkId;
  label: string;
  /** How many objects the link leads to. Omit while the host has no answer:
   *  the badge then stays off rather than claiming zero. */
  count?: number;
}
/** One row the host puts in the header's overflow menu. The chrome owns where
 *  the row sits and how it reads; the host owns what it does and whether it is
 *  offered at all. */
export interface ChromeMenuAction {
  id: string;
  label: string;
  icon?: ReactNode;
  /** A row whose action destroys something reads in danger ink. */
  danger?: boolean;
  onSelect: () => void;
}
export interface ChromeProps {
  /** The chat's name, the surface's leading identity. */
  title: string;
  /** A glyph tight to the left of the name, for a surface whose title names a
   *  place rather than one chat. Decorative: the title already says what this
   *  is. */
  titleIcon?: ReactNode;
  /** Rename the chat by its own name. Wired, the title itself is the control:
   *  pressing it swaps the heading for a field holding the WHOLE name, Enter
   *  keeps it and Escape abandons it. Omitted — the editor's webview passes
   *  nothing — the title is plain text and there is no second control beside it
   *  to tell apart from the chat's own name.
   *
   *  Resolves when the new name has landed. A rejection reverts the heading to
   *  the name it had and reads its message out as the refusal, so the host
   *  answers with the reason the write was refused rather than a silent
   *  no-op. */
  onRenameTitle?: (title: string) => Promise<void>;
  /** The subordinate breadcrumb after the name; folded out under squeeze. */
  workspace?: string;
  /** Where this chat sits in the stack, outermost first and this chat last, in
   *  place of `title`. A chat opened from another one is a level, not a dead
   *  end: the trail names every level above it and reaches any of them in one
   *  press, rather than making a reader climb a rung at a time. A chat with
   *  nothing above it is the same shape with one crumb. */
  trail?: Crumb[];
  /** The working rail's quick links; without them the rail does not render. */
  links?: ChromeLink[];
  /** Actions the host puts in the three-dot menu, ahead of the links. They are
   *  a chat's own doors that earn no key of their own on the bar — saving it as
   *  a template is the first — so a host that wires any makes the three-dot
   *  menu their permanent home: unlike the links, which have the rail below and
   *  fold into this menu only at the narrowest width, these have nowhere else
   *  to be. */
  moreActions?: readonly ChromeMenuAction[];
  /** The signed-in account, named at the top of the settings menu. Omit while
   *  the host has none: the menu then opens straight onto its actions. */
  account?: { email: string; name?: string; plan?: string };
  /** Which settings actions the host has wired. The menu carries these and
   *  nothing else, so it never offers a door the host cannot open. Leads the
   *  actions: it is the one control here that changes how every chat runs, not
   *  just this one. */
  settings?: readonly ChromeSettingsId[];
  onSettingsAction?: (id: ChromeSettingsId) => void;
  /** Leaves for the browser. A standalone key rather than a menu row, because
   *  it is the one action here that goes somewhere else. Omit while the host
   *  does not know where the web app is. */
  onOpenWebApp?: () => void;
  onNewChat?: () => void;
  /** Lets more people into the open chat. Omit where there is nothing to share
   *  — no chat, no place to share it into (the editor's webview is exactly
   *  that host), or a reader who may not — and the key then does not render.
   *  The host owns whatever the press opens; this row only says where the key
   *  sits, which is beside the other action taken on the open chat. */
  onShareChat?: () => void;
  /** Where the open chat's workspace runs, drawn first in the action row. The
   *  host owns the control: only it can read the machine and move the
   *  workspace. */
  machine?: ReactNode;
  /** Copies the open chat: the owner's duplicate, or a copy into the reader's
   *  own drive. Omit where there is nothing to copy — no chat, no drive
   *  behind it — and the key then does not render. The host owns the wording
   *  (`copyChatLabel`), because only it knows whose chat this is. */
  onCopyChat?: () => void;
  /** What the copy key says; the host decides by ownership. */
  copyChatLabel?: string;
  /** Deletes the open chat. Omit where there is no chat to delete (home) or
   *  the reader may not; the key then does not render. The host owns the
   *  confirm step. */
  onDeleteChat?: () => void;
  onOpenLink?: (id: ChromeLinkId) => void;
}
/** Each rail link carries an icon keyed by its id, a discrete set: every link
 *  renders with its glyph or the set loses its all-or-none reading. */
const LINK_ICONS: Record<ChromeLinkId, ReactNode> = {
  knowledge: (
    <>
      <path d="M7 3.6C5.6 2.8 4 2.6 2.6 3v7.6c1.4-.4 3-.2 4.4.6 1.4-.8 3-1 4.4-.6V3c-1.4-.4-3-.2-4.4.6Z" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M7 3.6v7.6" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </>
  ),
  lineage: (
    <>
      <circle cx="3.4" cy="4" r="1.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <circle cx="10.6" cy="4" r="1.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <circle cx="7" cy="10.4" r="1.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <path d="M4.7 4.7 6.3 9M9.3 4.7 7.7 9" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </>
  ),
  artifacts: (
    <>
      <path d="M7 2.1 12 4.6v4.8L7 11.9 2 9.4V4.6Z" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M2 4.6 7 7.1l5-2.5M7 7.1v4.8" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </>
  ),
};
function LinkGlyph({ id }: { id: ChromeLinkId }): ReactElement {
  return (
    <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">{LINK_ICONS[id]}</svg>
  );
}
/** A tinted count badge that flashes once when its value increments; under
 *  prefers-reduced-motion the flash CSS swaps for a plain color step. Rendered
 *  only where a count is known. */
function CountBadge({ value }: { value: number }): ReactElement {
  const [flash, setFlash] = useState(false);
  const prev = useRef(value);
  useEffect(() => {
    if (value === prev.current) return;
    prev.current = value;
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 800);
    return () => clearTimeout(t);
  }, [value]);
  return (
    <span className="chat-link__count chat-num" data-flash={flash ? "" : undefined}>
      {value}
    </span>
  );
}
/** The title key, as an element `Text` can annotate.
 *
 *  `Text` types its extra props as a generic element's, which have no `type`,
 *  and a key that defaulted to `submit` inside any future form would post it.
 *  Rendering the button through a component settles the attribute here while
 *  the overflow measurement and the tip stay `Text`'s, so the heading keeps the
 *  one truncation contract every other reading on this row uses. */
function TitleButton(props: HTMLAttributes<HTMLElement>): ReactElement {
  return <button type="button" {...props} />;
}

/** What a refusal with nothing to say reads as. The host normally rejects with
 *  the server's own sentence; this is the floor under a rejection that carries
 *  none. */
const RENAME_REFUSED = "This chat could not be renamed.";

/** The chat's name, and — where the host wired one — the way to change it.
 *
 *  The name IS the control: there is no second key beside it for a reader to
 *  tell apart from the title itself. Pressing it (or reaching it by Tab and
 *  pressing Enter) swaps the heading for a field holding the WHOLE name rather
 *  than the clipped reading on screen, selected, so a long name is one
 *  keystroke from being replaced and scrolls under the caret for anyone editing
 *  it in place.
 *
 *  Leaving the field is the same decision as pressing Enter — a name that
 *  changed is kept, one that did not is abandoned — because a header that threw
 *  an edit away on a stray click reads as a bug, and one that saved a name
 *  nobody touched writes a version for nothing. Escape abandons outright. The
 *  field stays put and read-only while the write is in flight: swapping it back
 *  to the old heading mid-write reads as the rename having failed.
 */
/** How a chat's own actions open, wherever its name is drawn: a right-click on
 *  the identity row, or the keyboard's own menu request on whatever has focus
 *  in it. Nothing is drawn beside the name — the name IS the control. */
type ChatActionsTrigger = ReturnType<typeof useContextMenu>["triggerProps"];

function ChromeTitle({
  title,
  onRename,
  editing,
  setEditing,
  trigger,
}: {
  title: string;
  onRename?: (title: string) => Promise<void>;
  editing: boolean;
  setEditing: (editing: boolean) => void;
  /** Opens the chat's actions from the identity row, when the host wired any. */
  trigger?: ChatActionsTrigger;
}): ReactElement {
  const [draft, setDraft] = useState(title);
  const [pending, setPending] = useState(false);
  const [reason, setReason] = useState<string | null>(null);
  const field = useRef<HTMLInputElement | null>(null);

  // Selected on open, not on every commit: a 300-character name is then one
  // keystroke from being replaced, and the caret still lands inside the field's
  // own scroll for anyone who types instead.
  useLayoutEffect(() => {
    if (editing) field.current?.select();
  }, [editing]);

  const open = (): void => {
    setDraft(title);
    setReason(null);
    setEditing(true);
  };
  const abandon = (): void => {
    setEditing(false);
    setDraft(title);
  };
  const commit = (): void => {
    if (pending || !onRename) return;
    const next = draft.trim();
    // A name emptied is not a rename request: the chat keeps the name it had.
    if (next === "" || next === title) {
      abandon();
      return;
    }
    setPending(true);
    void onRename(next).then(
      () => {
        setPending(false);
        setEditing(false);
      },
      (error: unknown) => {
        setPending(false);
        setEditing(false);
        setDraft(title);
        setReason(error instanceof Error && error.message ? error.message : RENAME_REFUSED);
      },
    );
  };

  if (editing) {
    return (
      <span className="chat-chrome__title-edit">
        <input
          ref={field}
          type="text"
          className="chat-chrome__title-field"
          aria-label="Chat title"
          autoFocus
          readOnly={pending}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            // The transcript below listens for keys of its own; an edit of the
            // header is not a message.
            event.stopPropagation();
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            } else if (event.key === "Escape") {
              event.preventDefault();
              abandon();
            }
          }}
        />
      </span>
    );
  }

  return (
    <>
      {/* The actions open from the name itself — a right-click across the row,
          or the menu key on the name that has focus. */}
      <span className="chat-chrome__title-row" {...trigger}>
        <h1 className="chat-chrome__title">
          {onRename ? (
            <Text
              as={TitleButton}
              className="chat-chrome__title-button"
              tooltip="truncate"
              tooltipLabel={title}
              aria-label={`Rename chat: ${title}`}
              onClick={open}
            >
              {title}
            </Text>
          ) : (
            <Text className="chat-chrome__title-plain" tooltip="truncate">
              {title}
            </Text>
          )}
        </h1>
      </span>
      {reason !== null ? (
        <span className="chat-chrome__title-error" role="alert">
          {reason}
        </span>
      ) : null}
    </>
  );
}

export function Chrome({
  title,
  titleIcon,
  onRenameTitle,
  workspace,
  trail,
  links,
  moreActions,
  account,
  settings,
  onSettingsAction,
  onOpenWebApp,
  onNewChat,
  onShareChat,
  machine,
  onCopyChat,
  copyChatLabel = COPY_CHAT,
  onDeleteChat,
  onOpenLink,
}: ChromeProps): ReactElement {
  const settingsItems = SETTINGS_ITEMS.filter((item) => settings?.includes(item.id));
  // The name's own state lives here rather than inside the title, because a
  // chat opened from another one draws its name as the last crumb of a trail
  // and still has to offer the same three actions — including the rename, which
  // swaps the whole identity row for the field.
  const [renaming, setRenaming] = useState(false);
  const titleMenu = useContextMenu();
  const chatName = trail ? (trail[trail.length - 1]?.label ?? title) : title;
  // The same three actions the rail offers on a row, on the chat that is open,
  // in the product's one right-click menu. A host that wired none of them gets
  // no menu rather than an empty one.
  const chatActions: ContextMenuItem[] = [
    ...(onRenameTitle
      ? [
          {
            id: "rename",
            label: RENAME_CHAT,
            icon: <IconPencil size={13} stroke={1.8} aria-hidden />,
            onSelect: () => setRenaming(true),
          },
        ]
      : []),
    ...(onCopyChat
      ? [
          {
            id: "copy",
            label: copyChatLabel,
            icon: <IconCopy size={13} stroke={1.8} aria-hidden />,
            onSelect: onCopyChat,
          },
        ]
      : []),
    ...(onDeleteChat
      ? [
          {
            id: "delete",
            label: DELETE_CHAT,
            icon: <IconTrash size={13} stroke={1.8} aria-hidden />,
            tone: "destructive" as const,
            onSelect: onDeleteChat,
          },
        ]
      : []),
  ];
  const hasChatActions = chatActions.length > 0;
  const titleTrigger = hasChatActions ? titleMenu.triggerProps : undefined;
  // The overflow menu's own rows: the chat's doors that earn no key on the bar.
  // Renaming leads them and is the chrome's own — the name IS the field, so
  // only the chrome can open it — and a reader who never right-clicks the title
  // has nowhere else to find it.
  const overflowActions: ChromeMenuAction[] = [
    ...(onRenameTitle
      ? [
          {
            id: "rename",
            label: RENAME_CHAT,
            icon: <IconPencil size={13} stroke={1.8} aria-hidden />,
            onSelect: () => setRenaming(true),
          },
        ]
      : []),
    ...(moreActions ?? []),
  ];
  const railLinks = links ?? [];
  return (
    <header className="chat-chrome">
      <div className="chat-chrome__bar">
        <div className="chat-chrome__id">
          {trail && !renaming ? (
            // The crumb naming this chat is the heading, not a control, so the
            // row itself takes the focus the keyboard needs to ask for the
            // menu — otherwise the trail's actions are pointer-only.
            <span
              className="chat-chrome__title-row"
              {...titleTrigger}
              {...(titleTrigger ? { tabIndex: 0, role: "group", "aria-label": chatName } : {})}
            >
              <Breadcrumbs crumbs={trail} label="Chat trail" heading />
            </span>
          ) : (
            <>
              {titleIcon ? <span className="chat-chrome__title-icon" aria-hidden="true">{titleIcon}</span> : null}
              {/* The name carries the chat's own actions: renaming it, copying
                  it, throwing it away. A host that wired none of the three
                  leaves a plain heading with nothing to press. */}
              {hasChatActions ? (
                <ChromeTitle
                  // A chat renamed from its trail shows the field in place of
                  // the whole trail, so the name being retyped has the row.
                  title={chatName}
                  onRename={onRenameTitle}
                  editing={renaming}
                  setEditing={setRenaming}
                  trigger={titleTrigger}
                />
              ) : (
                <Text as="h1" className="chat-chrome__title" tooltip="truncate">
                  {title}
                </Text>
              )}
              {workspace && !trail ? (
                <>
                  <span className="chat-chrome__crumb" aria-hidden="true">·</span>
                  <Text className="chat-chrome__ws" tooltip="truncate">
                    {workspace}
                  </Text>
                </>
              ) : null}
            </>
          )}
        </div>
        <div className="chat-chrome__actions">
          {machine}
          {onOpenWebApp ? (
            <button type="button" className="chat-icon" title={WEB_APP} aria-label={WEB_APP} onClick={onOpenWebApp}>
              <IconWorld size={16} stroke={1.8} aria-hidden />
            </button>
          ) : null}
          {settingsItems.length > 0 ? (
            <PopMenu
              label={SETTINGS}
              glyph={<IconSettings size={16} stroke={1.8} aria-hidden />}
              header={
                account ? (
                  <>
                    {account.name ? <span className="chat-chrome__acct-name">{account.name}</span> : null}
                    <Text className="chat-chrome__acct-email" tooltip="truncate">
                      {account.email}
                    </Text>
                    {account.plan ? <span className="chat-chrome__acct-plan">{account.plan}</span> : null}
                  </>
                ) : null
              }
            >
              {(close) =>
                settingsItems.map((item) => (
                  <MenuRow
                    key={item.id}
                    id={item.id}
                    icon={item.icon}
                    label={item.label}
                    danger={item.danger}
                    onSelect={() => {
                      close();
                      onSettingsAction?.(item.id);
                    }}
                  />
                ))
              }
            </PopMenu>
          ) : null}
          {onNewChat ? (
            <button type="button" className="chat-icon" title={NEW_CHAT} aria-label={NEW_CHAT} onClick={onNewChat}>
              <IconPencil size={16} stroke={1.8} aria-hidden />
            </button>
          ) : null}
          {/* Before Delete: reaching the header by keyboard passes what lets
              people in before what throws the chat away. */}
          {onShareChat ? (
            <button type="button" className="chat-icon" title={SHARE_CHAT} aria-label={SHARE_CHAT} onClick={onShareChat}>
              <IconShare size={16} stroke={1.8} aria-hidden />
            </button>
          ) : null}
          {/* After Share, before Delete: what makes another chat out of this
              one sits between letting people in and throwing it away. */}
          {onCopyChat ? (
            <button type="button" className="chat-icon" title={copyChatLabel} aria-label={copyChatLabel} onClick={onCopyChat}>
              <IconCopy size={16} stroke={1.8} aria-hidden />
            </button>
          ) : null}
          {onDeleteChat ? (
            <button type="button" className="chat-icon" title={DELETE_CHAT} aria-label={DELETE_CHAT} onClick={onDeleteChat}>
              <IconTrash size={16} stroke={1.8} aria-hidden />
            </button>
          ) : null}
          {overflowActions.length > 0 || railLinks.length > 0 ? (
            // Carrying links alone, the menu is the width-of-last-resort home
            // the rail folds into and stays hidden until then. Carrying an
            // action, it is that action's ONLY home, so the class that hides it
            // is withheld.
            <PopMenu
              {...(overflowActions.length > 0 ? {} : { className: "chat-chrome__more" })}
              label={MORE}
              glyph={<OverflowGlyph />}
            >
              {(close) => (
                <>
                  {overflowActions.map((action) => (
                    <MenuRow
                      key={action.id}
                      id={action.id}
                      icon={action.icon}
                      label={action.label}
                      danger={action.danger}
                      onSelect={() => {
                        close();
                        action.onSelect();
                      }}
                    />
                  ))}
                  {railLinks.map((link) => (
                    <MenuRow
                      key={link.id}
                      icon={<LinkGlyph id={link.id} />}
                      label={link.label}
                      trailing={typeof link.count === "number" ? <CountBadge value={link.count} /> : undefined}
                      onSelect={() => {
                        close();
                        onOpenLink?.(link.id);
                      }}
                    />
                  ))}
                </>
              )}
            </PopMenu>
          ) : null}
        </div>
      </div>
      {/* The rail owns the row below the identity bar rather than competing with
          the title for one row's width. A Tooltip names each link once its
          label has folded out. */}
      {links && links.length > 0 ? (
        <nav className="chat-chrome__rail" aria-label="Project">
          {links.map((link) => (
            <Tooltip key={link.id} label={link.label}>
              {(tp) => (
                <button type="button" className="chat-link" aria-label={link.label} onClick={() => onOpenLink?.(link.id)} {...tp}>
                  <span className="chat-link__icon"><LinkGlyph id={link.id} /></span>
                  <span className="chat-link__label">{link.label}</span>
                  {typeof link.count === "number" ? <CountBadge value={link.count} /> : null}
                </button>
              )}
            </Tooltip>
          ))}
        </nav>
      ) : null}
      {hasChatActions ? (
        <ContextMenu {...titleMenu.menuProps} items={chatActions} label={`Actions for ${chatName}`} />
      ) : null}
    </header>
  );
}
