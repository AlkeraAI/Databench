// The two Looker calls answer about the same subject from two distances:
// looker.list_dashboards says what exists on an instance, looker.dashboard_tiles
// says what one dashboard actually reads. The tiles card leads with the
// deduplicated warehouse tables, because that union is the question the tool was
// built to answer; the tiles below it are the provenance for that answer.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { Text } from "../sharedUi";
import { IconLayoutDashboard } from "@tabler/icons-react";
import { count, readResult, records, str, strings } from "./alkeraPayload";
import { Band, LeafPath } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./looker.module.css";
interface Dashboard {
  id: string;
  title: string;
  urn: string;
}
interface Tile {
  title: string;
  model: string;
  explore: string;
  tables: string[];
}
interface LookerView {
  tiles: boolean;
  connection: string;
  dashboards: Dashboard[];
  dashboardId: string;
  title: string;
  urn: string;
  tileCards: Tile[];
  tables: string[];
}
function deriveLooker(part: ToolConversationPart): LookerView {
  const result = readResult(part.output);
  return {
    // A call can arrive under an MCP prefix, so the pair matches on the suffix.
    tiles: part.name.endsWith("dashboard_tiles"),
    connection: str(part.input?.connection),
    dashboards: records(result?.dashboards).map((dashboard) => ({
      id: str(dashboard.id),
      title: str(dashboard.title),
      urn: str(dashboard.urn),
    })),
    dashboardId: str(result?.dashboard_id) || str(part.input?.dashboard_id),
    title: str(result?.title),
    urn: str(result?.urn),
    tileCards: records(result?.tiles).map((tile) => ({
      title: str(tile.title),
      model: str(tile.model),
      explore: str(tile.explore),
      tables: strings(tile.tables),
    })),
    tables: strings(result?.tables),
  };
}
/** A warehouse table named the way the warehouse names it: the database and
 *  schema locate, the table itself reads. */
function Table({ table }: { table: string }): ReactElement {
  return (
    // LeafPath renders nodes, so the tip's reading is explicit.
    <Text className="chat-tool-chip chat-tool-clip chat-tool-mono" tooltip="truncate" tooltipLabel={table}>
      <LeafPath path={table} sep="." />
    </Text>
  );
}
/** The LookML the tile queries, in Looker's own model::explore form. */
function sourceOf(tile: Tile): string {
  if (tile.model && tile.explore) return `${tile.model}::${tile.explore}`;
  return tile.model || tile.explore;
}
function Dashboards({ view }: { view: LookerView }): ReactElement {
  return (
    <>
      <Band>
        <Text className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono" tooltip="truncate">
          {view.connection}
        </Text>
      </Band>
      <div className={`${s.list} chat-tool-ruled`} data-cap="300">
        {view.dashboards.map((dashboard) => (
          <div key={dashboard.id} className={`${s.dash} chat-tool-line`}>
            <span className="chat-tool-glyph" aria-hidden="true">
              <IconLayoutDashboard size={14} stroke={1.6} />
            </span>
            <Text className="chat-tool-clip" tooltip="truncate">
              {dashboard.title || dashboard.id}
            </Text>
            {dashboard.title ? <span className="chat-tool-mono chat-tool-end chat-tool-quiet">{dashboard.id}</span> : null}
          </div>
        ))}
      </div>
    </>
  );
}
function Tiles({ view }: { view: LookerView }): ReactElement {
  return (
    <>
      <Band>
        <Text className="chat-tool-band__text chat-tool-band__text--one" tooltip="truncate">
          {view.title || view.dashboardId}
        </Text>
        {view.connection ? <span className="chat-tool-band__param">{view.connection}</span> : null}
      </Band>
      {view.tables.length > 0 ? (
        <div className={s.reads}>
          <p className={s.readsHead}>Reads</p>
          <p className={s.tables}>
            {view.tables.map((table) => (
              <Table key={table} table={table} />
            ))}
          </p>
        </div>
      ) : null}
      <div className={`${s.tiles} chat-tool-ruled`} data-cap="300">
        {view.tileCards.map((tile, index) => (
          <div key={index} className={s.tile}>
            <Text as="p" className={s.tileTitle} tooltip="truncate">
              {tile.title || "Untitled tile"}
            </Text>
            <p className={s.tileMeta}>
              {sourceOf(tile) ? (
                <Text className="chat-tool-mono chat-tool-clip chat-tool-quiet" tooltip="truncate">
                  {sourceOf(tile)}
                </Text>
              ) : null}
              {tile.tables.length > 0 ? (
                tile.tables.map((table) => <Table key={table} table={table} />)
              ) : (
                <span className="chat-tool-quiet">No backing table</span>
              )}
            </p>
          </div>
        ))}
      </div>
    </>
  );
}
/** The well's interior: an instance's dashboards, or one dashboard's sources. It
 *  paints no ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const view = deriveLooker(part);
  return <div data-tool="looker">{view.tiles ? <Tiles view={view} /> : <Dashboards view={view} />}</div>;
}
/** The step one dashboard's sources contribute to the transcript's tool group. */
export const headTiles: CardHead = (part) => {
  const view = deriveLooker(part);
  return {
    object: view.title || view.dashboardId,
    data: {
      kind: "count",
      text: `${count(view.tileCards.length, "tile", "tiles")}, ${count(view.tables.length, "table", "tables")}`,
    },
    body: <Body part={part} />,
  };
};
/** The step an instance's dashboard roster contributes. */
export const headDashboards: CardHead = (part) => {
  const view = deriveLooker(part);
  return {
    object: view.connection,
    data: { kind: "count", text: count(view.dashboards.length, "dashboard", "dashboards") },
    body: <Body part={part} />,
  };
};
