"""Brand constants for HTML email, flattened to literal values email clients accept.

The product UI's brand lives in ``packages/ui/src/theme/tokens.css`` (the ``--alk*``
custom properties, both schemes) and the ui font layer (``@alkera/ui/fonts``).
None of that survives an email client — no CSS custom properties, no
``oklch()``/``color-mix()``, no scheme switching — so email pulls the *resolved literal*
values instead.

This is the LIGHT / parchment scheme (the more universal choice for email; it also
survives a client's forced dark-mode inversion better than the app's default dark
scheme). When the brand changes, update these to match the upstream source above — for
~a dozen near-immutable values a hand-maintained module is the proportionate choice over
a codegen step.
"""

from __future__ import annotations

# Surfaces — warm parchment, not stark white.
PAGE_BG = "#eae6dd"  # --alkera-bg (light): the email body / canvas
CARD_BG = "#f5f1ea"  # --alkera-panel (light) / theme.white: the content card
TEXT = "#28251f"  # --alkera-text (light) / theme.black: warm near-black ink
MUTED = "#686459"  # --alkera-muted (light): secondary / footer text
HAIRLINE = "#d9d1c3"  # flat approximation of --alkera-border (light); 1px dividers

# Brand olive — the light-scheme button fill (--alk-brand). White-on-#51683f clears
# WCAG AA (~5.6:1). NOT the Mantine `brand` tuple #76915c, which drives badges.
CTA_BG = "#51683f"
CTA_TEXT = "#ffffff"
DANGER = "#9a2a1e"  # --alkera-danger (light): deep brick red

RADIUS = "6px"  # --alk-radius-md; buttons + the content card

# Font stacks copied verbatim from typography.ts `fontFamilies`. The brand faces are
# self-hosted (@fontsource) with no CDN URL, and most clients (Gmail strips <head>,
# Outlook strips web fonts) will render the fallback — so the fallback IS the design.
FONT_DISPLAY = "'Newsreader', Georgia, 'Times New Roman', serif"
FONT_UI = "'IBM Plex Sans', Arial, Helvetica, sans-serif"
