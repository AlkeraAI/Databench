import { useLayoutEffect, useRef } from "react";

import "./vine-background.css";

// The auth background: a few independent vine systems that draw themselves in, with sparse
// leaves sprouting along each as the draw passes them. The vines wander freely with hairpin
// loops and each splits its own branch — abstract and non-grid, some passing behind the
// opaque card rather than framing its edges. Path lengths and tangents are read from the
// live DOM (getTotalLength / getPointAtLength), so leaf placement and sprout timing are
// computed in a layout effect and applied imperatively. Subtle and ambient by design —
// faint vine-ink, thin engraved lines, bone leaves. Theme-aware via the --pa-auth-vine
// token; prefers-reduced-motion renders the whole plate static. Classes use the portal-local
// pa-auth-vine* prefix so no global CSS can bleed in.

const LEAF_D = "M0 0 Q6 -5 10 0 Q6 5 0 0Z";
const DRAW_EASE = "cubic-bezier(0.4, 0, 0.2, 1)";
const SPROUT_EASE = "cubic-bezier(0.22, 1, 0.36, 1)";
const SPROUT_MS = 620;
const LEAF_SPREAD = 42; // degrees a leaf splays off the vine's tangent

interface Leaf {
  /** Fractional position along the vine, [0,1]. */
  t: number;
  /** Which way the leaf splays off the tangent. */
  side: 1 | -1;
  /** Scale multiplier on the ~10px base leaf. */
  size?: number;
}

interface Vine {
  d: string;
  /** ms before the draw starts (the stagger). */
  delay: number;
  /** ms for the vine to fully draw. */
  duration: number;
  /** Thinner stroke for a branching offshoot. */
  tendril?: boolean;
  leaves: Leaf[];
}

// Three independent vine systems — they do NOT share a graph; each wanders on its own and
// splits its own branch. Free hairpin meanders, not an edge frame; the hero stroke's middle
// passes behind the opaque card (hidden), so it reads as a fishhook on the left and a
// separate sweep on the right. (Card bounds ≈ x 520–918, y 160–734 at 1440×900, so leaves
// are kept off the hidden middle.)
const VINES: Vine[] = [
  // Hero meander: drops from the top, hairpins at the lower-left, then sweeps right across
  // the upper field — behind the card — and out the upper-right.
  {
    d: "M 300 -40 C 280 120 180 150 180 280 C 180 400 320 430 410 380 C 520 322 560 250 720 240 C 900 228 1000 300 1180 250 C 1320 212 1380 120 1520 150",
    delay: 0,
    duration: 3200,
    leaves: [
      { t: 0.12, side: -1, size: 1.9 },
      { t: 0.27, side: 1, size: 2.1 },
      { t: 0.82, side: -1, size: 1.8 },
      { t: 0.94, side: 1, size: 1.6 },
    ],
  },
  // Hero's branch: an offshoot off the hairpin that curls down-left on its own.
  {
    d: "M 180 280 C 110 300 80 400 140 470",
    delay: 1400,
    duration: 1100,
    tendril: true,
    leaves: [{ t: 0.72, side: 1, size: 1.4 }],
  },
  // Lower-right wave: a separate system — a free wave rising from the foot to the right edge.
  {
    d: "M 360 950 C 520 870 620 960 780 910 C 940 862 980 720 1130 745 C 1290 768 1430 820 1520 900",
    delay: 500,
    duration: 3000,
    leaves: [
      { t: 0.2, side: -1, size: 1.9 },
      { t: 0.55, side: 1, size: 2.1 },
      { t: 0.86, side: -1, size: 1.7 },
    ],
  },
  // Wave's branch: an offshoot off the wave's crest, rising up-right independently.
  {
    d: "M 1130 745 C 1180 660 1150 560 1240 520",
    delay: 1900,
    duration: 1000,
    tendril: true,
    leaves: [{ t: 0.7, side: 1, size: 1.4 }],
  },
  // Lower-left fishhook: a third independent system — a small hook in the lower-left corner.
  {
    d: "M -40 600 C 140 580 230 680 200 790 C 180 860 100 870 70 810",
    delay: 900,
    duration: 2400,
    leaves: [
      { t: 0.35, side: -1, size: 1.9 },
      { t: 0.68, side: 1, size: 1.7 },
    ],
  },
];

// The SVG transform that seats a leaf on its vine: translate to the point at fraction t,
// rotate to the tangent there (splayed to the chosen side), and scale to the leaf's size.
function leafTransform(stem: SVGPathElement, len: number, leaf: Leaf): string {
  const at = len * leaf.t;
  const eps = Math.min(2, len * 0.01);
  const p = stem.getPointAtLength(at);
  const a = stem.getPointAtLength(Math.max(0, at - eps));
  const b = stem.getPointAtLength(Math.min(len, at + eps));
  const tangent = (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI;
  const angle = tangent + leaf.side * LEAF_SPREAD;
  return `translate(${p.x} ${p.y}) rotate(${angle}) scale(${leaf.size ?? 1})`;
}

export function VineBackground() {
  const ref = useRef<SVGSVGElement>(null);

  useLayoutEffect(() => {
    const svg = ref.current;
    if (!svg) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const animations: Animation[] = [];

    svg.querySelectorAll<SVGPathElement>(".pa-auth-vine__stem").forEach((stem) => {
      const vi = Number(stem.dataset.vine);
      const vine = VINES[vi];
      if (!vine) return;
      const len = stem.getTotalLength();

      if (!reduce) {
        stem.style.strokeDasharray = `${len}`;
        stem.style.strokeDashoffset = `${len}`;
        animations.push(
          stem.animate([{ strokeDashoffset: len }, { strokeDashoffset: 0 }], {
            duration: vine.duration,
            delay: vine.delay,
            easing: DRAW_EASE,
            fill: "forwards",
          }),
        );
      }

      vine.leaves.forEach((leaf, li) => {
        const group = svg.querySelector<SVGGElement>(
          `.pa-auth-vine__leaf[data-vine="${vi}"][data-leaf="${li}"]`,
        );
        if (!group) return;
        group.setAttribute("transform", leafTransform(stem, len, leaf));

        const shape = group.querySelector<SVGPathElement>(".pa-auth-vine__leaf-shape");
        if (!shape) return;
        if (reduce) {
          shape.style.opacity = "1";
          return;
        }

        // Sprout in the draw's wake: the delay scales with the leaf's position along the
        // vine, so each leaf opens just after the (eased) line has swept past it.
        shape.style.opacity = "0";
        animations.push(
          shape.animate(
            [
              { opacity: 0, transform: "scale(0)" },
              { opacity: 1, transform: "scale(1)" },
            ],
            {
              duration: SPROUT_MS,
              delay: vine.delay + leaf.t * vine.duration,
              easing: SPROUT_EASE,
              fill: "forwards",
            },
          ),
        );
      });
    });

    return () => animations.forEach((animation) => animation.cancel());
  }, []);

  return (
    <svg
      ref={ref}
      className="pa-auth-vine"
      viewBox="0 0 1440 900"
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
    >
      {VINES.map((vine, vi) => (
        <path
          key={`stem-${vi}`}
          className="pa-auth-vine__stem"
          style={vine.tendril ? { strokeWidth: 1.5 } : undefined}
          data-vine={vi}
          d={vine.d}
        />
      ))}
      {VINES.flatMap((vine, vi) =>
        vine.leaves.map((_leaf, li) => (
          <g key={`leaf-${vi}-${li}`} className="pa-auth-vine__leaf" data-vine={vi} data-leaf={li}>
            <path className="pa-auth-vine__leaf-shape" d={LEAF_D} />
          </g>
        )),
      )}
    </svg>
  );
}
