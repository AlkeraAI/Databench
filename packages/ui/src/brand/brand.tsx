// The brand the app shows a person: product name, logo and lockup.
//
// One seam. The open build shows Databench; a product registers its own brand into
// BRAND during composition (`installExtensions`, before the first render) and every
// surface follows. Components render <BrandLogo> or read `currentBrand()`; none
// imports a brand's assets or compares an edition string.

import type { ComponentType, CSSProperties } from "react";

import { ExtensionError, ExtensionPoint } from "../extensions";
import { DATABENCH_BRAND } from "./databench";

export interface BrandLogoProps {
  /** Rendered height in px (the mark and the lockup share it). Default 64. */
  size?: number;
  /** Render the mark with the product name beside it instead of the mark alone. */
  wordmark?: boolean;
  /** Accessible name. Omit for the product name; "" renders decoratively (aria-hidden). */
  title?: string;
  className?: string;
  style?: CSSProperties;
}

export interface WebBrand {
  /** Unique within the point. */
  readonly key: string;
  /** The product name a person reads: titles, captions, accessible names. */
  readonly productName: string;
  /** The credit line where one reads naturally (an about panel, a footer). */
  readonly attribution: string;
  readonly Logo: ComponentType<BrandLogoProps>;
}

/** The product brand. At most one is registered; with none the open brand is shown. */
export const BRAND = new ExtensionPoint<WebBrand>("brand");

/** The brand to show: the registered product brand, else Databench. */
export function currentBrand(): WebBrand {
  const registered = BRAND.items();
  if (registered.length > 1) {
    throw new ExtensionError(`only one brand may be registered, found ${registered.map((b) => b.key).join(", ")}`);
  }
  return registered[0] ?? DATABENCH_BRAND;
}

/** The current brand's logo: the mark, or the lockup with `wordmark`. */
export function BrandLogo({ title, ...props }: BrandLogoProps) {
  const brand = currentBrand();
  return <brand.Logo title={title ?? brand.productName} {...props} />;
}
