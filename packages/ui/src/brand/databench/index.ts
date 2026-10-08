// The Databench brand: what the open build shows a person when no product brand is installed.

import type { WebBrand } from "../brand";
import { DatabenchLogo } from "./DatabenchLogo";
import { DATABENCH_IDENTITY } from "./identity";

export const DATABENCH_BRAND: WebBrand = { ...DATABENCH_IDENTITY, Logo: DatabenchLogo };
