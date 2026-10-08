// A skill is instructions the agent pulls into context on purpose, so the card
// renders them as the document they are. Two calls share the design: one with no
// name lists what exists, one with a name loads that skill's body.
//
// The alkera tool answers in JSON (`skills`, or `name` + `body`); the opencode
// tool answers with the body wrapped in a `<skill_content>` envelope that also
// carries a sampled list of the files the skill ships. Both are read here so the
// two render as one card.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { LanguageIcon, Text } from "../sharedUi";
import { Prose } from "../prose";
import { count, readResult, records, str } from "./alkeraPayload";
import { Band, LeafPath } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import "./skill.css";
interface SkillCard {
  name: string;
  description: string;
}
interface SkillView {
  name: string;
  body: string;
  files: string[];
  /** True when the file list came from the envelope, which samples it. */
  sampled: boolean;
  skills: SkillCard[];
}
/** The opencode envelope: the skill's own text sits between its heading and the
 *  base-directory note, and the bundled files follow in their own block. */
function parseEnvelope(text: string): SkillView | null {
  const opened = /<skill_content name="([^"]*)">/.exec(text);
  if (!opened) return null;
  const lines = text.split("\n");
  const from = lines.findIndex((line) => line.startsWith("# Skill:"));
  const to = lines.findIndex((line) => line.startsWith("Base directory for this skill:"));
  return {
    name: opened[1],
    body: lines
      .slice(from < 0 ? 0 : from + 1, to < 0 ? lines.length : to)
      .join("\n")
      .trim(),
    files: [...text.matchAll(/<file>([^<]*)<\/file>/g)].map((match) => match[1]),
    sampled: true,
    skills: [],
  };
}
function deriveSkill(part: ToolConversationPart): SkillView {
  const result = readResult(part.output);
  if (result) {
    return {
      name: str(result.name) || str(part.input?.name),
      body: str(result.body),
      files: [],
      sampled: false,
      skills: records(result.skills)
        .map((entry) => ({ name: str(entry.name), description: str(entry.description) }))
        .filter((skill) => skill.name.length > 0),
    };
  }
  const envelope = parseEnvelope(str(part.output) || str(part.content));
  return envelope ?? { name: str(part.input?.name), body: "", files: [], sampled: false, skills: [] };
}
function FileRow({ path, onOpen }: { path: string; onOpen?: () => void }): ReactElement {
  const inner = (
    <>
      <span className="chat-tool-glyph" aria-hidden="true">
        <LanguageIcon path={path} size={16} />
      </span>
      {/* LeafPath renders nodes, so the tip's reading has to be handed over. */}
      <Text className="chat-skill-file__path chat-tool-mono chat-tool-clip" tooltip="truncate" tooltipLabel={path}>
        <LeafPath path={path} />
      </Text>
    </>
  );
  if (!onOpen) {
    return <div className="chat-skill-file chat-tool-line">{inner}</div>;
  }
  return (
    <button type="button" className="chat-skill-file chat-tool-line" aria-label={`Open ${path}`} onClick={onOpen}>
      {inner}
    </button>
  );
}
/** The well's interior: the skill's instructions and what it ships with, or the
 *  roster of what can be loaded. It paints no ground, edge, radius, or outer pad;
 *  the group's well owns those. */
function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const view = deriveSkill(part);
  if (view.body.length === 0 && view.skills.length > 0) {
    return (
      <div data-tool="skill">
        <div className="chat-skill-roster chat-tool-ruled" data-cap="300">
          {view.skills.map((skill) => (
            <div key={skill.name} className="chat-skill-skill">
              <p className="chat-skill-skill__name chat-tool-mono">{skill.name}</p>
              {skill.description ? <p className="chat-skill-skill__desc">{skill.description}</p> : null}
            </div>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div data-tool="skill">
      <Band>
        <Text className="chat-tool-band__text chat-tool-band__text--one" tooltip="truncate">
          {view.name}
        </Text>
      </Band>
      <div className="chat-tool-doc" data-cap="340">
        <Prose content={view.body} />
      </div>
      {view.files.length > 0 ? (
        <div className="chat-skill-files">
          <p className="chat-skill-files__head">Ships with</p>
          {view.files.map((path, index) => (
            <FileRow
              key={`${path}-${index}`}
              path={path}
              onOpen={env?.onOpenPath ? () => env.onOpenPath?.(path) : undefined}
            />
          ))}
          {view.sampled ? <p className="chat-tool-note">The file list is sampled.</p> : null}
        </div>
      ) : null}
    </div>
  );
}
/** The step this tool contributes to the transcript's tool group. A call with no
 *  name lists what exists, so the voice is the payload's to decide. */
export const head: CardHead = (part, env) => {
  const view = deriveSkill(part);
  const listing = view.body.length === 0 && view.skills.length > 0;
  const lines = view.body.length === 0 ? 0 : view.body.split("\n").length;
  return {
    verb: listing ? "Listed" : undefined,
    object: listing ? "skills" : view.name,
    data: listing
      ? { kind: "count", text: count(view.skills.length, "skill", "skills") }
      : lines > 0
        ? { kind: "count", text: count(lines, "line", "lines") }
        : undefined,
    // A roster IS the answer; a loaded skill is a long instruction document and
    // waits behind its word.
    expanded: listing,
    disclosure: listing ? "skills" : undefined,
    loneSummary: listing ? "Listed the available skills" : undefined,
    body: <Body part={part} env={env} />,
  };
};
