// Stands in for `sanitize-html` inside the frame (the bundler aliases it), for
// the one caller there: ManagerBase.inline_sanitize, which cleans widget
// descriptions. The frame has a DOM, so this walks a parsed fragment instead
// of carrying postcss and htmlparser2. Semantics follow sanitize-html's for
// the options that caller passes: a tag outside the allowlist is unwrapped
// (its text kept) except the tags whose content is not text, which go whole;
// attributes are kept per tag and `*`, with `prefix-*` wildcards; URLs in
// `href` and `src` must be relative or use an allowed scheme.

export interface SanitizeOptions {
  allowedTags?: string[];
  allowedAttributes?: Record<string, string[]>;
  allowedSchemes?: string[];
}

const DROP_WITH_CONTENT = new Set(["script", "style", "textarea", "option", "noscript"]);
const DEFAULT_SCHEMES = ["http", "https", "ftp", "mailto", "tel"];
const URL_ATTRIBUTES = new Set(["href", "src", "action", "cite", "srcset", "background"]);

function attributeAllowed(name: string, patterns: string[]): boolean {
  return patterns.some((p) => (p.endsWith("*") ? name.startsWith(p.slice(0, -1)) : p === name));
}

function urlAllowed(value: string, schemes: string[]): boolean {
  // Strip what browsers ignore before deciding the scheme (tabs, newlines,
  // control characters, leading spaces), as sanitize-html does.
  // eslint-disable-next-line no-control-regex -- strips control characters
  const cleaned = value.replace(/[\u0000- \u007f-\u009f]/g, "").toLowerCase();
  if (cleaned.startsWith("//")) return schemes.includes("http") || schemes.includes("https");
  const match = /^([a-z][a-z0-9+.-]*):/.exec(cleaned);
  if (!match) return true; // relative
  return schemes.includes(match[1]);
}

function clean(node: Node, options: Required<SanitizeOptions>): void {
  for (const child of Array.from(node.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE) continue;
    if (child.nodeType !== Node.ELEMENT_NODE) {
      child.remove(); // comments, processing instructions
      continue;
    }
    const el = child as Element;
    const tag = el.tagName.toLowerCase();
    if (DROP_WITH_CONTENT.has(tag) && !options.allowedTags.includes(tag)) {
      el.remove();
      continue;
    }
    clean(el, options);
    if (!options.allowedTags.includes(tag)) {
      el.replaceWith(...Array.from(el.childNodes));
      continue;
    }
    const allowed = [...(options.allowedAttributes["*"] ?? []), ...(options.allowedAttributes[tag] ?? [])];
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      const keep =
        attributeAllowed(name, allowed) && (!URL_ATTRIBUTES.has(name) || urlAllowed(attr.value, options.allowedSchemes));
      if (!keep) el.removeAttribute(attr.name);
    }
  }
}

export default function sanitize(html: string, options: SanitizeOptions = {}): string {
  const resolved: Required<SanitizeOptions> = {
    allowedTags: (options.allowedTags ?? []).map((t) => t.toLowerCase()),
    allowedAttributes: options.allowedAttributes ?? {},
    allowedSchemes: options.allowedSchemes ?? DEFAULT_SCHEMES,
  };
  // A <template> parses without running scripts or loading images.
  const template = document.createElement("template");
  template.innerHTML = html;
  clean(template.content, resolved);
  const holder = document.createElement("div");
  holder.appendChild(template.content);
  return holder.innerHTML;
}
