// The one field renderer behind every connection form. The cross-surface
// contracts it owns: field order (required → method → OPTIONAL LAST with the
// "Optional" divider), the browser's degradation of file fields, the accessory
// column, and the placeholder a select shows when nothing is answered.

import { useState } from "react";

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Switch } from "../../primitives";
import type { ConnectionDraft, ConnectionFormView, FormFieldView } from "../types";
import { ConnectionFormFields } from "./ConnectionFormFields";

const FORM: ConnectionFormView = {
  note: "",
  auth_methods: [
    {
      name: "pat",
      label: "Token",
      oauth: null,
      fields: [
        {
          name: "token",
          label: "Token",
          type: "password",
          required: true,
          secret: true,
          default: "",
          enum_values: [],
          help: "",
          placeholder: "",
          group: "",
        },
      ],
    },
  ],
  shared_fields: [
    {
      name: "account",
      label: "Account",
      type: "text",
      required: true,
      secret: false,
      default: "",
      enum_values: [],
      help: "",
      placeholder: "",
      group: "",
    },
    {
      name: "warehouse",
      label: "Warehouse",
      type: "text",
      required: false,
      secret: false,
      default: "",
      enum_values: [],
      help: "",
      placeholder: "",
      group: "",
    },
  ],
};

function draft(overrides: Partial<ConnectionDraft> = {}): ConnectionDraft {
  return { handle: "wh", method: "pat", values: {}, ...overrides };
}

const follows = (a: Element, b: Element) =>
  // eslint-disable-next-line no-bitwise -- the DOM position API is a bitmask
  (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;

describe("ConnectionFormFields", () => {
  it("renders required → method secret → optional LAST, with the Optional divider", () => {
    // Credentials sit beside the identity fields they belong with, so a user
    // types account → token rather than account → warehouse → token.
    render(<ConnectionFormFields form={FORM} draft={draft()} onDraftChange={() => {}} />);
    expect(follows(screen.getByLabelText(/Account/), screen.getByLabelText(/Token/))).toBe(true);
    expect(follows(screen.getByLabelText(/Token/), screen.getByLabelText(/Warehouse/))).toBe(true);
    expect(screen.getByRole("separator", { name: "Optional" })).toBeInTheDocument();
  });

  it("degrades a file field to a plain text input when no host picker exists", () => {
    // The browser portal has no host to pick a path with, so the field has to
    // stay typeable rather than offering a button nothing answers.
    const fileForm: ConnectionFormView = {
      note: "",
      auth_methods: [],
      shared_fields: [
        {
          name: "path",
          label: "Database file",
          type: "file",
          required: true,
          secret: false,
          default: "",
          enum_values: [],
          help: "",
          placeholder: "",
          group: "",
        },
      ],
    };
    const { rerender } = render(
      <ConnectionFormFields form={fileForm} draft={draft()} onDraftChange={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: "Browse…" })).not.toBeInTheDocument();

    rerender(
      <ConnectionFormFields
        form={fileForm}
        draft={draft()}
        onDraftChange={() => {}}
        pickPath={async () => "/picked"}
      />,
    );
    expect(screen.getByRole("button", { name: "Browse…" })).toBeInTheDocument();
  });

  it("wires the name's refusal to the field a screen reader is on", () => {
    const PROBLEM = "That name is already in use.";
    render(
      <ConnectionFormFields
        form={FORM}
        draft={draft()}
        onDraftChange={() => {}}
        handleError={PROBLEM}
      />,
    );
    const input = screen.getByLabelText(/Connection name/);
    expect(input).toHaveAttribute("aria-invalid", "true");
    const shown = screen.getByText(PROBLEM);
    expect(input.closest(".alk-field")?.contains(shown)).toBe(true);
    const errorId = shown.closest("p")?.id ?? "";
    expect(errorId).not.toBe("");
    expect(input.getAttribute("aria-describedby") ?? "").toContain(errorId);
  });

  it("heads the accessory column once, over the first field that has one", () => {
    // A form can ask something ABOUT each value as well as for it. The question
    // is the same all the way down, so asking it per row would be three copies
    // of one heading.
    const COLUMN = "Share with members";
    render(
      <ConnectionFormFields
        form={FORM}
        draft={draft()}
        onDraftChange={() => {}}
        fieldAccessory={(f: FormFieldView) => (f.secret ? null : <Switch aria-label={`Share ${f.label}`} />)}
        accessoryHeader={<Switch aria-label={COLUMN} label={COLUMN} />}
      />,
    );
    expect(screen.getAllByRole("checkbox", { name: COLUMN })).toHaveLength(1);
    const header = screen.getByRole("checkbox", { name: COLUMN });
    const first = screen.getByRole("checkbox", { name: "Share Account" });
    const last = screen.getByRole("checkbox", { name: "Share Warehouse" });
    expect(follows(header, first)).toBe(true);
    expect(follows(first, last)).toBe(true);
  });

  describe("a certificate field", () => {
    const PEM = "-----BEGIN CERTIFICATE-----\nMIIBkTCB+w==\n-----END CERTIFICATE-----\n";

    const ca = (over: Partial<FormFieldView> = {}): FormFieldView => ({
      name: "sslrootcert",
      label: "Certificate authority (PEM)",
      type: "password",
      required: false,
      secret: true,
      default: "",
      enum_values: [],
      help: "Paste the certificate contents, with \\n for line breaks.",
      placeholder: "",
      group: "",
      ...over,
    });

    const formWith = (field: FormFieldView): ConnectionFormView => ({
      note: "",
      auth_methods: [],
      shared_fields: [field],
    });

    it("takes a pasted PEM verbatim, newlines and all", () => {
      // A one-line password input cannot hold a multi-line certificate: what
      // came out would not be what went in.
      const onDraftChange = vi.fn();
      render(
        <ConnectionFormFields
          form={formWith(ca())}
          draft={draft()}
          onDraftChange={onDraftChange}
        />,
      );
      const box = screen.getByLabelText(/Certificate authority/);
      expect(box.tagName).toBe("TEXTAREA");

      fireEvent.change(box, { target: { value: PEM } });
      expect(onDraftChange).toHaveBeenCalledTimes(1);
      expect(onDraftChange.mock.calls[0][0].values.sslrootcert).toBe(PEM);
    });

    it("keeps the connector's own caption and the blank-keeps placeholder", () => {
      const KEEP = "Stored; leave blank to keep";
      render(
        <ConnectionFormFields
          form={formWith(ca({ placeholder: KEEP }))}
          draft={draft()}
          onDraftChange={() => {}}
        />,
      );
      const box = screen.getByLabelText(/Certificate authority/);
      expect(box).toHaveAttribute("placeholder", KEEP);
      expect(box).not.toBeRequired();
      expect(
        screen.getByText("Paste the certificate contents, with \\n for line breaks."),
      ).toBeInTheDocument();
    });

    it("opens masked and unmasks on Show, for a field the schema calls secret", () => {
      render(
        <ConnectionFormFields
          form={formWith(ca())}
          draft={draft({ values: { sslrootcert: PEM } })}
          onDraftChange={() => {}}
        />,
      );
      const box = screen.getByLabelText(/Certificate authority/);
      expect(box).toHaveAttribute("data-masked");

      fireEvent.click(screen.getByRole("button", { name: "Show" }));
      expect(box).not.toHaveAttribute("data-masked");
      expect(screen.getByRole("button", { name: "Hide" })).toBeInTheDocument();
    });

    it("offers no Show over an empty box, and one the moment a certificate is in it", () => {
      // The toggle masks characters; over nothing it visibly does nothing, which
      // is how it read on the add-connection form before anything was pasted.
      const { rerender } = render(
        <ConnectionFormFields form={formWith(ca())} draft={draft()} onDraftChange={() => {}} />,
      );
      const box = screen.getByLabelText(/Certificate authority/);
      expect(box).not.toHaveAttribute("data-masked");
      expect(screen.queryByRole("button", { name: /Show|Hide/ })).not.toBeInTheDocument();

      rerender(
        <ConnectionFormFields
          form={formWith(ca())}
          draft={draft({ values: { sslrootcert: PEM } })}
          onDraftChange={() => {}}
        />,
      );
      expect(screen.getByLabelText(/Certificate authority/)).toHaveAttribute("data-masked");
      expect(screen.getByRole("button", { name: "Show" })).toBeInTheDocument();
    });

    it("leaves a non-secret certificate box unmasked and untoggled", () => {
      // Druid and Elasticsearch ask for the same document without marking it a
      // secret; a Show on a field nothing hides is a control that does nothing.
      render(
        <ConnectionFormFields
          form={formWith(ca({ name: "ca_bundle", label: "CA bundle", type: "text", secret: false }))}
          draft={draft()}
          onDraftChange={() => {}}
        />,
      );
      const box = screen.getByLabelText(/CA bundle/);
      expect(box.tagName).toBe("TEXTAREA");
      expect(box).not.toHaveAttribute("data-masked");
      expect(screen.queryByRole("button", { name: "Show" })).not.toBeInTheDocument();
    });

    it("leaves a single-line secret on its one-line box", () => {
      // Only a field that carries a PEM earns the textarea — a password must
      // keep the reveal affordance the rest of the product gives it.
      render(<ConnectionFormFields form={FORM} draft={draft()} onDraftChange={() => {}} />);
      expect(screen.getByLabelText(/Token/).tagName).toBe("INPUT");
    });
  });

  it("renders a trailing field last, after the optional tail", () => {
    // The team form appends the deployment tier there so it reads as a closing
    // question, so its position is NOT a function of its required flag.
    const trailing: FormFieldView = {
      name: "environment",
      label: "Tier",
      type: "text",
      required: true,
      secret: false,
      default: "",
      enum_values: [],
      help: "",
      placeholder: "",
      group: "",
    };
    render(
      <ConnectionFormFields
        form={{ ...FORM, trailing_fields: [trailing] }}
        draft={draft()}
        onDraftChange={() => {}}
      />,
    );
    expect(follows(screen.getByLabelText(/Warehouse/), screen.getByLabelText(/Tier/))).toBe(true);
  });

  describe("choosing from fixed options", () => {
    const TIERS = ["prod", "staging", "dev", "local"];
    const CHOOSE = "Choose…"; // pins-source: the placeholder on a required select
    const NOT_SET = "Not set"; // pins-source: the placeholder on an optional select

    const tier = (over: Partial<FormFieldView> = {}): FormFieldView => ({
      name: "environment",
      label: "Tier",
      type: "select",
      required: true,
      secret: false,
      default: "",
      enum_values: TIERS,
      help: "",
      placeholder: "",
      group: "",
      ...over,
    });

    /** The control, reached through its own field block. A select trigger's
     *  accessible name IS whatever it is showing, so finding it by name would
     *  assume the very answer these cases are here to check. */
    const trigger = () =>
      within(screen.getByText("Tier").closest(".alk-field") as HTMLElement).getByRole("button");

    const renderSelect = (f: FormFieldView, values: Record<string, string> = {}) => {
      render(
        <ConnectionFormFields
          form={{ note: "", auth_methods: [], shared_fields: [f] }}
          draft={draft({ values })}
          onDraftChange={vi.fn()}
        />,
      );
    };

    /** The one button whose ACCESSIBLE NAME carries every `part` — the name a
     *  screen reader would read out, computed by `getByRole` rather than read off
     *  an attribute, because no single attribute holds it. */
    const namedTrigger = (...parts: string[]) =>
      screen.getByRole("button", { name: (n: string) => parts.every((p) => n.includes(p)) });

    /** A controlled host, so a pick lands in the draft the way a page's would. */
    function SelectHost({ field: f }: { field: FormFieldView }) {
      const [d, setD] = useState<ConnectionDraft>(draft());
      return (
        <ConnectionFormFields
          form={{ note: "", auth_methods: [], shared_fields: [f] }}
          draft={d}
          onDraftChange={setD}
        />
      );
    }

    it.each([
      ["required", true],
      ["optional", false],
    ])("announces which field a %s select belongs to", (_label, required) => {
      // Named by its content alone the control announced "Choose…": a
      // screen-reader user tabbing the form heard a placeholder with no field
      // attached to it.
      renderSelect(tier({ required }));
      expect(namedTrigger("Tier")).toBe(trigger());
    });

    it("points the field's label at the trigger, so the label reaches the control", () => {
      renderSelect(tier());
      // The label carries a required mark beside the word, so match its lead.
      expect(screen.getByLabelText(/^Tier/)).toBe(trigger());
    });

    it.each([
      ["nothing is answered yet", undefined, CHOOSE],
      ["an option has been picked", "staging", "staging"],
    ])("announces what it is showing while %s", (_label, pick, showing) => {
      // A control that announces its field but not its value tells a
      // screen-reader user WHICH question this is and nothing about the answer,
      // which is the half they cannot see.
      render(<SelectHost field={tier()} />);
      if (pick !== undefined) {
        fireEvent.click(trigger());
        fireEvent.click(screen.getByRole("menuitemradio", { name: pick }));
      }
      expect(trigger()).toHaveTextContent(showing);
      expect(namedTrigger("Tier", showing)).toBe(trigger());
    });

    it("marks an optional one unset rather than picking for the user", () => {
      renderSelect(tier({ required: false }));

      expect(trigger()).toHaveTextContent(NOT_SET);
      // Asserting the absence of the first option alone would pass on a
      // fallback to the second.
      for (const value of TIERS) expect(trigger()).not.toHaveTextContent(value);
    });

    it("still offers every option the connector declared", () => {
      // The placeholder is an extra row, not a replacement for the first one.
      renderSelect(tier());
      fireEvent.click(trigger());

      for (const value of TIERS) {
        expect(screen.getByRole("menuitemradio", { name: value })).toBeInTheDocument();
      }
      expect(screen.getByRole("menuitemradio", { name: CHOOSE })).toBeInTheDocument();
    });
  });

  it("filters the method picker via methodFilter", () => {
    const twoMethods: ConnectionFormView = {
      ...FORM,
      auth_methods: [
        ...FORM.auth_methods,
        { name: "oauth", label: "Browser sign-in", oauth: null, fields: [], team_capable: true },
      ],
    };
    render(
      <ConnectionFormFields
        form={twoMethods}
        draft={draft({ method: "oauth" })}
        onDraftChange={() => {}}
        methodFilter={(m) => m.team_capable === true}
      />,
    );
    // Only one team-capable method survives the filter → no picker at all.
    expect(screen.queryByText("Authentication")).not.toBeInTheDocument();
  });
});
