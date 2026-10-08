// The subset of semver ranges the patch registry needs: `*`, exact versions,
// `^`, `~`, and space-separated comparator sets (`>=1.0.0 <2`), joined by `||`.

type Version = [number, number, number];

export function parseVersion(text: string): Version | null {
  const match = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$/.exec(text.trim());
  if (!match) return null;
  return [Number(match[1]), Number(match[2] ?? 0), Number(match[3] ?? 0)];
}

function compare(a: Version, b: Version): number {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
}

function satisfiesComparator(version: Version, comparator: string): boolean {
  if (comparator === "*" || comparator === "x" || comparator === "") return true;
  const match = /^(\^|~|>=|<=|>|<|=)?(.+)$/.exec(comparator);
  if (!match) return false;
  const op = match[1] ?? "=";
  const target = parseVersion(match[2]);
  if (!target) return false;
  const c = compare(version, target);
  switch (op) {
    case "^": {
      if (c < 0) return false;
      if (target[0] > 0) return version[0] === target[0];
      if (target[1] > 0) return version[0] === 0 && version[1] === target[1];
      return version[0] === 0 && version[1] === 0 && version[2] === target[2];
    }
    case "~":
      return c >= 0 && version[0] === target[0] && version[1] === target[1];
    case ">=":
      return c >= 0;
    case "<=":
      return c <= 0;
    case ">":
      return c > 0;
    case "<":
      return c < 0;
    default:
      return c === 0;
  }
}

/** Whether `version` (a concrete version) lies in `range`. */
export function satisfies(version: string, range: string): boolean {
  const parsed = parseVersion(version);
  if (!parsed) return false;
  return range.split("||").some((set) =>
    set
      .trim()
      .split(/\s+/)
      .every((comparator) => satisfiesComparator(parsed, comparator)),
  );
}
