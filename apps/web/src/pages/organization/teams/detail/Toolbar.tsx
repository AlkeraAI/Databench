import { SegmentedControl, TextInput, Toolbar as ToolbarRow } from "@alkera/ui";

type Scope = "direct" | "descent";

/** The roster toolbar, on the shared Toolbar row. When admins reach the team by descent, the scope
 *  switch leads (Direct members vs By descent, each carrying its count as a tinted chip — the two
 *  listings can overlap, so the counts are per listing, not a partition) and the search rides the
 *  end cluster. When nothing reaches the team from above, the search takes the leading cluster
 *  instead so it never floats alone against empty space. */
export function Toolbar({
  hasScope,
  scope,
  onScope,
  directCount,
  descentCount,
  query,
  onQuery,
  searchLabel,
}: {
  hasScope: boolean;
  scope: Scope;
  onScope: (s: Scope) => void;
  directCount: number;
  descentCount: number;
  query: string;
  onQuery: (q: string) => void;
  searchLabel: string;
}) {
  const scopeOptions = [
    { key: "direct", label: "Direct members", count: directCount },
    { key: "descent", label: "By descent", count: descentCount },
  ];
  const search = (
    <TextInput type="search" value={query} onChange={(e) => onQuery(e.target.value)} placeholder="Find a member" aria-label={searchLabel} />
  );

  return (
    <ToolbarRow style={{ padding: "0 var(--alkSpace5) 0 var(--alkSpace7)" }} end={hasScope ? search : undefined}>
      {hasScope ? (
        <SegmentedControl options={scopeOptions} value={scope} onChange={(k) => onScope(k as Scope)} label="Roster scope" />
      ) : (
        search
      )}
    </ToolbarRow>
  );
}
