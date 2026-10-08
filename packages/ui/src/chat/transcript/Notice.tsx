// System notices: one geometry, level-marked by border, wash, and icon.
import s from "./notice.module.css";
export type NoticeLevel = "danger" | "warning" | "neutral";
function NoticeIcon({ level }: { level: NoticeLevel }) {
  if (level === "danger") {
    return (
      <svg viewBox="0 0 14 14" width="15" height="15" aria-hidden="true">
        <circle cx="7" cy="7" r="5.4" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M7 4v3.6" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        <circle cx="7" cy="9.7" r="0.7" fill="currentColor" />
      </svg>
    );
  }
  if (level === "warning") {
    return (
      <svg viewBox="0 0 14 14" width="15" height="15" aria-hidden="true">
        <path d="M7 1.9 12.6 11.4H1.4Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        <path d="M7 5.4v2.8" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        <circle cx="7" cy="9.8" r="0.7" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 14 14" width="15" height="15" aria-hidden="true">
      <circle cx="7" cy="7" r="5.4" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="7" cy="4.5" r="0.75" fill="currentColor" />
      <path d="M7 6.6v3.6" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}
/** The one thing the reader can do about the notice. A `href` renders a link
 *  (a page they can open), an `onClick` a button (something the shell does for
 *  them); a notice with neither says only what happened. */
export interface NoticeAction {
  label: string;
  href?: string;
  onClick?: () => void;
}

export function Notice({
  level,
  title,
  body,
  action,
  onDismiss,
}: {
  level: NoticeLevel;
  title: string;
  body: string;
  action?: NoticeAction | null;
  /** A surface banner offers its own dismissal; a transcript notice passes none. */
  onDismiss?: () => void;
}) {
  return (
    <div className={s.notice} data-level={level}>
      <span className={s.icon}>
        <NoticeIcon level={level} />
      </span>
      <div >
        <p className={s.title}>{title}</p>
        {/* A notice whose whole content is its title — a refusal with no
            statement to quote — is one line, not one line and an empty one
            under it. */}
        {body.trim() ? <p className={s.body}>{body}</p> : null}
        {action ? (
          action.href ? (
            <a className={`alk-link ${s.action}`} data-testid="notice-action" href={action.href}>
              {action.label}
            </a>
          ) : (
            <button
              type="button"
              className={`alk-link ${s.action}`}
              data-testid="notice-action"
              onClick={action.onClick}
            >
              {action.label}
            </button>
          )
        ) : null}
      </div>
      {onDismiss ? (
        <button type="button" className={s.close} aria-label="Dismiss" onClick={onDismiss}>
          <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
            <path d="M3.5 3.5 10.5 10.5M10.5 3.5 3.5 10.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      ) : null}
    </div>
  );
}
