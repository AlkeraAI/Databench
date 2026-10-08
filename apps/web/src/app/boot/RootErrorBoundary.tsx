// App-root React error boundary for the ui portal. Catches render errors
// anywhere below it, reports them (logs + Sentry), and shows a recoverable
// fallback that lets the user send a crash report with an optional comment.

import { Component, type ReactNode } from "react";

import { Button, Inline, Stack, Textarea } from "@alkera/ui";

import { reportClientError, submitCrashReport } from "./reportClientError";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
  comment: string;
  status: "idle" | "sending" | "sent" | "failed";
}

export class RootErrorBoundary extends Component<Props, State> {
  state: State = { error: null, comment: "", status: "idle" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error): void {
    void reportClientError(error, { boundary: "root" });
  }

  private handleSend = async (): Promise<void> => {
    const { error, comment } = this.state;
    if (!error) return;
    this.setState({ status: "sending" });
    const id = await submitCrashReport({
      message: error.message || error.name,
      stacktrace: error.stack,
      comment: comment.trim() || undefined,
    });
    this.setState({ status: id ? "sent" : "failed" });
  };

  render(): ReactNode {
    const { error, status, comment } = this.state;
    if (!error) return this.props.children;

    return (
      <div style={{ maxWidth: 480, margin: "0 auto", padding: "64px 24px" }}>
        <Stack gap={5} align="stretch">
          {/* Title + body are one tight group; the group-to-controls gap is the wider one, so the
              hierarchy reads without display:grid. Bare heading/paragraph margins are reset so the
              Stack gaps own every space. */}
          <Stack gap={3} align="stretch">
            <h1
              style={{
                margin: 0,
                fontFamily: "var(--alkFontDisplay)",
                fontSize: "var(--alkText3xl)",
                fontWeight: 600,
                letterSpacing: "-0.01em",
                color: "var(--alkPrimaryText)",
              }}
            >
              Something went wrong
            </h1>
            <p style={{ margin: 0, color: "var(--alkSecondaryText)" }}>
              The page hit an unexpected error. Send a report to help us fix it. It carries the error and stack
              trace, but no file contents or secrets.
            </p>
          </Stack>
          {status === "sent" ? (
            <p role="status" style={{ margin: 0 }}>
              Your report was sent. Thank you.
            </p>
          ) : (
            <>
              <Textarea
                value={comment}
                placeholder="What were you doing when this happened? (optional)"
                onChange={(e) => this.setState({ comment: e.currentTarget.value })}
                rows={3}
              />
              {status === "failed" && (
                <p role="alert" style={{ margin: 0, color: "var(--alkDangerText)" }}>
                  We couldn't send the report. You can still reload.
                </p>
              )}
              <Inline gap={3}>
                <Button
                  onClick={() => {
                    void this.handleSend();
                  }}
                  disabled={status === "sending"}
                >
                  {status === "sending" ? "Sending..." : "Send report"}
                </Button>
                <Button variant="secondary" onClick={() => window.location.reload()}>
                  Reload
                </Button>
              </Inline>
            </>
          )}
        </Stack>
      </div>
    );
  }
}
