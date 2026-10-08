// Effort-ladder helpers — model logic shared by the composer and the settings surface.

/** The middle rung of a model's effort ladder — the neutral default when none is chosen. */
export function middleEffort(efforts: string[]): string | undefined {
  return efforts[Math.floor(efforts.length / 2)];
}

/** The words an effort value is shown as where it is not a plain word. */
const EFFORT_WORDS: Readonly<Record<string, string>> = {
  xhigh: "Extra high",
};

/** An effort value as a person reads it: "Low", "Extra high". A value this
 *  build has not met is shown as its own word, capitalised. */
export function effortLabel(value: string): string {
  const known = EFFORT_WORDS[value.toLowerCase()];
  if (known !== undefined) return known;
  return value.charAt(0).toUpperCase() + value.slice(1);
}
