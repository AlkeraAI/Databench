// Which data systems this project is actually wired to. The answer splits in
// two: a plugin whose signal is present in the workspace, and one Alkera supports
// that nobody has configured. That split is the card.
//
// Under each plugin, a connection is either live or merely detected, and the
// difference is what the reader acts on, so the two are separate labeled runs
// rather than one row of chips a reader has to decode. The payload's `surfaces`
// stays off the card: it names internal extension points a transcript reader
// cannot act on.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { ConnectorMark, Text } from "../sharedUi";
import { readResult, records, str } from "./alkeraPayload";
import { EmptyLine } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./list_plugins.module.css";
interface Connection {
  handle: string;
  environment: string;
}
interface PluginView {
  name: string;
  description: string;
  active: boolean;
  enabled: boolean;
  urnFormat: string;
  connected: Connection[];
  detected: Connection[];
}
function connectionsOf(value: unknown, live: boolean): Connection[] {
  return records(value)
    .filter((entry) => (entry.added === true) === live)
    .map((entry) => ({ handle: str(entry.handle), environment: str(entry.environment) }));
}
function derivePlugins(part: ToolConversationPart): PluginView[] {
  return records(readResult(part.output)?.plugins).map((entry) => ({
    name: str(entry.name),
    description: str(entry.description),
    active: entry.active === true,
    // The field defaults to true on the wire, so only an explicit false is off.
    enabled: entry.enabled !== false,
    urnFormat: str(entry.urn_format),
    connected: connectionsOf(entry.connections, true),
    detected: connectionsOf(entry.connections, false),
  }));
}
function Run({ label, items, live }: { label: string; items: Connection[]; live?: boolean }): ReactElement {
  return (
    <p className={s.pluginsRun}>
      <span className={s.pluginsRunLabel}>{label}</span>
      {items.map((connection, index) => (
        <span key={`${connection.handle}-${index}`} className={`${s.pluginsConn} chat-tool-chip`} data-live={live ? "" : undefined}>
          {connection.handle}
          {connection.environment ? <span className={s.pluginsConnEnv}>{connection.environment}</span> : null}
        </span>
      ))}
    </p>
  );
}
function Plugin({ plugin }: { plugin: PluginView }): ReactElement {
  return (
    <div className={s.pluginsPlugin}>
      <p className={`${s.pluginsPluginHead} chat-tool-line chat-tool-line--base`}>
        {/* A plugin's `name` IS the connector registry's id, so the vendor's own
            mark is one lookup with no mapping. An id the registry doesn't carry
            (generic_sql) takes the component's plug. */}
        <ConnectorMark id={plugin.name} size={14} className={s.pluginsPluginMark} />
        <Text className={s.pluginsPluginName} tooltip="truncate">
          {plugin.name}
        </Text>
        {plugin.enabled ? null : <span className={s.pluginsPluginOff}>turned off</span>}
      </p>
      {plugin.description ? (
        <Text as="p" className={s.pluginsPluginDesc} tooltip="truncate">
          {plugin.description}
        </Text>
      ) : null}
      {plugin.connected.length > 0 ? <Run label="Connected" items={plugin.connected} live /> : null}
      {plugin.detected.length > 0 ? <Run label="Detected" items={plugin.detected} /> : null}
      {plugin.urnFormat ? (
        <Text as="p" className={`${s.pluginsPluginUrn} chat-tool-mono`} tooltip="truncate">
          {plugin.urnFormat}
        </Text>
      ) : null}
    </div>
  );
}
function Lane({ label, plugins }: { label: string; plugins: PluginView[] }): ReactElement | null {
  if (plugins.length === 0) return null;
  return (
    <section className={s.pluginsLane}>
      <p className={`${s.pluginsLaneHead} chat-tool-lane-head`}>
        {label}
        <span className="chat-tool-num chat-tool-quiet">{plugins.length}</span>
      </p>
      {plugins.map((plugin) => (
        <Plugin key={plugin.name} plugin={plugin} />
      ))}
    </section>
  );
}
/** The well's interior: what is wired up, then what could be. It paints no
 *  ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const plugins = derivePlugins(part);
  return (
    <div data-tool="list_plugins">
      {plugins.length === 0 ? (
        <EmptyLine>No plugins are available.</EmptyLine>
      ) : (
        <div  data-cap="360">
          <Lane label="Active" plugins={plugins.filter((plugin) => plugin.active)} />
          <Lane label="Available" plugins={plugins.filter((plugin) => !plugin.active)} />
        </div>
      )}
    </div>
  );
}
/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part) => {
  const plugins = derivePlugins(part);
  const active = plugins.filter((plugin) => plugin.active).length;
  return {
    object: "plugins",
    data: { kind: "count", text: `${active} of ${plugins.length} active` },
    body: <Body part={part} />,
  };
};
