import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QRCodeSVG } from "qrcode.react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { silentNotify } from "@/tests/fixtures/notify";

// The MFA enrollment section, with its api/mfa hooks (the costly boundary) mocked; everything
// the section DOES with them runs for real. The flow is a state machine, and each test pins an
// observable transition a wrong implementation would miss: off → enroll reveals the secret +
// confirm field; confirm reveals the once-shown backup codes; an enabled account turns it off
// only with a code. The disable button must stay disabled until a code is typed (a hijacked
// session can't disable MFA freely) — that gate is pinned too.

type Query<T> = { data?: T; isPending?: boolean; isError?: boolean; refetch?: () => void };
type Mut = { mutate: ReturnType<typeof vi.fn>; isPending: boolean; error?: unknown };

const ENROLL = { secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/Alkera:ada?secret=JBSWY3DPEHPK3PXP" };
const BACKUP = { backup_codes: ["aaaa-1111", "bbbb-2222", "cccc-3333"] };

const h = {
  status: {} as Query<{ enabled: boolean; backup_codes_remaining: number }>,
  enroll: {} as Mut,
  confirm: {} as Mut,
  disable: {} as Mut,
};

// A mutation mock whose mutate invokes onSuccess with a scripted result, so the section's
// success transitions (reveal codes, clear field) are exercised.
const mut = (result?: unknown): Mut => ({
  mutate: vi.fn((vars: unknown, opts?: { onSuccess?: (d: unknown) => void }) => opts?.onSuccess?.(result ?? vars)),
  isPending: false,
  error: undefined,
});

vi.mock("@/api/mfa", () => ({
  useMfaStatus: () => h.status,
  useMfaEnroll: () => h.enroll,
  useMfaConfirm: () => h.confirm,
  useMfaDisable: () => h.disable,
}));

const { MfaSection } = await import("@/pages/organization/settings/MfaSection");

afterEach(cleanup);
beforeEach(() => {
  h.status = { data: { enabled: false, backup_codes_remaining: 0 }, refetch: vi.fn() };
  h.enroll = mut(ENROLL);
  h.confirm = mut(BACKUP);
  h.disable = mut();
});

describe("MfaSection", () => {
  it("shows a skeleton while the status is pending", () => {
    h.status = { isPending: true };
    render(<MfaSection notify={silentNotify} />);
    expect(screen.queryByRole("button", { name: /set up/i })).toBeNull();
  });

  it("starts enrollment and reveals the setup key + confirm field", async () => {
    const user = userEvent.setup();
    render(<MfaSection notify={silentNotify} />);

    await user.click(screen.getByRole("button", { name: /set up/i }));
    expect(h.enroll.mutate).toHaveBeenCalledTimes(1);
    expect(screen.getByText(ENROLL.secret)).toBeInTheDocument();
    // The setup key is the shared copyable reading — its copy affordance carries the field's name.
    expect(screen.getByRole("button", { name: /copy setup key/i })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /two-factor setup qr code/i })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /6-digit code/i })).toBeInTheDocument();
  });

  it("renders a QR code that encodes the otpauth URI", async () => {
    const user = userEvent.setup();
    render(<MfaSection notify={silentNotify} />);
    await user.click(screen.getByRole("button", { name: /set up/i }));

    // QRCodeSVG is a pure function of its props, so a reference render with the same lib pins the
    // encoding — the section's module paths match a reference fed the enroll URI, and NOT one fed
    // a different URI (so the comparison can't pass vacuously). Fails exactly when the section
    // hands the encoder the wrong value, never on lib internals (both sides drift together).
    const qrPaths = (uri: string) =>
      Array.from(
        renderToStaticMarkup(<QRCodeSVG value={uri} size={160} level="M" marginSize={0} />).matchAll(
          / d="([^"]+)"/g,
        ),
        (m) => m[1],
      ).join("|");

    const qr = screen.getByRole("img", { name: /two-factor setup qr code/i });
    const rendered = Array.from(qr.querySelectorAll("path"), (p) => p.getAttribute("d")).join("|");
    expect(rendered).toBe(qrPaths(ENROLL.otpauth_uri));
    expect(rendered).not.toBe(qrPaths("otpauth://totp/Alkera:eve?secret=NOTTHESAMESECRET"));
  });

  it("confirms the code and reveals the one-time backup codes", async () => {
    const user = userEvent.setup();
    render(<MfaSection notify={silentNotify} />);

    await user.click(screen.getByRole("button", { name: /set up/i }));
    await user.type(screen.getByRole("textbox", { name: /6-digit code/i }), "123456");
    await user.click(screen.getByRole("button", { name: /verify and turn on/i }));

    expect(h.confirm.mutate).toHaveBeenCalledWith("123456", expect.anything());
    const list = screen.getByRole("list", { name: /backup codes/i });
    for (const code of BACKUP.backup_codes) {
      expect(within(list).getByText(code)).toBeInTheDocument();
    }
  });

  it("when enabled, blocks disable until a code is entered, then disables with it", async () => {
    const user = userEvent.setup();
    h.status = { data: { enabled: true, backup_codes_remaining: 7 }, refetch: vi.fn() };
    render(<MfaSection notify={silentNotify} />);

    expect(screen.getByText(/7 backup codes remaining/i)).toBeInTheDocument();
    const turnOff = screen.getByRole("button", { name: /turn off/i });
    expect(turnOff).toBeDisabled();

    await user.type(screen.getByRole("textbox", { name: /authenticator code/i }), "654321");
    expect(turnOff).toBeEnabled();
    await user.click(turnOff);
    expect(h.disable.mutate).toHaveBeenCalledWith("654321", expect.anything());
  });
});
