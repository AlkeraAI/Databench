import type { Linter, Rule } from "eslint";

export interface ManifestGroup {
  name: string;
  side: string | null;
  paths: string[];
  regexps: RegExp[];
}

export interface ResolveContext {
  root: string;
  aliases: Record<string, string>;
  packages: Map<string, { dir: string; exports: Record<string, string> | string | null; main: string | null }>;
}

export function globToRegExp(pattern: string): RegExp;
export function parseManifest(text: string): ManifestGroup[];
export function manifestSetting(settings: unknown): string | null;
export function privateGroupOf(groups: ManifestGroup[], path: string): string | null;
export function tsconfigAliases(packageDir: string, root: string): Record<string, string>;
export function resolveSpecifier(specifier: string, fromFile: string, context: ResolveContext): string | null;
export const openBoundaryRule: Rule.RuleModule;
export const openBoundaryPlugin: { rules: Record<string, Rule.RuleModule> };
export const openBoundaryConfig: Linter.Config;
