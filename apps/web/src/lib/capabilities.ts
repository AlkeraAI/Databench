/**
 * Whether the server granted a capability.
 *
 * Fail closed: only an explicit `true` is a yes. A `can_*` field that is missing
 * (an older server, a trimmed projection, a row the client built itself) is not
 * a grant, because the alternative is offering a control the server then
 * refuses. Every `can_*` read in the portal goes through here, and
 * `apps/web/src/tests/architecture/failOpenCapabilities.test.ts` flags one that reads a
 * missing answer as a yes.
 */
export function granted(flag: boolean | null | undefined): boolean {
  return flag === true;
}
