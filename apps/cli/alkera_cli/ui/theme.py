"""Adaptive light-academia palette + the themed console.

Background detection order: ``ALKERA_THEME`` env override → OSC 11
terminal probe → ``COLORFGBG`` heuristic → dark fallback. The OSC 11
probe is the only place the CLI touches termios; it runs once at chat
entry — before the Textual app takes over the tty — and always restores
the tty state.

The module-level ``console`` is constructed with NO ``file=`` argument
on purpose: rich resolves ``sys.stdout`` lazily in that case, so a host
that redirects stdout (the Textual driver, a captured test harness) is
honored instead of bypassed. Pinning a file handle here would silently
defeat that redirection.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass
from typing import Literal

from rich.console import Console
from rich.theme import Theme

try:
    import termios
    import tty

    _HAS_TTY_PROBE = True
except ImportError:  # termios/tty are POSIX-only; absent on Windows
    _HAS_TTY_PROBE = False

BackgroundKind = Literal["light", "dark"]

OSC11_TIMEOUT_S = 0.15
"""How long to wait for the terminal's OSC 11 reply before giving up."""

_OSC11_REPLY = re.compile(rb"\]11;rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")


@dataclass(frozen=True)
class Palette:
    """Semantic color roles for one background kind (truecolor hex).

    Fields are grouped by shade family. The text ladder is ordered by
    contrast against the canvas (primary > secondary > tertiary)."""

    # Text ladder — foreground tones, brightest to faintest.
    primary_text: str
    secondary_text: str
    tertiary_text: str
    # Olive / brand accents — the APP CHROME (banner, mode chip, composer
    # border, the block cursor everywhere outside the chat transcript).
    brand: str
    brand_strong: str
    # Warm dimmed-pastel accents — the CHAT TRANSCRIPT's hue mix, grouped by
    # tool category so the conversation reads as a muted, colorful index
    # instead of a wall of olive. Each is tuned PER-CANVAS to clear WCAG
    # 4.5:1 on the canvas it draws on (tool icons + prose headings sit on the
    # canvas, not the code band). The chrome stays `brand`; only the
    # transcript draws these.
    accent_file: str  # read / write  — blush
    accent_exec: str  # bash / sql    — eucalyptus
    accent_search: str  # web / tool search — periwinkle
    accent_prose: str  # markdown headings + the rule-notice emphasis — peach
    # True green — completion / success (distinct from the olive brand).
    success: str
    # Other hues.
    warning: str
    danger: str
    info: str
    # Read-only permission mode — a soft purple pastel, off the olive/blue/orange
    # the other modes use so "analyst mode" reads as its own colour (the mode
    # chip, the focus border, and the mode-menu row wash all take it).
    mode_read_only: str
    # Structure — frames + dividers + code surfaces.
    outline: str
    hairline: str
    code_bg: str
    # Diff washes — an added / removed line in a unified diff carries a green /
    # red TINT (a step off `code_bg`), and the line text keeps its normal code
    # ink ON the tint (NOT recolored). The convention is git's: the highlight
    # signals the change, the foreground stays readable. `primary_text` clears
    # WCAG 4.5:1 on both tints (a dimmer tone would not — paint changed lines in
    # primary ink, keep the line-number gutter OFF the band).
    diff_add_bg: str
    diff_del_bg: str
    # Cursor + selection washes. The olive `chosen_bg` doubles as the
    # list/menu/settings cursor wash (no "selected" state on those surfaces);
    # interrupt cards keep the neutral `cursor_bg_gray` for their cursor.
    cursor_bg_gray: str
    chosen_bg: str
    chosen_cursor_bg: str
    danger_bg: str
    canvas: str


LIGHT = Palette(
    # Text ladder, ordered by contrast vs canvas: primary 10.2, secondary 5.31
    # (the warm taupe formerly `tool` — the user free-text echo), tertiary 4.79
    # (hints, descriptions, placeholders, thinking, row numbers; ≥4.5).
    primary_text="#3f3a33",
    secondary_text="#6e6353",
    tertiary_text="#746a5c",
    brand="#666b44",
    brand_strong="#565c39",
    # Warm dimmed pastels for the transcript, darkened for the paper canvas so
    # each clears 4.5:1 on it (measured): blush 4.98, eucalyptus 4.63,
    # periwinkle 5.13, peach 4.97.
    accent_file="#a0505f",
    accent_exec="#547654",
    accent_search="#56649c",
    accent_prose="#955b30",
    success="#4f7a52",
    # Amber, darkened to clear 4.5:1 on paper (4.88 — it carries the compaction
    # gauge, a quantitative reading, so it must clear the text gate not just the
    # 3:1 non-text one; the old #8a6c26 measured 4.46).
    warning="#856526",
    danger="#a3493d",
    # The extension's plan blue (historically oklch(60% 0.075 245) = #5985aa)
    # measures only 3.54:1 on paper — this is the same hue/chroma at 50% OKLCH
    # lightness (5.35:1). TODO: generate these values from the ui tokens
    # (packages/ui/src/theme/tokens.css) instead of mirroring by hand.
    info="#3c688b",
    # Read-only mode: a medium purple darkened for the paper canvas (5.06:1 —
    # the chip + focus border are content). Distinct in hue from the brand
    # olive, plan blue, and bypass orange.
    mode_read_only="#7a55a3",
    # Frames + scrollbars (heavier line) ≥3:1; dividers/chips (lighter line);
    # code/keycap surface (deliberately == `cursor_bg_gray`, decoupled so a
    # retune of one doesn't drag the other).
    outline="#8c826f",
    hairline="#d8d1c1",
    code_bg="#e9e3d3",
    # Diff washes off the paper code band (success/danger over `code_bg` @0.30):
    # add 1.42:1, del 1.50:1 off `code_bg` (a visible tint), and `primary_text`
    # on them 6.18 / 5.88 (≥4.5 — the kept code ink stays readable).
    diff_add_bg="#bbc3ac",
    diff_del_bg="#d4b5a6",
    # Cursor wash on the interrupt cards (permission / question / plan), kept a
    # neutral GRAY — the olive read "weird" there.
    cursor_bg_gray="#e9e3d3",
    # Selected (chosen) answer / checked multi-box AND the list/menu/settings
    # cursor wash: the MIDDLE olive — a stronger brand-over-canvas blend. Dark
    # ink on it = 6.45:1; vs canvas 1.58 (a present, clearly-olive band).
    chosen_bg="#bfca8a",
    # Cursor ON a chosen row: `chosen_bg` lifted ~1.08 (#bfca8a → #ceda95) — a
    # gentle step up so the cursor never goes blank over a selection, yet
    # adjacent chosen+cursored multi rows don't clash. Vs `chosen_bg` 1.17.
    chosen_cursor_bg="#ceda95",
    # Red band for a destructive row (Quit / reject) — a soft PASTEL rose
    # (HSL ~8°/52%/86%), airier than the old translucent tint. Dark ink on it
    # = 7.67:1; vs canvas 1.45 (a gentle pastel wash).
    danger_bg="#eecec9",
    canvas="#f7f3ea",
)
"""Light background: warm ink taupe text, dried-olive accents.
Contrast vs the paper canvas (measured): brand 5.60, strong 7.03,
secondary_text 5.31, tertiary_text 4.79 (≥4.5 — previews/hints are real
content), outline 3.42 (≥3:1 non-text with margin). Transcript pastels vs
canvas: accent_file 4.98, accent_exec 4.63, accent_search 5.13,
accent_prose 4.97 (all ≥4.5 — icons + headings are content)."""

DARK = Palette(
    # Text ladder, ordered by contrast vs canvas: primary 12.5, secondary 7.56
    # (the warm taupe formerly `tool` — the user free-text echo), tertiary 5.09
    # (hints, descriptions, placeholders, thinking, row numbers).
    primary_text="#d9d3c7",
    secondary_text="#b0a48e",
    tertiary_text="#8d8577",
    brand="#939a68",
    brand_strong="#aeb37e",
    # Warm dimmed pastels for the transcript, lightened for the near-black
    # canvas so each clears 4.5:1 on it (measured): blush 7.90, eucalyptus
    # 9.26, periwinkle 7.88, peach 8.42.
    accent_file="#d39aa3",
    accent_exec="#9cc0a0",
    accent_search="#9aa6e0",
    accent_prose="#d6a579",
    success="#94b285",
    warning="#c8a45c",
    danger="#c87f70",
    # The extension's plan token (historically oklch(60% 0.075 245) = #5985aa)
    # lifted ~4% to #608aad: the raw value clears 4.74:1 on the canvas but only
    # 4.39 on the raised `$panel` band the plan INTERRUPT card draws its bold
    # title on — this lift carries it to 4.70 there (and improves the canvas to
    # ~5.0). Re-sync if the ui tokens ever auto-generate these.
    info="#608aad",
    # Read-only mode: a soft lavender lightened for the near-black canvas
    # (8.16:1). Reads as its own pastel — clearly not the olive brand, the
    # plan blue, or the bypass orange.
    mode_read_only="#b79ad6",
    # Frames + scrollbars (heavier line); dividers/chips (lighter line); code/
    # keycap surface (deliberately == `cursor_bg_gray`, decoupled).
    outline="#6b6456",
    hairline="#46423a",
    code_bg="#34322a",
    # Diff washes off the near-black code band (success/danger over `code_bg`
    # @0.26): add 1.61:1, del 1.45:1 off `code_bg` (a visible tint), and
    # `primary_text` on them 5.36 / 5.93 (≥4.5 — the kept code ink stays readable).
    diff_add_bg="#4d5342",
    diff_del_bg="#5a463c",
    # Cursor wash on the interrupt cards (permission / question / plan), kept a
    # neutral GRAY — the olive read "weird" there.
    cursor_bg_gray="#34322a",
    # Selected (chosen) answer / checked multi-box AND the list/menu/settings
    # cursor wash: the MIDDLE olive — distinct from the gray interrupt cursor and
    # the bright `brand_strong` text. Parchment on it = 4.58:1; vs canvas 2.72.
    chosen_bg="#5a5d40",
    # Cursor ON a chosen row: `chosen_bg` lifted ~1.08 (#5a5d40 → #616445) — a
    # gentle step up so the cursor never goes blank over a selection, yet
    # adjacent chosen+cursored multi rows don't clash. Vs `chosen_bg` 1.11.
    chosen_cursor_bg="#616445",
    # Red band for a destructive row (Quit / reject) — a soft PASTEL rose
    # (HSL ~8°/34%/36%): lightness-lifted + desaturated off the danger hue so it
    # reads rosy, not the old muddy brown. Parchment text on it = 5.10:1; vs
    # canvas 2.45 (a clearer, softer band).
    danger_bg="#7b453d",
    canvas="#14130f",
)
"""Dark background: parchment text, dried-olive accents (wreath-leaf
khaki, not bright sage). Contrast vs near-black (measured): brand 6.66,
strong 9.00, tertiary_text 5.09, outline 3.38. Transcript pastels vs canvas:
accent_file 7.90, accent_exec 9.26, accent_search 7.88, accent_prose 8.42
(all ≥4.5). cursor_bg_gray is a neutral dark gray, kept distinct from the
olive `chosen_bg` (which doubles as the list cursor wash) and the `brand`
used for the nav-button hover."""


def blend_hex(fg: str, bg: str, alpha: float) -> str:
    """Mix ``fg`` over ``bg`` at ``alpha`` opacity — the terminal stand-in
    for translucency (e.g. mode-tinted row washes)."""
    f = [int(fg[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i : i + 2], 16) for i in (1, 3, 5)]
    mixed = (round(x * alpha + y * (1 - alpha)) for x, y in zip(f, b, strict=True))
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


@dataclass(frozen=True)
class Seeds:
    """A theme's seed colors. ``_build`` DERIVES a full ``Palette`` from these —
    every surface tone (code band, selection / cursor / diff washes, outline,
    hairline) is blended off the canvas / ink / brand so each scheme is internally
    coherent. The hand-tuned ``DARK`` / ``LIGHT`` are NOT built this way (they
    predate the seed system and carry per-role WCAG tuning); the seed library is
    how the ADDITIONAL dark themes (Slate / Sepia / Teal / Violet / Mono) are
    made."""

    name: str
    canvas: str
    ink: str
    brand: str
    brand_strong: str
    accent_file: str
    accent_exec: str
    accent_search: str
    accent_prose: str
    success: str
    warning: str
    danger: str
    info: str
    mode_read_only: str


_TERTIARY_BLEND = 0.60
"""How far ``tertiary_text`` blends toward the canvas in ``_build``. 0.60 (NOT
the 0.52 the visual mockup used) keeps every built theme's tertiary ≥4.5:1 on its
canvas — hints / row numbers are real content, so they clear the text gate
(matching ``DARK``'s hand-tuned 5.09)."""


def _build(s: Seeds) -> Palette:
    """Derive a full ``Palette`` from a theme's ``Seeds``. Surface tones blend off
    the canvas / ink / brand via ``blend_hex`` so each scheme stays coherent; the
    accents + semantics come straight from the seed."""
    c, ink = s.canvas, s.ink
    return Palette(
        primary_text=ink,
        secondary_text=blend_hex(ink, c, 0.70),
        tertiary_text=blend_hex(ink, c, _TERTIARY_BLEND),
        brand=s.brand,
        brand_strong=s.brand_strong,
        accent_file=s.accent_file,
        accent_exec=s.accent_exec,
        accent_search=s.accent_search,
        accent_prose=s.accent_prose,
        success=s.success,
        warning=s.warning,
        danger=s.danger,
        info=s.info,
        mode_read_only=s.mode_read_only,
        outline=blend_hex(ink, c, 0.42),
        hairline=blend_hex(ink, c, 0.22),
        code_bg=blend_hex(ink, c, 0.13),
        diff_add_bg=blend_hex(s.success, c, 0.30),
        diff_del_bg=blend_hex(s.danger, c, 0.30),
        cursor_bg_gray=blend_hex(ink, c, 0.13),
        chosen_bg=blend_hex(s.brand, c, 0.42),
        chosen_cursor_bg=blend_hex(s.brand, c, 0.50),
        danger_bg=blend_hex(s.danger, c, 0.42),
        canvas=c,
    )


_SEEDS: tuple[Seeds, ...] = (
    Seeds(
        "slate",
        "#13151b",
        "#d7d9e0",
        "#7f9ccb",
        "#9cb6e0",
        "#d59aa6",
        "#8fc6b4",
        "#9aa6e6",
        "#d8b07c",
        "#8fb892",
        "#cba85e",
        "#d2837a",
        "#74a6d8",
        "#b79ad6",
    ),
    Seeds(
        "sepia",
        "#171310",
        "#e7ddca",
        "#c79a5a",
        "#dcb878",
        "#d79aa0",
        "#b7c184",
        "#c2a6d8",
        "#d8a06a",
        "#a6bd7a",
        "#d2ab5e",
        "#cf8068",
        "#c99a6a",
        "#c79ad6",
    ),
    Seeds(
        "teal",
        "#101417",
        "#d6dad8",
        "#54b4a6",
        "#7accbf",
        "#d99aa0",
        "#86c8ad",
        "#8fb2e0",
        "#d6ab78",
        "#8fc6a0",
        "#c8b05e",
        "#d28878",
        "#5fa8c0",
        "#b0a0d6",
    ),
    Seeds(
        "violet",
        "#16131c",
        "#dbd6e0",
        "#a98fd6",
        "#c2a9e8",
        "#dc9ab0",
        "#9cc0a0",
        "#9aa6e6",
        "#dba878",
        "#9cba88",
        "#cba85e",
        "#d2839a",
        "#6f9cd8",
        "#c79adf",
    ),
    Seeds(
        "mono",
        "#141413",
        "#d9d6d0",
        "#b0aa9c",
        "#c8c3b6",
        "#c89aa0",
        "#9cba9c",
        "#9aa6c8",
        "#cbb090",
        "#9cba88",
        "#c8a85e",
        "#cf8068",
        "#7fa8c8",
        "#b8a8c8",
    ),
)

BUILT_THEMES: dict[str, Palette] = {s.name: _build(s) for s in _SEEDS}
"""The additional dark themes, derived from seeds, keyed by short name. Olive
(= ``DARK``) and Light (= ``LIGHT``) are hand-tuned and NOT in here."""


def palette_for(kind: BackgroundKind) -> Palette:
    return LIGHT if kind == "light" else DARK


def build_theme(kind: BackgroundKind) -> Theme:
    """A rich Theme of semantic ``alk.*`` style names. Renderers use
    these names exclusively — never literal colors — so retuning the
    palette is a one-file change."""
    p = palette_for(kind)
    return Theme(
        {
            "alk.primary_text": p.primary_text,
            "alk.secondary_text": p.secondary_text,
            "alk.tertiary_text": p.tertiary_text,
            "alk.brand": p.brand,
            "alk.brand_strong": f"bold {p.brand_strong}",
            "alk.success": p.success,
            "alk.warning": p.warning,
            "alk.danger": p.danger,
            "alk.outline": p.outline,
        }
    )


def _luminance(r: float, g: float, b: float) -> float:
    """BT.709 luma of gamma-encoded channels in [0, 1] — the standard
    terminal-background heuristic (light when > 0.5)."""
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def classify_reply(reply: bytes) -> BackgroundKind | None:
    """Map a raw OSC 11 reply (``…]11;rgb:RRRR/GGGG/BBBB…``) to a
    background kind. Channels are 1-4 hex digits per the X11 spec and
    scale by their own width. Returns None when unparseable."""
    match = _OSC11_REPLY.search(reply)
    if match is None:
        return None
    channels = [int(g, 16) / float(16 ** len(g) - 1) for g in match.groups()]
    light = _luminance(channels[0], channels[1], channels[2]) > 0.5
    return "light" if light else "dark"


def _read_osc11_reply(fd: int, timeout: float) -> bytes:
    """Accumulate tty bytes until an OSC terminator (ST or BEL) or the
    deadline passes. Bounded so a chatty tty can never wedge startup."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline and len(buf) < 256:
        remaining = max(deadline - time.monotonic(), 0)
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 64)
        if not chunk:
            break
        buf += chunk
        if b"\x07" in buf or b"\x1b\\" in buf:
            break
    return buf


def _probe_osc11(timeout: float = OSC11_TIMEOUT_S) -> BackgroundKind | None:
    """Ask the terminal for its background color via OSC 11.

    Returns None when stdio isn't a tty, the terminal never replies, or
    the reply is unparseable. cbreak mode (echo off, signals intact) for
    the read so the reply never leaks onto the screen; tty state is
    always restored before returning.
    """
    if not _HAS_TTY_PROBE:  # no termios/tty (Windows); fall back to COLORFGBG / dark
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return None
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b]11;?\x1b\\")
        sys.stdout.flush()
        reply = _read_osc11_reply(fd, timeout)
    except OSError:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return classify_reply(reply)


def _from_colorfgbg(value: str | None) -> BackgroundKind | None:
    """``COLORFGBG`` is ``<fg>;<bg>`` (some terminals ``<fg>;default;<bg>``).
    ANSI backgrounds 0-6 and 8 are dark; 7 and 9-15 light."""
    if not value:
        return None
    last = value.split(";")[-1].strip()
    if not last.isdigit():
        return None
    bg = int(last)
    if bg == 7 or 9 <= bg <= 15:
        return "light"
    if 0 <= bg <= 8:
        return "dark"
    return None


def detect_background() -> BackgroundKind:
    """Resolve the terminal background kind: env → OSC 11 → COLORFGBG → dark."""
    forced = os.environ.get("ALKERA_THEME", "").strip().lower()
    if forced == "light":
        return "light"
    if forced == "dark":
        return "dark"
    probed = _probe_osc11()
    if probed is not None:
        return probed
    fallback = _from_colorfgbg(os.environ.get("COLORFGBG"))
    if fallback is not None:
        return fallback
    return "dark"


console: Console = Console(theme=build_theme("dark"))
"""The one console every renderer writes to. Built with no ``file=`` so
``console.file`` resolves ``sys.stdout`` dynamically on each write — a
host that redirects stdout (a TUI driver, a captured test harness) is
honored instead of bypassed by a pinned handle (see module docstring)."""

_active: BackgroundKind = "dark"
_pushed = False


def activate() -> BackgroundKind:
    """Detect the terminal background once (chat entry) and retheme."""
    return apply_background(detect_background())


def apply_background(kind: BackgroundKind) -> BackgroundKind:
    """Switch the live palette (activation now; ``/theme`` later)."""
    global _active, _pushed
    if _pushed:
        console.pop_theme()
    console.push_theme(build_theme(kind))
    _active = kind
    _pushed = True
    return kind
