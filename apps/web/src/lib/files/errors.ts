/**
 * The one mapping from a Files API refusal to the sentence a person reads.
 *
 * Every refusal the browser can provoke arrives as an {@link ApiError} carrying the
 * server's envelope `code` (and its `status` for the ones with no code of their own,
 * such as the storage quota's 507). A page renders the copy this returns INLINE, next
 * to the thing that was refused — a generic banner would lose the one fact that makes
 * the refusal actionable: who is holding it, which folder granted the access, how much
 * room is left.
 *
 * The mapping lives here rather than beside each call site so a new refusal code is a
 * row in one table, and so two screens can never disagree about what `files.held`
 * means. A code with no row falls through to the server's own message, which is
 * already user-safe, and only then to a generic sentence.
 */

import { ApiError } from "@/api/errors";
import { capitalize } from "@/lib/format/text";

import { formatSize } from "./columns";

/** What a refusal looks like on screen: one short sentence, one optional detail. */
export interface FilesErrorCopy {
  /** The canonical envelope code, or `"error"` when the refusal carried none. */
  code: string;
  /** The headline. Always a full sentence, never a code. */
  title: string;
  /** The reason, when there is a second sentence worth reading. */
  detail?: string;
  /** True when the refusal is worth retrying once the situation changes. */
  retryable: boolean;
}

/**
 * Facts the page knows that the envelope does not carry.
 *
 * The error envelope is deliberately thin — it never echoes a body — so the holder of
 * a lease and the ancestor that granted access come from the facets the page already
 * read. Both are optional: the copy degrades to a true, if vaguer, sentence when the
 * page has nothing to name.
 */
export interface FilesErrorContext {
  /** The lease holder's display name, from the `lease` facet. */
  holder?: string;
  /** The machine the holder took it on, when the facet named one. */
  machine?: string;
  /** How the holder stands to the reader, from the `lease` facet: the reader
   *  themselves, the box running their own chat, or a box they run. With it,
   *  `holder` is the chat's title or the box's name. */
  yours?: "you" | "chat" | "box";
  /** The ancestor folder an inherited grant comes from. */
  grantedBy?: string;
  /** What the caller was trying to do, e.g. `"move"` — used in the held copy. */
  action?: string;
}

/** The storage-quota refusal has no code of its own; HTTP 507 is the whole signal. */
export const QUOTA_STATUS = 507;

type Builder = (context: FilesErrorContext, error: ApiError) => Omit<FilesErrorCopy, "code">;

/**
 * The server's own sentence, or `null` when it never sent one.
 *
 * {@link ApiError} synthesizes `"<fallback> (409)"` for a body it could not parse, so a
 * raw `error.message` is not evidence that the server said anything — and putting that
 * placeholder in front of product copy is exactly the jargon this module exists to keep
 * off the screen.
 */
function serverSentence(error: ApiError): string | null {
  const message = error.message.trim();
  if (!message || /\(\d{3}\)$/.test(message)) return null;
  return message;
}

/**
 * A row for one of the "no room left" refusals.
 *
 * The server's own message carries the figures ("2.0 TB of 2.0 TB used"), which is the
 * one fact that tells a person whether deleting something will help, so it is kept in
 * front of the advice rather than replaced by it.
 */
function quotaCopy(title: string, who = "an org admin"): Builder {
  return (_context, error) => {
    const said = serverSentence(error);
    return {
      title,
      detail: said
        ? `${said} Free up space in the trash, or ask ${who} to raise the limit.`
        : `Free up space in the trash, or ask ${who} to raise the limit.`,
      retryable: false,
    };
  };
}

/** A byte figure off a refusal's detail bag, or null when it did not carry one. */
function bytesIn(detail: Readonly<Record<string, unknown>> | null, key: string): number | null {
  const value = detail?.[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

/**
 * The refusal for a person's own storage limit — the org or a team admin set it
 * for them, and the drive itself may still have room.
 *
 * The server names the limit that bound, the team it belongs to, what the write
 * needed and what is left, and the copy repeats those figures: they are what tell
 * a person whether trashing something will make the file fit, and whom to ask.
 * "Out of storage" is not said — with most of the allowance free it would be
 * false. An older server that sent no figures gets a true, vaguer sentence.
 */
const userQuotaCopy: Builder = (_context, error) => {
  const detail = error.detail;
  const team = typeof detail?.team_name === "string" && detail.team_name ? detail.team_name : null;
  const limitName = team ? `your ${team} storage limit` : "your storage limit";
  const who = team ? `an admin of ${team}` : "your org or team admin";
  const advice = `Free up space in the trash, or ask ${who} to raise it.`;
  const needed = bytesIn(detail, "needed_bytes");
  const limit = bytesIn(detail, "limit_bytes");
  const left = bytesIn(detail, "remaining_bytes");
  if (limit == null || left == null) {
    return {
      title: `${capitalize(limitName)} is reached.`,
      detail: advice,
      retryable: false,
    };
  }
  const room = left > 0 ? `${formatSize(left)} is left` : "nothing is left";
  return {
    title:
      needed != null && needed > 0
        ? `${formatSize(needed)} does not fit ${limitName}.`
        : `${capitalize(limitName)} is reached.`,
    detail: `The limit is ${formatSize(limit)} and ${room}. ${advice}`,
    retryable: false,
  };
};

/**
 * One row per refusal the Files surface can produce.
 *
 * Product copy only: no HTTP status, no code, no mention of an editor or a terminal.
 * Each says what happened and what the person can do about it.
 */
/** A folder held by the reader's own mount, chat or box: named as theirs, never as
 *  "someone", which would send them looking for a colleague who does not exist. */
function leasedByYou(context: FilesErrorContext): Omit<FilesErrorCopy, "code"> {
  const named = context.holder ? ` ${context.holder}` : "";
  if (context.yours === "chat") {
    return {
      title: `Your chat${named} is using this folder.`,
      detail: "It becomes editable again when the chat goes to sleep.",
      retryable: true,
    };
  }
  if (context.yours === "box") {
    return {
      title: `Your box${named} is using this folder.`,
      detail: "It becomes editable again once the box releases it.",
      retryable: true,
    };
  }
  return {
    title: "You have this folder for local use.",
    detail: context.machine
      ? `It becomes editable again once you release it on ${context.machine}.`
      : "It becomes editable again once you release it.",
    retryable: true,
  };
}

function leasedByOther(context: FilesErrorContext): Omit<FilesErrorCopy, "code"> {
  return {
    title: context.holder
      ? // The holder opens the sentence, and it can be the word "someone" or
        // "the chat X" as well as a person's name — so the first letter is
        // raised here, the way the branch below already spells it.
        `${capitalize(context.holder)} has this folder for local use.`
      : "Someone has this folder for local use.",
    detail: context.holder
      ? `${
          context.machine ? `It is checked out on ${context.machine}. ` : ""
        }You can ask ${context.holder} for it back, and it becomes editable again the moment they release it.`
      : "It becomes editable again the moment the folder is released.",
    retryable: true,
  };
}

const COPY: Readonly<Record<string, Builder>> = {
  // --- the state refuses this change right now (409) ----------------------
  "files.held": (context) => ({
    title: "This is on legal hold.",
    detail: `A hold keeps everything inside it exactly as it is, so it cannot be ${
      context.action ?? "changed"
    } until the hold is lifted. An org admin can lift it.`,
    retryable: false,
  }),
  "files.inherited_grant": (context) => ({
    title: "This access is inherited, so it cannot be removed here.",
    detail: context.grantedBy
      ? `It comes from ${context.grantedBy}. Remove it there, or share this folder separately.`
      : "It comes from a folder further up. Remove it there, or share this folder separately.",
    retryable: false,
  }),
  // The server says whose ownership it is — "You already own this." to the
  // owner themselves — which this table cannot know, so its sentence is kept.
  "files.already_owner": (_context, error) => ({
    title: serverSentence(error) ?? "They already own this.",
    detail: "Nothing was changed.",
    retryable: false,
  }),
  // A chat in a workspace that holds several chats is shared with its workspace.
  "files.share_the_workspace": () => ({
    title: "Share the workspace this chat is in instead.",
    detail: "Nothing was changed.",
    retryable: false,
  }),
  // An org grant names only the caller's own organization.
  "files.org_principal_not_own": () => ({
    title: "You can only share with your own organization.",
    detail: "Nothing was changed.",
    retryable: false,
  }),
  // Forcing a box off a folder cuts off a turn in flight, so it takes a reason.
  "files.force_reason_required": () => ({
    title: "Say why you are taking this folder back.",
    detail: "Nothing was changed.",
    retryable: false,
  }),
  // A chat's folder stays in the workspace it was started in.
  "files.chat_workspace_move": () => ({
    title: "A chat stays in its workspace.",
    detail: "Start a new chat in the other workspace instead.",
    retryable: false,
  }),
  "files.container_readonly": () => ({
    title: "Files live in your home folder or a team folder, not here.",
    detail: "This level only lists where things are kept. Open your home folder or a team folder and add it there.",
    retryable: false,
  }),
  "files.leased": (context) => (context.yours ? leasedByYou(context) : leasedByOther(context)),
  "files.lease_fenced": () => ({
    title: "Someone else has this folder for local use now.",
    detail: "The copy you had was replaced by a newer one. Reload the folder and try again.",
    retryable: true,
  }),
  // The chat this folder belonged to was put to sleep, deleted or moved, and its
  // hold on the folder ended with it; taking the folder again starts afresh.
  "files.lease_ended": () => ({
    title: "This folder is no longer out for local use.",
    detail: "Reload the folder, then take it again to keep working on it.",
    retryable: false,
  }),
  "files.lease_mismatch": () => ({
    title: "That is not in the folder you have for local use.",
    detail: "Reload the folder, then try again.",
    retryable: false,
  }),
  "files.conflict_of_elsewhere": () => ({
    title: "The file this copy was displaced from is not in that folder.",
    detail: "Upload it into the folder the original is in.",
    retryable: false,
  }),
  "files.bad_encoding": () => ({
    title: "That upload could not be read.",
    detail: "It arrived in a form this server cannot open. Try again.",
    retryable: true,
  }),
  "files.batch_too_large": () => ({
    title: "That is too many files to send at once.",
    detail: "Send fewer files at a time.",
    retryable: true,
  }),
  "files.file_needs_content": () => ({
    title: "A new file needs its content.",
    detail: "Upload the file rather than creating an empty one.",
    retryable: false,
  }),
  "files.live_too_many": () => ({
    title: "Too many files in this folder are open for editing.",
    detail: "Close or save a few of them, then try again.",
    retryable: true,
  }),
  "files.cycle": () => ({
    title: "A folder cannot be moved inside itself.",
    detail: "Pick a destination that is not inside the folder you are moving.",
    retryable: false,
  }),
  "files.moving": () => ({
    title: "This is still being moved.",
    detail: "Wait for the move to finish, then try again.",
    retryable: true,
  }),
  "files.frozen": () => ({
    title: "This drive is over its storage limit.",
    detail: "Free up space, or ask an org admin to raise the limit.",
    retryable: false,
  }),
  "files.trashed": () => ({
    title: "This is in the trash.",
    detail: "Restore it from the trash first, then try again.",
    retryable: false,
  }),
  "files.exists": () => ({
    title: "Something here already has that name.",
    detail: "Pick another name, or choose a different folder.",
    retryable: false,
  }),
  "files.not_undoable": () => ({
    title: "This can't be undone.",
    detail: "Nothing was changed. Move or delete what it made instead.",
    retryable: false,
  }),
  "files.large_move": () => ({
    title: "This folder is too big to move in one go.",
    detail: "Move the folders inside it a few at a time.",
    retryable: false,
  }),
  "files.too_many_sessions": () => ({
    title: "Too many uploads are already running.",
    detail: "Wait for one to finish, then try again.",
    retryable: true,
  }),
  "files.part_mismatch": () => ({
    title: "This upload does not match what the server received.",
    detail: "Upload the file again.",
    retryable: true,
  }),
  "files.parts_mismatch": () => ({
    title: "Some of this upload never arrived.",
    detail: "Upload the file again.",
    retryable: true,
  }),
  "files.session_state": () => ({
    title: "This upload is no longer open.",
    detail: "Upload the file again.",
    retryable: false,
  }),
  "files.conflict_resolved": () => ({
    title: "Someone already settled this.",
    detail: "Reload to see how it was settled.",
    retryable: false,
  }),
  "files.conflict": () => ({
    title: "This cannot be changed right now.",
    detail: "Something else is working on it. Try again in a moment.",
    retryable: true,
  }),
  "files.read_only_content": () => ({
    title: "The contents of this file cannot be replaced.",
    detail: "Upload it as a new file instead.",
    retryable: false,
  }),

  // --- what you are holding is no longer what is stored (412) -------------
  "files.precondition_failed": () => ({
    title: "Someone changed this while you were working.",
    detail: "Reload and try again.",
    retryable: true,
  }),
  // 428: the request left without saying which version it was changing. That is a
  // client fault, not somebody else's edit, so it does not borrow the sentence above.
  "files.if_match_required": () => ({
    title: "That could not be sent.",
    detail: "Reload the page and try again.",
    retryable: true,
  }),

  // --- it is not there, or it is not yours (404 / 403) --------------------
  "files.not_found": () => ({
    title: "This is not here any more.",
    detail: "It may have been moved or deleted, or you may not have access. Reload the folder.",
    retryable: false,
  }),
  "files.forbidden": () => ({
    title: "You do not have permission to do that.",
    detail: "Ask someone who can edit this folder to do it, or to share it with you.",
    retryable: false,
  }),

  // --- no room left (507) -------------------------------------------------
  "files.quota_exceeded": quotaCopy("Your organization is out of storage."),
  "files.quota_bytes": quotaCopy("Your organization is out of storage."),
  "files.quota_nodes": quotaCopy("Your organization has reached its limit on the number of files."),
  // The drive may still have room: this is the limit an org or team admin set for one person.
  "files.user_quota_bytes": userQuotaCopy,

  // --- the request itself is not one Files can run ------------------------
  "files.error": () => ({
    title: "That did not go through.",
    detail: "Something went wrong on our side. Try again.",
    retryable: true,
  }),
  "files.invalid_request": () => ({
    title: "Files cannot do that.",
    detail: "Reload the page and try again.",
    retryable: false,
  }),
  "files.idempotency_key_required": () => ({
    title: "That request could not be sent safely.",
    detail: "Reload the page and try again.",
    retryable: false,
  }),
  "files.idempotency_mismatch": () => ({
    title: "This looks like a repeat of a different request.",
    detail: "Reload the page and try again.",
    retryable: false,
  }),
  "files.bad_limit": () => ({
    title: "That is more rows than one page can hold.",
    detail: "Ask for between 1 and 1,000 rows.",
    retryable: false,
  }),
  "files.invalid_marker": () => ({
    title: "This list is out of date.",
    detail: "Reload the folder to start again.",
    retryable: false,
  }),
  "files.bad_filter": () => ({
    title: "That filter cannot be used here.",
    detail: "Clear the filter and try again.",
    retryable: false,
  }),
  "files.unknown_principal_kind": () => ({
    title: "This cannot be shared with that.",
    detail: "Share with a person, a team, or everyone in your organization.",
    retryable: false,
  }),
  "files.unknown_filter": () => ({
    title: "There is no filter by that name.",
    detail: "Clear the filter and try again.",
    retryable: false,
  }),
  "files.bad_id": () => ({
    title: "That link does not point at anything here.",
    detail: "Check the address, or go back to the folder.",
    retryable: false,
  }),
  "files.bad_query": () => ({
    title: "That search cannot be run.",
    detail: "Try different words.",
    retryable: false,
  }),
  "files.bulk_empty": () => ({
    title: "Nothing was selected.",
    detail: "Pick at least one item and try again.",
    retryable: false,
  }),
  "files.bulk_too_large": () => ({
    title: "That is too many items at once.",
    detail: "Select fewer of them and try again.",
    retryable: false,
  }),
  "files.bulk_missing_field": () => ({
    title: "Part of that request was missing.",
    detail: "Reload the page and try again.",
    retryable: false,
  }),
  "files.bulk_duplicate_id": () => ({
    title: "The same item was listed twice.",
    detail: "Select each item once and try again.",
    retryable: false,
  }),
  "files.bulk_bad_op": () => ({
    title: "That is not something Files can do.",
    detail: "Reload the page and try again.",
    retryable: false,
  }),
  "files.bulk_plan_missing": () => ({
    title: "That batch could not be run.",
    detail: "Start it again.",
    retryable: true,
  }),
  "files.operation_failed": () => ({
    title: "That job stopped before it finished.",
    detail: "Start it again.",
    retryable: true,
  }),
  "files.runner_lost": () => ({
    title: "That job never started.",
    detail: "Start it again.",
    retryable: true,
  }),
  "files.invalid_batch": () => ({
    title: "That batch could not be read.",
    detail: "Reload the page and try again.",
    retryable: false,
  }),
  "files.tree_too_large": () => ({
    title: "That is too many folders to create at once.",
    detail: "Create fewer of them and try again.",
    retryable: false,
  }),
  "files.tree_too_deep": () => ({
    title: "Those folders are nested too deeply.",
    detail: "Create them a few levels at a time.",
    retryable: false,
  }),
  "files.retention_label_unsupported_kind": () => ({
    title: "This kind of item cannot be kept on a schedule.",
    detail: "Apply the rule to a folder instead.",
    retryable: false,
  }),
  "files.rendering_not_implemented": () => ({
    title: "There is no preview for this kind of file yet.",
    detail: "Download it to open it.",
    retryable: false,
  }),
  "files.rendering_retired": () => ({
    title: "Saved queries and reports no longer open here.",
    detail: "Open the chat template instead.",
    retryable: false,
  }),
  "files.unknown_crash_point": () => ({
    title: "That did not go through.",
    detail: "Something went wrong on our side. Try again.",
    retryable: true,
  }),

  // --- uploads ------------------------------------------------------------
  "files.invalid_size": () => ({
    title: "That file's size does not look right.",
    detail: "Upload it again.",
    retryable: true,
  }),
  // The server names a version's origin itself; a refusal is our bug, not the reader's.
  "files.invalid_source": () => ({
    title: "That did not go through.",
    detail: "Something went wrong on our side. Try again.",
    retryable: true,
  }),
  "files.too_large": () => ({
    title: "That file is too big to upload here.",
    detail: "Upload a smaller one, or ask an administrator to raise the limit.",
    retryable: false,
  }),
  "files.size_mismatch": () => ({
    title: "The whole file did not arrive.",
    detail: "Upload it again.",
    retryable: true,
  }),
  "files.checksum_mismatch": () => ({
    title: "The file changed while it was uploading.",
    detail: "Upload it again.",
    retryable: true,
  }),
  "files.part_checksum_mismatch": () => ({
    title: "Part of the file changed while it was uploading.",
    detail: "Upload it again.",
    retryable: true,
  }),
  // The upload routes refuse a part with no bytes, and a 0-byte file is exactly
  // one empty part — so this is what an empty file gets, every time, and telling
  // the person to upload it again sends them round the same refusal.
  "files.empty_part": () => ({
    title: "An empty file can't be uploaded.",
    detail: "Put something in the file, then upload it again.",
    retryable: false,
  }),
  "files.part_out_of_range": () => ({
    title: "Part of that upload did not fit.",
    detail: "Upload the file again.",
    retryable: true,
  }),
  "files.not_a_folder": () => ({
    title: "That destination is a file, not a folder.",
    detail: "Pick a folder and try again.",
    retryable: false,
  }),
  "files.store_unavailable": () => ({
    title: "Storage is busy right now.",
    detail: "Try again in a moment.",
    retryable: true,
  }),
  // The one refusal in this table the BROWSER raises rather than the server: a
  // `File` is a handle, and the bytes behind it can be gone by the time a part
  // is read. It keeps its own `upload.` prefix because `files.` is the server's
  // namespace, and a code invented here inside it is a code a later release
  // could give a different meaning to.
  "upload.unreadable": () => ({
    title: "That file could not be read.",
    detail: "Check it is still where you picked it from, then upload it again.",
    retryable: true,
  }),

  // --- a name the server will not take ------------------------------------
  "files.invalid_name.empty": () => ({
    title: "A name cannot be blank.",
    detail: "Type a name and try again.",
    retryable: false,
  }),
  "files.invalid_name.nul": () => ({
    title: "That name uses a character that cannot be saved.",
    detail: "Remove it and try again.",
    retryable: false,
  }),
  "files.invalid_name.separator": () => ({
    title: "A name cannot contain a slash.",
    detail: "Pick a name without a slash in it.",
    retryable: false,
  }),
  "files.invalid_name.dot": () => ({
    title: "That name is reserved.",
    detail: "Pick a different name.",
    retryable: false,
  }),
  "files.invalid_name.too_long": () => ({
    title: "That name is too long.",
    detail: "Use a shorter name.",
    retryable: false,
  }),
  "files.invalid_name.control": () => ({
    title: "That name contains a hidden character that cannot be saved.",
    detail: "Retype the name and try again.",
    retryable: false,
  }),
  "files.invalid_name.surrounding_space": () => ({
    title: "A name cannot start or end with a space.",
    detail: "Remove the space and try again.",
    retryable: false,
  }),
  "files.invalid_name.bidi_control": () => ({
    title: "That name contains a character that makes it read as a different name.",
    detail: "Retype the name and try again.",
    retryable: false,
  }),
  "files.link_outside_tree": () => ({
    title: "A link must point to something inside this drive.",
    detail: "Point it at a file or folder in the drive.",
    retryable: false,
  }),
  // --- the platform paced this caller (429) ---------------------------------
  rate_limited: (_context, error) => ({
    title: "You are going a little fast.",
    detail: serverSentence(error) ?? "Wait a moment, then try again.",
    retryable: true,
  }),
};

/** The 507 row, keyed by status rather than by code. */
const QUOTA: Builder = quotaCopy("Your organization is out of storage.");

/**
 * The copy for one refusal.
 *
 * Anything that is not an {@link ApiError} — a dropped connection, a thrown `Error` —
 * still gets a sentence, because a screen that renders nothing when the network dies
 * is worse than one that says so.
 */
export function filesErrorCopy(error: unknown, context: FilesErrorContext = {}): FilesErrorCopy {
  if (!(error instanceof ApiError)) {
    return {
      code: "error",
      title: "That did not go through.",
      detail: "The connection dropped before the server answered. Try again.",
      retryable: true,
    };
  }

  const code = error.code ?? "error";
  const builder = (code in COPY ? COPY[code] : undefined) ?? (error.status === QUOTA_STATUS ? QUOTA : undefined);
  if (builder) return { code, ...builder(context, error) };

  return {
    code,
    // The envelope's message is written for a person; prefer it over anything invented
    // here, and only fall back when the refusal carried no message at all.
    title: error.message || "That did not go through.",
    retryable: error.status >= 500,
  };
}

/**
 * The copy for one refused row of a batch.
 *
 * A batch item is not a response of its own: the request answered 200 and the
 * row's status and code are fields in its body, carried there so a caller reuses
 * one error path for both shapes. So there are no headers to thread — no
 * `Retry-After` for one row of a batch that the server itself accepted — and the
 * row is lifted out of its envelope before the error is built rather than read
 * off something that was never a response. It goes through the same table as
 * every other refusal, so a code cannot mean two things.
 */
export function batchRowErrorCopy(
  row: { status: number; body?: { code?: string; message?: string } | null },
  context: FilesErrorContext = {},
): FilesErrorCopy {
  const { status, body } = row;
  return filesErrorCopy(new ApiError(status, body ?? null), context);
}

/** True when a refusal is one the Files surface has real copy for, rather than a
 *  pass-through of the server's own sentence. Pages use it to decide whether the
 *  refusal deserves its own inline block or a plain line of text. */
export function isKnownFilesError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return (error.code !== null && error.code in COPY) || error.status === QUOTA_STATUS;
}
