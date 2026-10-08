// Grow a machine's disk. The sizes, the price and whether the machine
// restarts are the server's (`disk_grow` on the machine, and its quote for the
// size typed); the dialog renders them, and shows a price only where the quote
// says machines are priced.

import { useState } from "react";

import { Modal, NumberInput, Stack } from "@alkera/ui";

import { machineBilling } from "../../../app/extensions/portal";
import { ApiError } from "../../../api/errors";
import { useDiskGrowQuote, useGrowDisk, type OrgMachineRead } from "../../../api/machines";

import { COPY, errorText, isConflict } from "./model";

export const GROW_COPY = {
  label: "Disk size (GB)",
  confirm: "Grow disk",
  restarts: "The machine restarts to grow its disk. Running chats finish first.",
} as const;

export const growTitle = (name: string): string => `Grow ${name}'s disk`;
export const growDone = (name: string, gb: number): string => `Growing ${name}'s disk to ${gb} GB.`;

export function GrowDiskDialog({
  open,
  machine,
  onClose,
  notify,
}: {
  open: boolean;
  machine: OrgMachineRead;
  onClose: () => void;
  notify: (message: string, ok: boolean) => void;
}) {
  const offer = machine.disk_grow ?? null;
  const [size, setSize] = useState("");
  const grow = useGrowDisk();
  const gb = Number(size);
  const whole = size !== "" && Number.isInteger(gb) && gb > 0;
  const quote = useDiskGrowQuote(machine.id, gb, open && whole && offer !== null);
  const refusedSize = quote.error instanceof ApiError && quote.error.status === 422;
  const range = offer ? `Between ${offer.min_gb} and ${offer.max_gb} GB.` : "";
  const sizeError = size !== "" && (!whole || refusedSize) ? range : undefined;
  const quoted = sizeError ? null : (quote.data ?? null);
  const refusal = quoted?.verdict === "refused" ? quoted.message : null;
  // What the new size costs, where an extension prices machines.
  const price = quoted ? (machineBilling()?.diskPriceLine(quoted) ?? null) : null;
  const close = () => {
    setSize("");
    grow.reset();
    onClose();
  };
  if (offer === null) return null;
  return (
    <Modal
      open={open}
      onClose={close}
      title={growTitle(machine.name)}
      size="sm"
      confirmLabel={GROW_COPY.confirm}
      confirmDisabled={quoted === null || refusal !== null}
      confirmBusy={grow.isPending}
      onConfirm={() =>
        grow.mutate(
          { machineId: machine.id, version: machine.version, volumeGb: gb },
          {
            onSuccess: () => {
              close();
              notify(growDone(machine.name, gb), true);
            },
            onError: (e) => {
              close();
              notify(isConflict(e) ? COPY.conflict : errorText(e, "Could not grow the disk."), false);
            },
          },
        )
      }
    >
      <Stack gap={3} align="stretch">
        <NumberInput
          label={GROW_COPY.label}
          mode="integer"
          value={size}
          placeholder={String(offer.min_gb)}
          error={sizeError}
          description={sizeError ? undefined : range}
          onValueChange={setSize}
        />
        {offer.restarts ? <p className="alk-meta">{GROW_COPY.restarts}</p> : null}
        {price && refusal === null ? (
          <p className="alk-meta" data-testid="grow-price">
            {price}
          </p>
        ) : null}
        {refusal ? (
          <p className="alk-meta" role="alert">
            {refusal}
          </p>
        ) : null}
      </Stack>
    </Modal>
  );
}
