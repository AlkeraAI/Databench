// A SQL cell's connection, in its header: DuckDB inside the notebook, or one
// of the workspace's connections. The server says which connections exist and
// which this reader can use (`GET .../connections`); a choice is written to
// the cell through the live notebook document, so collaborators and the agent
// see it at once.

import type { SqlConnectionSlot } from "@alkera/notebook-ui";
import { Select } from "@alkera/ui";

import type { NotebookConnection } from "@/api/notebooks";

/** The option that stands for no connection. A connection's name never holds
 *  a colon, so this cannot collide with one. */
export const DUCKDB_VALUE = ":duckdb";
export const DUCKDB_LABEL = "DuckDB";
export const MISSING_STATE = "Not in this workspace. Running this cell fails.";

export interface SqlConnectionPickerProps {
  slot: SqlConnectionSlot;
  /** The workspace's connections; `undefined` until they are known. */
  connections: readonly NotebookConnection[] | undefined;
}

/** Whose credentials a statement on the connection runs on, as a tooltip.
 *  The server names the person; a connection it names nobody for says
 *  nothing. */
export function runsOn(connection: NotebookConnection | undefined): string | undefined {
  return connection?.credential_owner ? `Runs on ${connection.credential_owner}'s credentials` : undefined;
}

function optionLabel(connection: NotebookConnection): string {
  const parts = [connection.name, connection.engine_title];
  if (connection.credential_owner) parts.push(connection.credential_owner);
  if (!connection.can_use) parts.push(connection.reason);
  return parts.join(" · ");
}

/** One entry per name: the cell stores a name, and the server marks every
 *  connection that shares one as unusable. */
function byName(connections: readonly NotebookConnection[]): NotebookConnection[] {
  const seen = new Set<string>();
  return connections.filter((c) => (seen.has(c.name) ? false : (seen.add(c.name), true)));
}

export function SqlConnectionPicker({ slot, connections }: SqlConnectionPickerProps) {
  const current = slot.connection;
  const options = byName(connections ?? []);
  const chosen = current === null ? undefined : options.find((c) => c.name === current);
  if (!slot.canEdit || connections === undefined) {
    return (
      <span className="nb-cell__connection" title={runsOn(chosen) ?? "Connection"}>
        {current ?? DUCKDB_LABEL}
      </span>
    );
  }
  const state = current === null ? null : chosen === undefined ? MISSING_STATE : chosen.can_use ? null : chosen.reason;
  return (
    <span className="nb-sql-connection" title={runsOn(chosen)}>
      <Select
        size="sm"
        aria-label="Connection"
        className="nb-sql-connection__select"
        value={current ?? DUCKDB_VALUE}
        // Closed, the control names the connection alone; the open list adds
        // its engine, whose credentials it runs on, and why it cannot run.
        triggerLabels={Object.fromEntries(options.map((c) => [c.name, c.name]))}
        onChange={(event) => {
          const next = event.currentTarget.value;
          slot.onChange(next === DUCKDB_VALUE ? null : next);
        }}
      >
        <option value={DUCKDB_VALUE}>{DUCKDB_LABEL}</option>
        {current !== null && chosen === undefined ? (
          <option value={current} disabled>
            {`${current} · Missing`}
          </option>
        ) : null}
        {options.map((connection) => (
          <option key={connection.id} value={connection.name} disabled={!connection.can_use}>
            {optionLabel(connection)}
          </option>
        ))}
      </Select>
      {state ? (
        <span className="nb-sql-connection__state" role="status">
          {state}
        </span>
      ) : null}
    </span>
  );
}
