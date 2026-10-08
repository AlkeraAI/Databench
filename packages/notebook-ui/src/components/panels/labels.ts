// How the panels name cells, graph errors and sizes.

import type { DocCell } from "../../model/types";

/** A cell's name, or its position when it has none. */
export function cellLabel(cell: Pick<DocCell, "name">, index: number): string {
  return cell.name && cell.name !== "_" ? cell.name : `Cell ${index + 1}`;
}

const GRAPH_ERRORS: Record<string, string> = {
  multiple_definitions: "Defines a name another cell also defines",
  cycle: "Part of a cycle",
};

/** A graph error code in words; an unknown code is shown with spaces. */
export function graphErrorLabel(error: string): string {
  // An error may name what it is about: `multiple_definitions:df`, or
  // `multiple_definitions: df` as a cell's own label reads.
  const [rawCode = "", rawName] = error.split(":", 2);
  const code = rawCode.trim();
  const name = rawName?.trim();
  const known = GRAPH_ERRORS[code];
  const words = code.replace(/_/g, " ").trim();
  const label = known ?? words.charAt(0).toUpperCase() + words.slice(1);
  return name ? `${label}: ${name}` : label;
}

/** "512 B", "1.5 KB", "12 MB", "1.2 GB": 1024-based, one decimal under ten. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  if (unit === 0) return `${bytes} B`;
  const shown = value < 10 ? Math.round(value * 10) / 10 : Math.round(value);
  return `${shown} ${units[unit]}`;
}
