import { formatUsd } from "@alkera/chat-model";
import { Button, SegmentedControl, TextInput } from "@alkera/ui";
import { IconBolt, IconChecklist, IconLock, IconLockOpen, IconPencil } from "@tabler/icons-react";
import { useEffect, useRef, useState, type ComponentType } from "react";
import { AUTH_REQUIRED_CODE } from "@/lib/rpcCodes";
import { usagePanelRows } from "../data/usageRows";
import { PERMISSION_MODE_OPTIONS } from "../adapters";
import type { SlashPanelProps } from "./types";
import "./panels.css";

const CREDIT_FORMAT = new Intl.NumberFormat();

/** A friendly message for a daemon RPC rejection — AUTH_REQUIRED means
 *  the user isn't signed in; otherwise surface the error's own text. */
function errorMessage(err: unknown): string {
  if (err && typeof err === "object") {
    const e = err as { code?: unknown; message?: unknown };
    if (e.code === AUTH_REQUIRED_CODE) return "Sign in to view your usage.";
    if (typeof e.message === "string" && e.message) return e.message;
  }
  return String(err);
}

/** The usage windows the daemon accepts (mirrors `usage_view.WINDOWS`). */
const USAGE_WINDOWS = ["7d", "30d", "90d", "all"] as const;
type UsageWindow = (typeof USAGE_WINDOWS)[number];

/** Per-mode glyph — the same mapping the composer's mode picker uses, so the
 *  `/mode` panel reads as its sibling. */
const MODE_ICONS: Record<string, ComponentType<{ size?: number | string }>> = {
  default: IconBolt,
  plan: IconChecklist,
  auto: IconPencil,
  read_only: IconLock,
  bypass: IconLockOpen,
};

function PanelActions({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <footer className="alk-slash-panel__actions">{children}</footer>;
}

// --- /usage ----------------------------------------------------------------

interface UsageStat {
  label: string;
  value: string;
  hero?: boolean;
}

/** The panel rows: an installed extension's rows (USAGE_ROW_SOURCES), then this
 *  window's request count. */
function usageStats(payload: Record<string, unknown>, window: string): UsageStat[] {
  const usage = (payload.usage ?? {}) as Record<string, unknown>;
  const stats: UsageStat[] = usagePanelRows(payload);
  if (typeof usage.total_requests === "number") {
    stats.push({ label: `Requests (${window})`, value: CREDIT_FORMAT.format(usage.total_requests) });
  }
  return stats;
}

export function UsagePanel({ ctx }: SlashPanelProps): React.JSX.Element {
  const [window, setWindow] = useState<UsageWindow>("30d");
  const [state, setState] = useState<
    { status: "loading" } | { status: "error"; message: string } | { status: "ready"; stats: UsageStat[] }
  >({ status: "loading" });

  // Take focus on open so ←/→ change the window without a click first.
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    rootRef.current?.focus();
  }, []);
  const stepWindow = (delta: number): void => {
    setWindow((current) => {
      const next = (USAGE_WINDOWS.indexOf(current) + delta + USAGE_WINDOWS.length) % USAGE_WINDOWS.length;
      return USAGE_WINDOWS[next];
    });
  };
  const onKeyDown = (event: React.KeyboardEvent): void => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      stepWindow(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      stepWindow(1);
    }
  };

  useEffect(() => {
    let cancelled = false;
    // Don't reset to "loading" on a window switch — keep the current numbers on
    // screen until the new window resolves, so the panel doesn't flicker empty.
    void ctx
      .getUsage(window)
      .then((data) => {
        if (!cancelled) setState({ status: "ready", stats: usageStats(data, window) });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: errorMessage(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [ctx, window]);

  return (
    <div className="alk-slash-panel" ref={rootRef} tabIndex={-1} onKeyDown={onKeyDown}>
      <header className="alk-slash-panel__topbar">
        <h2 className="alk-slash-panel__head">Usage</h2>
        <SegmentedControl
          label="Usage window"
          size="sm"
          value={window}
          onChange={(value) => setWindow(value as UsageWindow)}
          options={[
            { key: "7d", label: "7d" },
            { key: "30d", label: "30d" },
            { key: "90d", label: "90d" },
            { key: "all", label: "All" },
          ]}
        />
      </header>
      <div className="alk-slash-panel__body">
        {state.status === "loading" ? (
          <p className="alk-slash-panel__muted">Loading…</p>
        ) : state.status === "error" ? (
          <p className="alk-slash-panel__error" role="alert">
            {state.message}
          </p>
        ) : state.stats.length === 0 ? (
          <p className="alk-slash-panel__muted">No usage in this window.</p>
        ) : (
          <dl className="alk-slash-stats">
            {state.stats.map((stat) => (
              <div key={stat.label} className={`alk-slash-stats__row${stat.hero ? " is-hero" : ""}`}>
                <dt>{stat.label}</dt>
                <dd>{stat.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    </div>
  );
}

// --- /cost -----------------------------------------------------------------

const COST_WINDOWS: { key: string; label: string }[] = [
  { key: "chat", label: "This chat" },
  { key: "day", label: "Today" },
  { key: "week", label: "This week" },
];

export function CostPanel({ ctx }: SlashPanelProps): React.JSX.Element {
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "error"; message: string }
    | { status: "ready"; spent: Record<string, number>; caps: Record<string, number>; orgManaged: boolean }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    void ctx
      .getCostState()
      .then((cost) => {
        if (!cancelled) {
          setState({ status: "ready", spent: cost.spent, caps: cost.caps, orgManaged: cost.orgManaged });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: errorMessage(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [ctx]);

  return (
    <div className="alk-slash-panel">
      <h2 className="alk-slash-panel__head">Cost</h2>
      <div className="alk-slash-panel__body">
        {state.status === "loading" ? (
          <p className="alk-slash-panel__muted">Loading…</p>
        ) : state.status === "error" ? (
          <p className="alk-slash-panel__error" role="alert">
            {state.message}
          </p>
        ) : (
          <>
            <table className="alk-slash-table">
              <thead>
                <tr>
                  <th>Window</th>
                  <th>Spent</th>
                  <th>Cap</th>
                </tr>
              </thead>
              <tbody>
                {COST_WINDOWS.map(({ key, label }) => {
                  const spent = state.spent[key] ?? 0;
                  const cap = state.caps[key] ?? 0;
                  return (
                    <tr key={key}>
                      <td>{label}</td>
                      <td className={cap > 0 && spent >= cap ? "is-over" : undefined}>{formatUsd(spent)}</td>
                      <td>{formatUsd(cap)}</td>
                    </tr>
                  );
                })}
                <tr>
                  <td>Per query</td>
                  <td>—</td>
                  <td>{formatUsd(state.caps.per_query ?? 0)}</td>
                </tr>
              </tbody>
            </table>
            {state.orgManaged ? (
              <p className="alk-slash-panel__muted">Caps are org-managed. Overages are rejected, not raised.</p>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}

// --- /mode -----------------------------------------------------------------

export function ModePanel({ ctx, api }: SlashPanelProps): React.JSX.Element {
  const options = PERMISSION_MODE_OPTIONS;
  const [busy, setBusy] = useState(false);
  // The keyboard cursor — starts on the current mode so ↑/↓ move from there.
  const [highlight, setHighlight] = useState(() =>
    Math.max(0, options.findIndex((option) => option.value === ctx.currentMode)),
  );
  const apply = (mode: string): void => {
    if (busy) return;
    setBusy(true);
    void ctx
      .setMode(mode)
      .then(() => api.close())
      .catch(() => setBusy(false));
  };
  // Take focus on open so the arrows work without a click first.
  const groupRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    groupRef.current?.focus();
  }, []);
  const onKeyDown = (event: React.KeyboardEvent): void => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? 1 : -1;
      setHighlight((current) => (current + delta + options.length) % options.length);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      apply(options[highlight].value);
    }
  };

  return (
    <div className="alk-slash-panel">
      <h2 className="alk-slash-panel__head">Permission mode</h2>
      <div className="alk-slash-panel__body">
        <div
          className="alk-slash-mode"
          role="radiogroup"
          aria-label="Permission mode"
          tabIndex={0}
          ref={groupRef}
          aria-activedescendant={`alk-mode-${options[highlight].value}`}
          onKeyDown={onKeyDown}
        >
          {options.map((option, index) => {
            const active = option.value === ctx.currentMode;
            const Icon = MODE_ICONS[option.value] ?? IconBolt;
            return (
              <button
                key={option.value}
                id={`alk-mode-${option.value}`}
                type="button"
                role="radio"
                aria-checked={active}
                tabIndex={-1}
                data-mode={option.value}
                className={`alk-slash-mode__row${active ? " is-active" : ""}${index === highlight ? " is-highlight" : ""}`}
                disabled={busy}
                onMouseEnter={() => setHighlight(index)}
                onClick={() => apply(option.value)}
              >
                <span className="alk-slash-mode__row-head">
                  <span className="alk-slash-mode__dot" aria-hidden data-active={active ? "true" : undefined} />
                  <span style={{ flex: "1 1 auto", textAlign: "left" }}>{option.label}</span>
                  <span className="alk-slash-mode__icon" aria-hidden>
                    <Icon size={16} />
                  </span>
                </span>
                <span className="alk-slash-mode__desc">{option.description}</span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// --- /title ----------------------------------------------------------------

export function TitlePanel({ ctx, api }: SlashPanelProps): React.JSX.Element {
  const [value, setValue] = useState(ctx.currentTitle || "");
  const [busy, setBusy] = useState(false);
  const submit = (): void => {
    const next = value.trim();
    if (!next || busy) return;
    setBusy(true);
    void ctx
      .runCommand(`/title ${next}`)
      .then(() => {
        ctx.onTitleChanged();
        api.close();
      })
      .catch(() => setBusy(false));
  };

  return (
    <div className="alk-slash-panel">
      <h2 className="alk-slash-panel__head">Chat title</h2>
      <div className="alk-slash-panel__body">
        <TextInput
          autoFocus
          value={value}
          placeholder="Name this chat"
          aria-label="Chat title"
          size="md"
          disabled={busy}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
        />
      </div>
      <PanelActions>
        <Button variant="secondary" fill="ghost" size="sm" onClick={api.close}>
          Cancel
        </Button>
        <Button size="sm" loading={busy} disabled={!value.trim()} onClick={submit}>
          Save title
        </Button>
      </PanelActions>
    </div>
  );
}

// --- /clear ----------------------------------------------------------------

export function ClearConfirmPanel({ ctx, api }: SlashPanelProps): React.JSX.Element {
  const [busy, setBusy] = useState(false);
  const confirm = (): void => {
    setBusy(true);
    void ctx
      .runCommand("/clear")
      .then(() => api.close())
      .catch(() => setBusy(false));
  };

  return (
    <div className="alk-slash-panel">
      <h2 className="alk-slash-panel__head">Clear the conversation?</h2>
      <div className="alk-slash-panel__body">
        <p className="alk-slash-panel__sub">
          The next turn starts fresh. Earlier messages drop out of the agent's context.
        </p>
      </div>
      <PanelActions>
        <Button variant="secondary" fill="ghost" size="sm" onClick={api.close}>
          Cancel
        </Button>
        <Button variant="destructive" fill="outline" size="sm" loading={busy} onClick={confirm}>
          Clear conversation
        </Button>
      </PanelActions>
    </div>
  );
}
