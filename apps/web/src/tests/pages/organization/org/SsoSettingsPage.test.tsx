import { MemoryRouter } from "react-router-dom";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The org-admin SSO page, with its api/orgAdminSso hooks (the costly boundary) mocked; everything
// the page DOES with them runs for real. The behavior pinned: the form seeds from the loaded
// connection, the Save payload carries the RIGHT protocol-specific fields + the group mapping
// (blank rows dropped), the Save button stays disabled until the required fields are present,
// and minting a SCIM token reveals it once. The OIDC vs SAML payload shape is the asymmetry a
// wrong implementation gets wrong (sending SAML fields under OIDC, or vice-versa).

type Query<T> = { data?: T; isPending?: boolean; isError?: boolean; refetch?: () => void };
type Mut = { mutate: ReturnType<typeof vi.fn>; isPending: boolean; error?: unknown };

const BASE_SSO = {
  configured: true,
  enabled: false,
  enforced: false,
  protocol: "oidc",
  allowed_domains: "acme.com,acme.io",
  oidc_issuer: "https://acme.okta.com",
  oidc_client_id: "client-abc",
  has_client_secret: true,
  saml_idp_entity_id: null,
  saml_sso_url: null,
  has_saml_cert: false,
  saml_sp_entity_id: "alkera-sp",
  saml_acs_url: "https://api.example.com/acs",
  groups_mapping: { "acme-admins": "admin" },
  scim_enabled: false,
  has_scim_token: false,
  scim_base_url: "https://api.example.com/scim",
};

const h = {
  config: {} as Query<typeof BASE_SSO>,
  save: {} as Mut,
  mint: {} as Mut,
  revoke: {} as Mut,
};
const mut = (result?: unknown): Mut => ({
  mutate: vi.fn((_v: unknown, opts?: { onSuccess?: (d: unknown) => void; onSettled?: () => void }) => {
    opts?.onSuccess?.(result);
    opts?.onSettled?.();
  }),
  isPending: false,
  error: undefined,
});

vi.mock("@/api/orgAdminSso", () => ({
  useSsoConfig: () => h.config,
  useUpdateSsoConfig: () => h.save,
  useMintScimToken: () => h.mint,
  useRevokeScimToken: () => h.revoke,
}));

// This suite exercises the real SSO form, so the server gate must resolve to
// "enabled" (its gated branch is covered separately in featureGate.test).
vi.mock("@/api/dashboard", () => ({
  useIdentityDashboard: () => ({ data: { enterprise_features_enabled: true }, isPending: false }),
}));

const { SCIM_CONFIRM, SsoSettingsPage } = await import("@/pages/organization/org/SsoSettingsPage");

afterEach(cleanup);
beforeEach(() => {
  h.config = { data: structuredClone(BASE_SSO), refetch: vi.fn() };
  h.save = mut();
  h.mint = mut({ token: "scim-secret-xyz", scim_base_url: "https://api.example.com/scim" });
  h.revoke = mut();
});

const renderPage = () => render(<MemoryRouter><SsoSettingsPage /></MemoryRouter>);

describe("SsoSettingsPage", () => {
  it("seeds the OIDC fields from the loaded connection", () => {
    renderPage();
    expect(screen.getByRole("textbox", { name: /issuer url/i })).toHaveValue("https://acme.okta.com");
    expect(screen.getByRole("textbox", { name: /client id/i })).toHaveValue("client-abc");
  });

  it("saves an OIDC payload with the issuer + client id, no SAML fields, and the kept mapping", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(h.save.mutate).toHaveBeenCalledTimes(1);
    const body = h.save.mutate.mock.calls[0][0];
    expect(body).not.toHaveProperty("allowed_domains");
    expect(body).toMatchObject({
      protocol: "oidc",
      oidc_issuer: "https://acme.okta.com",
      oidc_client_id: "client-abc",
      groups_mapping: { "acme-admins": "admin" },
    });
    expect(body).not.toHaveProperty("saml_idp_entity_id");
  });

  it("switches to SAML and saves a SAML payload with the IdP fields", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("tab", { name: /saml/i }));
    await user.type(screen.getByRole("textbox", { name: /idp entity id/i }), "https://idp.acme.com/meta");
    await user.type(screen.getByRole("textbox", { name: /idp sso url/i }), "https://idp.acme.com/sso");
    await user.type(screen.getByRole("textbox", { name: /idp signing certificate/i }), "-----BEGIN CERTIFICATE-----xyz");
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    const body = h.save.mutate.mock.calls[0][0];
    expect(body).toMatchObject({
      protocol: "saml",
      saml_idp_entity_id: "https://idp.acme.com/meta",
      saml_sso_url: "https://idp.acme.com/sso",
    });
    expect(body).not.toHaveProperty("oidc_issuer");
  });

  it("drops a blank mapping row from the saved payload", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: /add mapping/i }));
    // The new row is blank — it must NOT appear in the saved mapping.
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    const body = h.save.mutate.mock.calls[0][0];
    expect(Object.keys(body.groups_mapping)).toEqual(["acme-admins"]);
  });

  it("disables Save until the issuer and client id are present", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.clear(screen.getByRole("textbox", { name: /issuer url/i }));
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
  });

  it("shows the SP entity id and ACS URL as copyable readings under SAML", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("tab", { name: /saml/i }));
    expect(screen.getByText("alkera-sp")).toBeInTheDocument();
    expect(screen.getByText("https://api.example.com/acs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy sp entity id/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy acs url/i })).toBeInTheDocument();
  });

  it("mints a SCIM token and reveals it once", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: /generate token/i }));
    expect(h.mint.mutate).toHaveBeenCalledTimes(1);
    expect(screen.getByText("scim-secret-xyz")).toBeInTheDocument();
  });
});

describe("SCIM token controls", () => {
  it("gives the once-shown token its own copy control, and raises no toast over the card", async () => {
    // user-event installs a clipboard stub on setup; what the page wrote is read back from it.
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: /generate token/i }));

    await user.click(screen.getByRole("button", { name: "Copy SCIM token" }));
    expect(await navigator.clipboard.readText()).toBe("scim-secret-xyz");
    // The base URL keeps its own, separate control.
    await user.click(screen.getByRole("button", { name: "Copy scim base url" }));
    expect(await navigator.clipboard.readText()).toBe("https://api.example.com/scim");
    expect(screen.queryByText(/copy it now/i)).not.toBeInTheDocument();
  });

  it("asks before revoking, and Cancel revokes nothing", async () => {
    h.config.data!.has_scim_token = true;
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Revoke and disable" }));
    expect(h.revoke.mutate).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: SCIM_CONFIRM.revoke.title });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(h.revoke.mutate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Revoke and disable" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: SCIM_CONFIRM.revoke.confirmLabel }),
    );
    expect(h.revoke.mutate).toHaveBeenCalledTimes(1);
    expect(h.mint.mutate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("asks before rotating a live token", async () => {
    h.config.data!.has_scim_token = true;
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Rotate token" }));
    expect(h.mint.mutate).not.toHaveBeenCalled();
    await user.click(
      within(screen.getByRole("dialog", { name: SCIM_CONFIRM.rotate.title })).getByRole("button", {
        name: SCIM_CONFIRM.rotate.confirmLabel,
      }),
    );
    expect(h.mint.mutate).toHaveBeenCalledTimes(1);
    expect(h.revoke.mutate).not.toHaveBeenCalled();
    expect(screen.getByText("scim-secret-xyz")).toBeInTheDocument();
  });

});

describe("Email domains", () => {
  it("lists the assigned domains as text the admin cannot edit", () => {
    renderPage();
    const list = screen.getByRole("list", { name: "Email domains" });
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "acme.com",
      "acme.io",
    ]);
    expect(screen.queryByRole("textbox", { name: /domain/i })).not.toBeInTheDocument();
    expect(screen.getByText("A platform administrator adds and removes these.")).toBeInTheDocument();
  });

  it("says when none are assigned, and still lets the admin generate a SCIM token", async () => {
    h.config.data!.allowed_domains = "";
    const user = userEvent.setup();
    renderPage();
    expect(screen.queryByRole("list", { name: "Email domains" })).not.toBeInTheDocument();
    expect(screen.getByText("No domains assigned.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /generate token/i }));
    expect(h.mint.mutate).toHaveBeenCalledTimes(1);
  });
});
