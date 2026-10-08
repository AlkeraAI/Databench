// The statuses a read carries, as the server writes them. Read from the file
// the server's own test holds to its vocabulary
// (`packages/api-core/tests/status/test_status_fixture.py`), so a test here
// never renders words no server sends.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { StatusFact } from "@/api/status";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = resolve(HERE, "../../../../../packages/api-core/tests/fixtures/status/facts.json");
const CASES = JSON.parse(readFileSync(FIXTURE, "utf8")) as Record<
  string,
  Record<string, { fact: StatusFact }>
>;

function factOf(subject: string, name: string): StatusFact {
  const found = CASES[subject]?.[name];
  if (!found) throw new Error(`no ${subject} status fixture named ${name}`);
  return found.fact;
}

/** A chat's status, by the fixture's name for it. */
export const chatFact = (name: string): StatusFact => factOf("chat", name);
/** A workspace's status, by the fixture's name for it. */
export const workspaceFact = (name: string): StatusFact => factOf("workspace", name);

/** The status the server writes on a chat read for a chat with nothing owed,
 *  by its machine's word: a test that scripts a machine state stands in for
 *  the server's builder with this. */
export function chatFactForMachine(machineStatus: string): StatusFact | null {
  const name: Record<string, string> = {
    ready: "awake",
    asleep: "asleep",
    starting: "starting",
    draining: "draining",
    restarting: "restarting",
    unreachable: "unreachable",
    none: "no_machine",
    stranded: "released",
    refused: "refused",
  };
  const found = name[machineStatus];
  return found ? chatFact(found) : null;
}

const GRIDS = CASES as unknown as Record<string, Record<string, StatusFact>>;

/** The platform console's status for a fleet row, as the server writes it for
 *  the row's allocation state, liveness and pending wake. */
export function fleetFact(row: { state: string; liveness?: string; wake_requested_at?: string | null }): StatusFact {
  const key = `${row.state}|${row.liveness ?? "none"}|${row.wake_requested_at ? "wake" : ""}`;
  const found = GRIDS.fleet?.[key];
  if (!found) throw new Error(`no fleet status fixture for ${key}`);
  return found;
}

/** An org machine card's status, as the server writes it for a card state and
 *  stop reason (the machine is named lab-b). */
export function machineFact(state: string, stopReason = ""): StatusFact {
  const found = GRIDS.machine?.[`${state}|${stopReason}`];
  if (!found) throw new Error(`no machine status fixture for ${state}|${stopReason}`);
  return found;
}

/** The fleet row of a credential revoked with no machine behind it. */
export const revokedFleetFact = (): StatusFact => {
  const found = GRIDS.fleet?.revoked;
  if (!found) throw new Error("no revoked fleet status fixture");
  return found;
};

/** A leased folder's status, by the fixture's name for it: `live`,
 *  `live_landing`, `paused_behind`, `paused_silent`, `paused_unreachable`,
 *  `saved_copy`. */
export const filesLiveFact = (name: string): StatusFact => factOf("files_live", name);
