/**
 * One reading of a size for every preview surface.
 *
 * The Files list reads sizes in decimal units ("3.1 MB" for 3,145,795 bytes),
 * so a preview's own lines must too, or a header and the paging line under it
 * disagree about the same file. The rule is the list's: step up while the
 * figure is at least 1000 of the unit or still rounds to it, one decimal.
 */

const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

function round1(value: number): number {
  return Math.floor(value * 10 + 0.5) / 10;
}

export function formatBytes(bytes: number): string {
  let index = 0;
  let value = Math.max(0, bytes);
  while (index < UNITS.length - 1) {
    if (value < 1000 && round1(value) < 1000) break;
    value /= 1000;
    index += 1;
  }
  const unit = UNITS[index];
  if (unit === "B") return `${Math.round(value)} B`;
  const rounded = round1(value);
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)} ${unit}`;
}
