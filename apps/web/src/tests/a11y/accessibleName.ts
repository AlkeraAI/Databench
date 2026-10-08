// What a screen reader would announce a control as, computed from the DOM the
// page actually rendered.
//
// A static sweep of the source cannot answer this: a label may arrive as a
// child, through `aria-labelledby`, or from an `alt` on the glyph, and a
// control that reads fine in one state reads as nothing in another. So the
// audit renders the surface and asks the tree, which is also what makes it fail
// when a name is taken away rather than when a file is edited.
//
// This is a reduced accname computation — the parts that decide whether a
// control has a name at all, in the order the specification resolves them. It
// does not model `aria-owns`, CSS-generated content, or the full text
// alternative of embedded controls, none of which this product names anything
// by.

/** Every element a person can operate. A `separator` counts only when it is in
 *  the tab order — a plain divider is decoration. */
export const INTERACTIVE_SELECTOR = [
  "button",
  "a[href]",
  "summary",
  "input:not([type='hidden'])",
  "select",
  "textarea",
  "[role='button']",
  "[role='link']",
  "[role='menuitem']",
  "[role='menuitemcheckbox']",
  "[role='menuitemradio']",
  "[role='tab']",
  "[role='checkbox']",
  "[role='radio']",
  "[role='switch']",
  "[role='slider']",
  "[role='separator'][tabindex]",
  "[role='combobox']",
].join(", ");

const PRESENTATIONAL = new Set(["none", "presentation"]);

/** Whether the accessibility tree can see this element at all. A subtree behind
 *  `aria-hidden`, `hidden`, or `display: none` announces nothing, so a control
 *  inside it is not missing a name — it is simply not there. */
function hiddenFromTree(element: Element): boolean {
  let node: Element | null = element;
  while (node) {
    if (node.getAttribute("aria-hidden") === "true") return true;
    if (node.hasAttribute("hidden")) return true;
    if (node instanceof HTMLElement && node.style.display === "none") return true;
    node = node.parentElement;
  }
  return false;
}

/** The text a subtree contributes to a name: its own text, plus the alternative
 *  text of any glyph inside it, with `aria-hidden` branches dropped. */
function textFrom(element: Element): string {
  const parts: string[] = [];
  const walk = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      parts.push(node.textContent ?? "");
      return;
    }
    if (!(node instanceof Element)) return;
    if (node.getAttribute("aria-hidden") === "true") return;
    const label = node.getAttribute("aria-label");
    if (label && label.trim()) {
      parts.push(label);
      return;
    }
    if (node instanceof HTMLImageElement && node.alt.trim()) {
      parts.push(node.alt);
      return;
    }
    // An SVG names itself with a <title> child; the walk below picks that up as
    // text, which is exactly what a reader announces.
    for (const child of Array.from(node.childNodes)) walk(child);
  };
  walk(element);
  return parts.join(" ").replace(/\s+/g, " ").trim();
}

/** The label elements pointing at this control, in document order. */
function labelText(element: Element): string {
  if (!(element instanceof HTMLInputElement || element instanceof HTMLSelectElement || element instanceof HTMLTextAreaElement)) {
    return "";
  }
  const labels = element.labels ? Array.from(element.labels) : [];
  const fromFor = element.id
    ? Array.from(element.ownerDocument.querySelectorAll(`label[for="${CSS.escape(element.id)}"]`))
    : [];
  const all = new Set<Element>([...labels, ...fromFor]);
  return Array.from(all)
    .map((l) => textFrom(l))
    .filter(Boolean)
    .join(" ")
    .trim();
}

/**
 * The name this element would be announced by, or the empty string when it
 * would be announced by nothing.
 *
 * Resolution order, as the specification has it: `aria-labelledby`, then
 * `aria-label`, then the host-language label (a `<label>` for a field, the
 * content for a button or a link), then `title`, then `placeholder` for a field
 * that has nothing else.
 */
export function accessibleName(element: Element): string {
  const labelledBy = element.getAttribute("aria-labelledby");
  if (labelledBy) {
    const named = labelledBy
      .split(/\s+/)
      .map((id) => element.ownerDocument.getElementById(id))
      .filter((n): n is HTMLElement => n != null)
      .map((n) => textFrom(n))
      .filter(Boolean)
      .join(" ")
      .trim();
    if (named) return named;
  }
  const label = element.getAttribute("aria-label");
  if (label && label.trim()) return label.trim();

  const fromLabel = labelText(element);
  if (fromLabel) return fromLabel;

  if (element instanceof HTMLInputElement && (element.type === "submit" || element.type === "button" || element.type === "reset")) {
    if (element.value.trim()) return element.value.trim();
  }
  if (!(element instanceof HTMLInputElement) && !(element instanceof HTMLSelectElement)) {
    const content = textFrom(element);
    if (content) return content;
  }

  const title = element.getAttribute("title");
  if (title && title.trim()) return title.trim();

  const placeholder = element.getAttribute("placeholder");
  if (placeholder && placeholder.trim()) return placeholder.trim();

  return "";
}

export interface UnnamedControl {
  /** `button.alk-iconbtn`, `a[href]`, … — enough to find it in the source. */
  where: string;
  /** The opening tag, so a failure names the control rather than a count. */
  markup: string;
}

function describe(element: Element): string {
  const tag = element.tagName.toLowerCase();
  const role = element.getAttribute("role");
  const classes = element.getAttribute("class");
  const first = classes ? `.${classes.trim().split(/\s+/)[0]}` : "";
  return `${tag}${role ? `[role=${role}]` : ""}${first}`;
}

/**
 * Every operable control inside `root` that the accessibility tree can see and
 * that would be announced by nothing.
 *
 * A control whose role is `none`/`presentation` is skipped (it has opted out of
 * the tree), as is anything inside a hidden branch.
 */
export function unnamedControls(root: ParentNode): UnnamedControl[] {
  const found: UnnamedControl[] = [];
  for (const element of Array.from(root.querySelectorAll(INTERACTIVE_SELECTOR))) {
    const role = element.getAttribute("role");
    if (role && PRESENTATIONAL.has(role)) continue;
    if (hiddenFromTree(element)) continue;
    if (accessibleName(element)) continue;
    found.push({ where: describe(element), markup: element.outerHTML.slice(0, 200) });
  }
  return found;
}

/** The audit's failure message: one line per nameless control. */
export function reportUnnamed(surface: string, found: UnnamedControl[]): string {
  return [
    `${surface}: ${found.length} control(s) a screen reader would announce as nothing.`,
    ...found.map((f) => `  ${f.where} — ${f.markup}`),
  ].join("\n");
}
