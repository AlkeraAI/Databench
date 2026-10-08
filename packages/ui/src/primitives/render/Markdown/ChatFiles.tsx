// How a transcript reaches the files inside its own chat folder.
//
// The markdown names a chat-root-relative path (`outputs/revenue.png`,
// `scratch/report.csv`); turning that into something a browser can load is the
// SHELL's business — the web asks Files for the node and its content URL, the
// VS Code webview asks its host for a `webview.asWebviewUri` of the local chat
// folder. So the renderer takes one resolver through context, installed once
// per shell around the transcript, and every image and file link in every
// message (the agent's and the reader's own) goes through it. No resolver
// installed means every image is a placeholder and every chat link that has no
// other routing is its label in plain words — a surface that has nowhere to
// load from never spins forever, and never shows a control that does nothing.
//
// A shell that can also say WHERE the file is (`locate`) turns each reference
// into a live one: the reader sees which file it is before clicking, clicking
// puts that file in front of them in their own file browser, and a reference to
// a file that is not there says which of two things that is instead of
// pretending to be a door: a file this transcript saw that has since been
// deleted, or one it has never seen. "Not arrived yet" is a state only a
// message still being written can be in: the box publishes a finished message
// only once the files it names are on the drive, so a finished message whose
// file the drive does not have names a file that is not coming (the agent
// never wrote it, or deleted it before this reader looked). That reference
// says so and stops waiting; it still becomes the file if the folder later has
// it.
// A shell without `locate` — the webview, which can only hand a path back to its
// host — is unchanged, so the two surfaces never diverge by accident.

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { doublingDelay } from "../../../backoff";
import { chatRelativePath, resolveChatPath } from "./chatPaths";

/** One file inside the chat's folder, as the shell found it back from a path a
 *  message named. `parentId` is the folder it sits in — null for a shell that
 *  can answer the node but not its place. */
export interface ChatFileRef {
  nodeId: string;
  parentId: string | null;
  name: string;
  path: string;
}

/** What `resolveUrl` rejects with when the file is there but its bytes have not
 *  reached anywhere the shell can read them yet (the drive is still bringing
 *  them from the machine that wrote them). It is "wait", not "nothing": the
 *  image shows a loading state and asks again when the shell next says the
 *  folder changed. */
export class ChatFileNotReady extends Error {
  constructor(message = "the file's bytes have not landed yet") {
    super(message);
    this.name = "ChatFileNotReady";
  }
}

export interface ChatFilesResolver {
  /** A URL the shell can put in an `<img src>` for this chat-relative path, or
   *  `null` when nothing is there. Rejecting with `ChatFileNotReady` says the
   *  bytes are on their way; any other rejection counts as `null`. */
  resolveUrl(path: string): Promise<string | null>;
  /** The file a chat-relative path names, or `null` when nothing is there any
   *  more. Only a shell that can look a path up offers it; without it a
   *  reference renders exactly as it always has. */
  locate?(path: string): Promise<ChatFileRef | null>;
  /** Put a located file in front of the reader in this shell's own file browser.
   *  Absent leaves a located reference opening through `openPath`. */
  reveal?(ref: ChatFileRef): void;
  /** Open the file the path names, where the shell has somewhere to open it —
   *  the Files viewer on the web, the editor in VS Code. Absent leaves images
   *  unclickable and chat links as labels. */
  openPath?(path: string): void;
}

/** What a reference renders as while, and after, the shell has been asked where
 *  the file is. `unasked` is both "no resolver" and "this shell cannot look a
 *  path up" — the cases that render the reference as written. A path the shell cannot
 *  find is `gone` when this provider has seen the file there before —
 *  "deleted" is a claim about a file that existed. Otherwise it is `pending`
 *  while the message naming it is still being written (named, never found,
 *  expected to turn up) and `missing` once that message is finished: the file
 *  is not coming, and the reference must not go on saying it is. */
type RefState =
  | { kind: "unasked" }
  | { kind: "asking" }
  | { kind: "here"; ref: ChatFileRef }
  | { kind: "pending" }
  | { kind: "missing" }
  | { kind: "gone" };

type AbsentKind = "gone" | "pending" | "missing";

/** What a reference to a file that is no longer in the chat says. */
export const CHAT_FILE_GONE_NOTE = "not in the chat any more";

/** What a reference to a file that has not been found yet says, while the
 *  message naming it is still being written. */
export const CHAT_FILE_PENDING_NOTE = "has not arrived";

/** What a finished message's reference to a file the chat does not have says. */
export const CHAT_FILE_MISSING_NOTE = "not in the chat";

const ABSENT_NOTES: Record<AbsentKind, string> = {
  gone: CHAT_FILE_GONE_NOTE,
  pending: CHAT_FILE_PENDING_NOTE,
  missing: CHAT_FILE_MISSING_NOTE,
};

const isAbsent = (state: RefState): state is { kind: AbsentKind } =>
  state.kind === "gone" || state.kind === "pending" || state.kind === "missing";

/** Whether the message these references sit in is still being written. Only
 *  such a message may say a file "has not arrived": a finished one was held
 *  back until its files were on the drive. Off unless a renderer says so, so a
 *  surface that never streams never shows a wait that cannot end. */
const ChatFilesStreamingContext = createContext(false);

/** Mark the references inside as belonging to a message still being written
 *  (`streaming`) or a finished one. The prose renderers wrap each message in
 *  it. */
export function ChatFilesStreaming({ streaming, children }: { streaming: boolean; children: ReactNode }): ReactNode {
  return <ChatFilesStreamingContext.Provider value={streaming}>{children}</ChatFilesStreamingContext.Provider>;
}

interface ChatFilesContextValue {
  resolver: ChatFilesResolver | null;
  /** The chat the transcript is, when the shell knows it: what decides
   *  whether a box's absolute path (`…/.alkera/chats/<chat>/…`) is this
   *  chat's file or someone else's. */
  chatId: string | null;
  /** One lookup per path per provider: a transcript naming the same report in
   *  five messages asks the shell once. */
  located: Map<string, Promise<ChatFileRef | null>>;
  /** Every path the shell has answered with a file, for the life of this
   *  resolver. Kept across `invalidate`, which is exactly when a file that was
   *  here can stop being here. */
  seen: Set<string>;
  /** The shell's `invalidate`, as given: what an image waiting for its bytes
   *  keys its next ask on, so it retries on the folder's own signal rather than
   *  on a timer. */
  epoch: number | undefined;
}

const ChatFilesContext = createContext<ChatFilesContextValue>({
  resolver: null,
  chatId: null,
  located: new Map(),
  seen: new Set(),
  epoch: undefined,
});

/** The file each held result was written out to, by handle, as the tool that
 *  wrote it reported the path. Its own context, because it grows with every
 *  streamed turn and the path lookups above must not be dropped each time. */
const ChatResultFilesContext = createContext<ReadonlyMap<string, string> | null>(null);

/** Install the shell's resolver around a transcript (and around the reader's
 *  own bubbles — a pasted image renders through the same door).
 *
 *  `invalidate` is the shell's way of saying the folder has changed underneath
 *  the transcript: bumping it drops the memo so every reference asks again.
 *
 *  `chatId` lets a box's absolute path into this chat's folder resolve (and
 *  keeps one into any other chat's from resolving); `resultFiles` is where each
 *  held result the transcript names was written out to. */
export function ChatFilesProvider({
  resolver,
  invalidate,
  chatId = null,
  resultFiles = null,
  children,
}: {
  resolver: ChatFilesResolver | null;
  invalidate?: number;
  chatId?: string | null;
  resultFiles?: ReadonlyMap<string, string> | null;
  children: ReactNode;
}): ReactNode {
  // What was seen is a fact about THIS resolver's chat, so it outlives an
  // invalidation but not a change of resolver.
  const seen = useMemo(() => new Set<string>(), [resolver]);
  const value = useMemo<ChatFilesContextValue>(
    () => ({ resolver, chatId, located: new Map(), seen, epoch: invalidate }),
    // A new memo per resolver AND per invalidation: the map is keyed by path
    // only, so it may not outlive either.
    [resolver, chatId, invalidate, seen],
  );
  return (
    <ChatFilesContext.Provider value={value}>
      <ChatResultFilesContext.Provider value={resultFiles}>{children}</ChatResultFilesContext.Provider>
    </ChatFilesContext.Provider>
  );
}

export function useChatFiles(): ChatFilesResolver | null {
  return useContext(ChatFilesContext).resolver;
}

/** A lookup that failed (the shell's read was refused or never answered), as
 *  opposed to one that answered "not there". */
const LOOKUP_FAILED = Symbol("lookup failed");

/** How long a file that was here and is now not found is given to come back
 *  before the reference says it is gone. A save that writes a new copy and
 *  renames it over the old one leaves the folder without the file for a
 *  moment, and a listing read in that moment is not the file being deleted. */
export const CHAT_FILE_GONE_CONFIRM_MS = 800;

/** How many times a lookup that failed is asked again, and the first wait. */
const LOOKUP_RETRIES = 3;
const LOOKUP_RETRY_MS = 2_000;
/** A finished message's "nowhere" is asked again this many times, doubling
 *  from {@link MISSING_RECHECK_MS}: a file the machine wrote a moment ago may
 *  not have reached the drive when the reference first looked, and the folder
 *  change that would correct it can arrive before the page listens for it. */
export const MISSING_RECHECKS = 4;
export const MISSING_RECHECK_MS = 1_500;

/** Where the shell says this path is, asked once per path.
 *
 *  Asking again after an invalidation keeps the last answer on screen until the
 *  new one lands: the folder changes many times while an agent writes, and a
 *  reference that went back to "asking" on each change would blink. Only a new
 *  path or a new lookup starts from "asking".
 *
 *  "Not in the chat any more" is a claim that the file was deleted, so it is
 *  made only on evidence: a lookup that failed is not evidence (it keeps the
 *  last answer and asks again on a climbing wait), and a file that was here is
 *  looked for once more after a beat before it is called gone. */
function useChatFileRef(path: string | null): RefState {
  const { resolver, located, seen } = useContext(ChatFilesContext);
  const streaming = useContext(ChatFilesStreamingContext);
  const locate = path === null ? undefined : resolver?.locate;
  const [state, setState] = useState<RefState>(() => (locate ? { kind: "asking" } : { kind: "unasked" }));
  // Bumped to ask again outside an invalidation: a failed lookup's retry, or
  // the second look before a seen file is called gone.
  const [again, setAgain] = useState(0);
  const retries = useRef(0);
  const missRechecks = useRef(0);
  const confirming = useRef(false);
  const answered = useRef<{ locate: ChatFilesResolver["locate"]; path: string; streaming: boolean } | null>(
    null,
  );
  useEffect(() => {
    if (!locate || path === null) {
      answered.current = null;
      setState({ kind: "unasked" });
      return;
    }
    let live = true;
    const last = answered.current;
    const same = last?.locate === locate && last.path === path;
    if (!same) {
      answered.current = null;
      retries.current = 0;
      missRechecks.current = 0;
      confirming.current = false;
      setState({ kind: "asking" });
    }
    if (same && last.streaming && !streaming) {
      // The message just finished. What it was told while it was still being
      // written may predate the file; ask once more before calling the file
      // missing, so the end of a message never reads a stale "nothing".
      located.delete(path);
    }
    let pending = located.get(path) as Promise<ChatFileRef | null | typeof LOOKUP_FAILED> | undefined;
    if (!pending) {
      // A rejection is held as a failure, not as "nowhere": it is not repeated
      // on every render, but it is not an answer either.
      pending = locate(path).catch(() => LOOKUP_FAILED);
      located.set(path, pending as Promise<ChatFileRef | null>);
    }
    const asked = pending;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const askAgainIn = (ms: number): void => {
      timer = setTimeout(() => {
        if (located.get(path) === asked) located.delete(path);
        setAgain((n) => n + 1);
      }, ms);
    };
    void asked.then((ref) => {
      if (ref && ref !== LOOKUP_FAILED) seen.add(path);
      if (!live) return;
      if (ref === LOOKUP_FAILED) {
        // Whatever the reference last showed stays; a reference never answered
        // reads as not found yet, which a later answer replaces.
        if (answered.current === null) {
          answered.current = { locate, path, streaming };
          setState(streaming ? { kind: "pending" } : { kind: "missing" });
        }
        if (retries.current < LOOKUP_RETRIES) {
          askAgainIn(doublingDelay(retries.current, LOOKUP_RETRY_MS));
          retries.current += 1;
        }
        return;
      }
      retries.current = 0;
      answered.current = { locate, path, streaming };
      if (ref) {
        confirming.current = false;
        missRechecks.current = 0;
        setState({ kind: "here", ref });
      } else if (seen.has(path)) {
        if (!confirming.current) {
          confirming.current = true;
          askAgainIn(CHAT_FILE_GONE_CONFIRM_MS);
          return;
        }
        confirming.current = false;
        setState({ kind: "gone" });
      } else {
        setState(streaming ? { kind: "pending" } : { kind: "missing" });
        if (!streaming && missRechecks.current < MISSING_RECHECKS) {
          askAgainIn(doublingDelay(missRechecks.current, MISSING_RECHECK_MS));
          missRechecks.current += 1;
        }
      }
    });
    return () => {
      live = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [locate, located, seen, path, streaming, again]);
  return state;
}

/** What the reader is told a reference points at: the file's own name and the
 *  folder inside the chat it sits in. The path the message wrote is what names
 *  the folder — a node id would mean nothing to a person. */
export function chatFileTitle(ref: ChatFileRef): string {
  const cut = ref.path.lastIndexOf("/");
  const folder = cut > 0 ? ref.path.slice(0, cut) : null;
  return `${ref.name} (in ${folder ?? "the chat's folder"})`;
}

/** The rendering for a reference whose file the shell could not find: the label
 *  as plain text, and a note saying why it is not a door — deleted, not
 *  arrived yet, or not in the chat at all. */
function Absent({ label, kind }: { label: string; kind: AbsentKind }): ReactNode {
  return (
    <>
      <span className={`chat-file-ref chat-file-ref--${kind}`}>{label}</span>{" "}
      <span className="chat-file-ref__note">{ABSENT_NOTES[kind]}</span>
    </>
  );
}

/** What one image shows, for one resolver and one path.
 *
 *  `url` is what the `<img>` is pointed at. Once there is one, only another URL
 *  replaces it: a re-ask after the folder changed, a lookup still in flight, an
 *  answer of nothing or a failure while asking again all leave the picture the
 *  reader already has where it is. The one way back to no picture is the shell
 *  saying the file itself is gone. `waiting` is what shows while there is no
 *  URL: a quiet loading block while the answer (or the bytes) are on the way,
 *  the labelled placeholder once the answer is that nothing is there. `loaded`
 *  is the URL the browser last actually drew. */
interface ImageState {
  resolver: ChatFilesResolver | null;
  path: string;
  url: string | null;
  loaded: string | null;
  waiting: "loading" | "missing";
}

const freshImage = (resolver: ChatFilesResolver | null, path: string): ImageState => ({
  resolver,
  path,
  url: null,
  loaded: null,
  waiting: resolver ? "loading" : "missing",
});

/** One chat image. The `alt` is the accessible name and the fallback the
 *  reader sees when the path resolves to nothing — never a spinner that stays.
 *  While the answer or the file's bytes are on the way it is a quiet loading
 *  block with no text, and once a picture is on screen it stays there until a
 *  newer one replaces it: the chat folder changes many times while an agent
 *  writes, and each change is a re-ask, never a reason to blink.
 *
 *  An image whose bytes have not landed (`ChatFileNotReady`) asks again on the
 *  shell's next `invalidate` — the drive says the folder changed when they land
 *  — and not on a timer of its own.
 *
 *  Every type on `CHAT_IMAGE_EXTENSIONS`, SVG included, goes through `<img>`:
 *  an inline `<svg>` would be live DOM on the chat's origin (scripts, event
 *  handlers, external references, all written by whoever produced the file),
 *  while a browser renders an `<img>` of an SVG as a static picture with
 *  scripting off. */
export function ChatImage({
  alt,
  path: written,
  name,
}: {
  alt: string;
  /** The chat path the message wrote: relative and already normalized, or a
   *  box's absolute path, which resolves only when it is into this chat. */
  path: string;
  /** What the placeholder calls the file when there is no picture to show.
   *  Defaults to `alt`, which is right for a caption the writer chose; a
   *  reader's own attachment carries the composer's pill number instead
   *  ("Image 1"), which names nothing they could look for, so the ticket
   *  passes the file's name. */
  name?: string;
}): ReactNode {
  const { chatId, epoch, resolver: shell } = useContext(ChatFilesContext);
  // A box path into another chat names nothing this transcript may show, so it
  // is asked about as if no shell were installed: the labelled placeholder.
  const inChat = resolveChatPath(written, chatId);
  const path = inChat ?? written;
  const resolver = inChat === null ? null : shell;
  const where = useChatFileRef(inChat);
  // A shell that can look the path up decides whether there are bytes to buy:
  // asking for a file it has already said is gone would be a request for
  // nothing, and its answer would be the same placeholder.
  const gone = where.kind === "gone";
  const buy = resolver !== null && where.kind !== "asking" && !isAbsent(where);
  const [stored, setStored] = useState<ImageState>(() => freshImage(resolver, path));
  const mine = (candidate: ImageState): boolean => candidate.resolver === resolver && candidate.path === path;
  const state = mine(stored) ? stored : freshImage(resolver, path);

  useEffect(() => {
    const own = (prev: ImageState): ImageState =>
      prev.resolver === resolver && prev.path === path ? prev : freshImage(resolver, path);
    if (!resolver) return;
    if (gone) {
      // The file was deleted: the picture of it goes with it.
      setStored(freshImage(resolver, path));
      return;
    }
    if (!buy) return;
    let live = true;
    resolver
      .resolveUrl(path)
      .then((url) => {
        if (!live) return;
        setStored((prev) => {
          const base = own(prev);
          if (url) return base.url === url ? base : { ...base, url };
          return base.url ? base : { ...base, waiting: "missing" };
        });
      })
      .catch((error: unknown) => {
        if (!live) return;
        setStored((prev) => {
          const base = own(prev);
          if (base.url) return base;
          return { ...base, waiting: error instanceof ChatFileNotReady ? "loading" : "missing" };
        });
      });
    return () => {
      live = false;
    };
    // `epoch` is the retry signal: an image waiting on its bytes (or showing a
    // version the agent may since have rewritten) asks again when the folder
    // changes, and at no other time.
  }, [resolver, buy, gone, path, epoch]);

  const label = name ?? alt;
  if (isAbsent(where)) {
    return (
      <figure className="alk-chat-image" data-state={where.kind}>
        <Absent label={label} kind={where.kind} />
      </figure>
    );
  }
  const here = where.kind === "here" ? where.ref : null;
  const title = here ? chatFileTitle(here) : path;
  const open = here && resolver?.reveal ? () => resolver.reveal?.(here) : resolver?.openPath;
  if (state.url === null) {
    return state.waiting === "loading" ? (
      <figure className="alk-chat-image" data-state="resolving">
        <span className="alk-chat-image__loading" role="img" aria-label={label} aria-busy="true" title={title} />
      </figure>
    ) : (
      <figure className="alk-chat-image" data-state="missing">
        <span className="alk-chat-image__missing" role="img" aria-label={label} title={title}>
          {label}
        </span>
      </figure>
    );
  }
  const url = state.url;
  const img = (
    <img
      className="alk-chat-image__img"
      src={url}
      alt={alt}
      title={title}
      loading="lazy"
      onLoad={() => setStored((prev) => (mine(prev) && prev.url === url ? { ...prev, loaded: url } : prev))}
      // A picture the browser has drawn once is never swapped for the
      // placeholder by a later failure; only one that never drew is missing.
      onError={() =>
        setStored((prev) =>
          mine(prev) && prev.url === url && prev.loaded === null ? { ...prev, url: null, waiting: "missing" } : prev,
        )
      }
    />
  );
  return (
    <figure className="alk-chat-image" data-state="ready">
      {open ? (
        <button
          type="button"
          className="alk-chat-image__open"
          aria-label={`Open ${alt}`}
          onClick={() => (here && resolver?.reveal ? resolver.reveal(here) : resolver?.openPath?.(path))}
        >
          {img}
        </button>
      ) : (
        img
      )}
    </figure>
  );
}

/** A link that goes nowhere on this shell: its label, as the words they are.
 *  Never a control — a button that answers a click with nothing reads as a
 *  broken product, where plain words read as a name. */
function Label({ label, title }: { label: string; title?: string }): ReactNode {
  return (
    <span className="alk-markdown-label" title={title}>
      {label}
    </span>
  );
}

/** A markdown link whose target may be a file in the chat's folder, as the
 *  message wrote it (`report.csv`, `./a%20b.csv`, the box's absolute path).
 *
 *  A target that names a chat file opens through the shell's resolver where
 *  one is installed; with none it falls back to the caller's link routing
 *  (`onOpen`), which is what every other link gets. A target that is NOT a chat
 *  file — another chat's folder, a system path, a `..` escape — keeps the
 *  caller's routing only on a shell with a place of its own to open paths (the
 *  editor, which has no `locate`). On a shell whose files ARE the chat's (the
 *  web, which looks paths up in the chat folder), there is nothing it could
 *  open, and so is any link with no routing at all: both are plain words. */
export function ChatFileLink({
  label,
  target,
  onOpen,
}: {
  label: string;
  target: string;
  onOpen: (() => void) | undefined;
}): ReactNode {
  const { resolver, chatId } = useContext(ChatFilesContext);
  const path = chatRelativePath(target, chatId);
  const where = useChatFileRef(path);
  if (path === null) {
    const fallback = resolver?.locate ? undefined : onOpen;
    if (!fallback) return <Label label={label} title={target} />;
    return (
      <button type="button" className="alk-markdown-link" onClick={fallback} title={target}>
        {label}
      </button>
    );
  }
  if (isAbsent(where)) return <Absent label={label} kind={where.kind} />;
  const here = where.kind === "here" ? where.ref : null;
  const open =
    here && resolver?.reveal
      ? () => resolver.reveal?.(here)
      : resolver?.openPath
        ? () => resolver.openPath?.(path)
        : onOpen;
  if (!open) return <Label label={label} title={path} />;
  return (
    <button
      type="button"
      className={`alk-markdown-link alk-chat-file-link chat-file-ref${here ? " chat-file-ref--live" : ""}`}
      data-chat-path={path}
      onClick={open}
      title={here ? chatFileTitle(here) : path}
    >
      {label}
    </button>
  );
}

/** The file a held result was written out to, if the shell knows one. */
export function useChatResultFile(handle: string): string | null {
  return useContext(ChatResultFilesContext)?.get(handle) ?? null;
}

/** A `[label](blob:<handle>)` reference on a surface that cannot open the held
 *  result itself (the web, where the result stayed on the machine that made
 *  it). When the agent wrote that result out to a file in the chat — which is
 *  what `blob.materialize` does, and what it did before naming the result —
 *  the reference opens that file, exactly like a link to it would. Otherwise it
 *  is the label, as plain words: never a chip that looks like it opens. */
export function ChatResultLink({ label, handle, title }: { label: string; handle: string; title?: string }): ReactNode {
  const file = useChatResultFile(handle);
  if (file === null) {
    return (
      <span className="alk-markdown-blobref" title={title}>
        {label}
      </span>
    );
  }
  return <ChatFileLink label={label} target={file} onOpen={undefined} />;
}
