// Markdown to safe HTML: markdown-it, then an allowlist sanitizer, then KaTeX.
//
// Math is lifted out before markdown-it sees the text (so `_` and `*` inside
// a formula are not read as emphasis) and replaced by private-use
// placeholders. The Markdown HTML is sanitized with a strict allowlist; only
// then are the placeholders in text nodes swapped for KaTeX's own markup,
// itself sanitized with KaTeX's profile. A placeholder that lands anywhere
// but a text node (an attribute) goes back to its TeX source as plain text,
// so rendered math never reaches an attribute.

import DOMPurify, { type Config, type DOMPurify as DOMPurifyInstance } from "dompurify";
import katex from "katex";
import MarkdownIt from "markdown-it";

export interface MathSpan {
  tex: string;
  display: boolean;
}

const OPEN = "";
const CLOSE = "";
const PLACEHOLDER = /(\d+)/g;

/** Finds `$$...$$` and `$...$` outside code, returning the text with each
 *  formula replaced by a placeholder and the formulas in order. `\$` is a
 *  literal dollar. Inline math follows Pandoc's rule: the opening `$` is not
 *  followed by a space, the closing `$` is not preceded by a space and not
 *  followed by a digit (so "$5 and $10" stays text). */
export function extractMath(markdown: string): { text: string; math: MathSpan[] } {
  const math: MathSpan[] = [];
  const out: string[] = [];
  const placeholder = (tex: string, display: boolean) => {
    math.push({ tex, display });
    return `${OPEN}${math.length - 1}${CLOSE}`;
  };
  const lines = markdown.replace(/[]/g, "").split("\n");
  let fence: string | null = null;
  let buffer: string[] = [];

  const flush = () => {
    if (buffer.length) out.push(replaceInProse(buffer.join("\n"), placeholder));
    buffer = [];
  };

  for (const line of lines) {
    const fenceMatch = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (fence === null && fenceMatch) {
      flush();
      fence = fenceMatch[1];
      out.push(line);
      continue;
    }
    if (fence !== null) {
      out.push(line);
      if (line.trim().startsWith(fence[0].repeat(fence.length)) && line.trim().replace(new RegExp(`\\${fence[0]}`, "g"), "") === "") {
        fence = null;
      }
      continue;
    }
    buffer.push(line);
  }
  flush();
  return { text: out.join("\n"), math };
}

function replaceInProse(text: string, placeholder: (tex: string, display: boolean) => string): string {
  let result = "";
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\" && text[i + 1] === "$") {
      result += "\\$";
      i += 2;
      continue;
    }
    if (ch === "`") {
      // An inline code span: copy it untouched up to the matching run.
      let run = 1;
      while (text[i + run] === "`") run += 1;
      const ticks = "`".repeat(run);
      const end = text.indexOf(ticks, i + run);
      if (end >= 0) {
        result += text.slice(i, end + run);
        i = end + run;
        continue;
      }
      result += ticks;
      i += run;
      continue;
    }
    if (ch === "$" && text[i + 1] === "$") {
      const end = text.indexOf("$$", i + 2);
      if (end > i + 2) {
        result += placeholder(text.slice(i + 2, end).trim(), true);
        i = end + 2;
        continue;
      }
    } else if (ch === "$") {
      const next = text[i + 1];
      if (next !== undefined && !/\s/.test(next)) {
        let j = i + 1;
        let found = -1;
        while (j < text.length) {
          if (text[j] === "\\") {
            j += 2;
            continue;
          }
          if (text[j] === "\n" && text[j + 1] === "\n") break;
          if (text[j] === "$") {
            if (!/\s/.test(text[j - 1]) && !/\d/.test(text[j + 1] ?? "")) found = j;
            break;
          }
          j += 1;
        }
        if (found > i + 1) {
          result += placeholder(text.slice(i + 1, found), false);
          i = found + 1;
          continue;
        }
      }
    }
    result += ch;
    i += 1;
  }
  return result;
}

/** One formula as sanitized KaTeX markup. Bad TeX renders as KaTeX's red
 *  error text rather than throwing. */
export function renderMath(span: MathSpan): string {
  const html = katex.renderToString(span.tex, {
    displayMode: span.display,
    throwOnError: false,
    trust: false,
    strict: "ignore",
    maxExpand: 500,
    maxSize: 50,
    output: "htmlAndMathml",
  });
  return katexPurifier().sanitize(html, KATEX_CONFIG) as string;
}

// -- the allowlist ------------------------------------------------------------

const TAGS = [
  "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li",
  "strong", "em", "b", "i", "s", "del", "code", "pre", "a", "img",
  "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
  "details", "summary", "sub", "sup", "span",
];
const ATTRS = ["href", "title", "alt", "src", "class", "colspan", "rowspan", "start", "open", "width", "height"];

const DATA_IMAGE = /^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$/i;
const SAFE_LINK = /^(?:https?:|mailto:|#|\/(?!\/)|\.{1,2}\/|[^:/?#]*(?:[/?#]|$))/i;
const ALIGN_CLASS = /^nb-md-align-(left|right|center)$/;

const MARKDOWN_CONFIG: Config = {
  ALLOWED_TAGS: TAGS,
  ALLOWED_ATTR: ATTRS,
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false,
  ALLOW_UNKNOWN_PROTOCOLS: false,
  // Links and images are judged by the hook below; this only lets data URIs
  // through to it (DOMPurify otherwise drops every data: src first).
  ADD_DATA_URI_TAGS: ["img"],
  KEEP_CONTENT: true,
};

const KATEX_CONFIG: Config = {
  USE_PROFILES: { html: true, svg: true, mathMl: true },
  // KaTeX keeps the TeX source in a MathML annotation for screen readers.
  ADD_TAGS: ["semantics", "annotation"],
  FORBID_TAGS: ["a", "img", "image", "use", "foreignObject", "style"],
  FORBID_ATTR: ["href", "xlink:href", "src"],
};

let instance: DOMPurifyInstance | null = null;
let katexInstance: DOMPurifyInstance | null = null;

// KaTeX's markup needs inline styles, SVG and MathML, which the Markdown
// allowlist refuses; it gets its own instance without the Markdown hooks.
function katexPurifier(): DOMPurifyInstance {
  katexInstance ??= DOMPurify(window);
  return katexInstance;
}

function purifier(): DOMPurifyInstance {
  if (instance) return instance;
  const purify = DOMPurify(window);
  purify.addHook("uponSanitizeAttribute", (node, data) => {
    if (data.attrName !== "class") return;
    const tag = node.nodeName.toLowerCase();
    const classes = data.attrValue.split(/\s+/).filter(Boolean);
    let keep: string[] = [];
    if (tag === "span") keep = classes;
    else if (tag === "code") keep = classes.filter((c) => /^language-[\w-]+$/.test(c));
    else if (tag === "th" || tag === "td") keep = classes.filter((c) => ALIGN_CLASS.test(c));
    if (keep.length === 0) data.keepAttr = false;
    else data.attrValue = keep.join(" ");
  });
  purify.addHook("afterSanitizeAttributes", (node) => {
    if (!(node instanceof Element)) return;
    const tag = node.nodeName.toLowerCase();
    if (tag === "img") {
      const src = node.getAttribute("src") ?? "";
      if (!DATA_IMAGE.test(src.trim())) node.remove();
      return;
    }
    if (tag === "a") {
      const href = (node.getAttribute("href") ?? "").trim();
      if (href && !SAFE_LINK.test(href)) node.removeAttribute("href");
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
  instance = purify;
  return purify;
}

const md = new MarkdownIt({ html: true, linkify: true, typographer: false });

// Tables align by an inline style, which the sanitizer strips; carry the
// alignment as a class instead.
md.core.ruler.push("nb_align_class", (state) => {
  for (const token of state.tokens) {
    if (token.type !== "th_open" && token.type !== "td_open") continue;
    const style = token.attrGet("style");
    const match = style ? /text-align:\s*(left|right|center)/.exec(style) : null;
    if (!token.attrs) continue;
    token.attrs = token.attrs.filter(([name]) => name !== "style");
    if (match) token.attrJoin("class", `nb-md-align-${match[1]}`);
  }
});

function restoreSource(value: string, math: MathSpan[]): string {
  return value.replace(PLACEHOLDER, (_, n: string) => {
    const span = math[Number(n)];
    if (!span) return "";
    return span.display ? `$$${span.tex}$$` : `$${span.tex}$`;
  });
}

/** Markdown to sanitized HTML with rendered math. */
export function renderMarkdown(markdown: string): string {
  const { text, math } = extractMath(markdown);
  const purify = purifier();
  const fragment = purify.sanitize(md.render(text), { ...MARKDOWN_CONFIG, RETURN_DOM_FRAGMENT: true });
  if (math.length > 0) substituteMath(fragment, math);
  const holder = document.createElement("div");
  holder.appendChild(fragment);
  return holder.innerHTML;
}

function substituteMath(root: DocumentFragment, math: MathSpan[]): void {
  const doc = root.ownerDocument;
  // Attributes first: math in an attribute goes back to its source text.
  for (const element of Array.from(root.querySelectorAll("*"))) {
    for (const attr of Array.from(element.attributes)) {
      if (attr.value.includes(OPEN)) element.setAttribute(attr.name, restoreSource(attr.value, math));
    }
  }
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if ((node as Text).data.includes(OPEN)) texts.push(node as Text);
  }
  for (const node of texts) {
    // Inside code (a placeholder never should be, but be exact) keep the source.
    if (node.parentElement?.closest("code, pre")) {
      node.data = restoreSource(node.data, math);
      continue;
    }
    const parts = node.data.split(PLACEHOLDER);
    const replacement = doc.createDocumentFragment();
    parts.forEach((part, index) => {
      if (index % 2 === 0) {
        if (part) replacement.appendChild(doc.createTextNode(part));
        return;
      }
      const span = math[Number(part)];
      if (!span) return;
      const template = doc.createElement("template");
      template.innerHTML = renderMath(span);
      // A span either way: a block element inside a paragraph would split it.
      const wrapper = doc.createElement("span");
      wrapper.className = span.display ? "nb-md-math nb-md-math--display" : "nb-md-math";
      wrapper.appendChild(template.content);
      replacement.appendChild(wrapper);
    });
    node.replaceWith(replacement);
  }
}
