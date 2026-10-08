// Todowrite: opencode's own list lands as a flat array of
// `{content, status, priority}` with no ids and no dependencies, so the roster
// keeps the order the agent wrote and nothing is regrouped. Priority is the one
// axis the head cannot carry, so the band reports its spread and the roster
// marks only the top rank. The result restates the call's input, so a run still
// in flight reads off the input and shows the same list.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";

import { count, records, str } from "./alkeraPayload";
import { Band, StatusGlyph } from "./shared";
import type { CardHead, StepFigure } from "./step";
import "./shared.css";
import s from "./todowrite.module.css";

type TodoStatus = "pending" | "in_progress" | "completed" | "cancelled";
type TodoPriority = "high" | "medium" | "low";

const STATUSES = new Set<string>(["pending", "in_progress", "completed", "cancelled"]);
const PRIORITIES = new Set<string>(["high", "medium", "low"]);

/** Highest first, so the spread reads as a ranking. */
const RANKS: TodoPriority[] = ["high", "medium", "low"];

interface Todo {
  content: string;
  status: TodoStatus;
  priority: TodoPriority;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** The array the call wrote, whichever side of the call carries it. */
function rows(part: ToolConversationPart): Record<string, unknown>[] {
  const raw = part.output ?? part.content;
  const written = records(typeof raw === "string" ? parseJson(raw) : raw);
  return written.length > 0 ? written : records(part.input?.todos);
}

function readTodos(part: ToolConversationPart): Todo[] {
  return rows(part).map((row) => {
    const status = str(row.status);
    const priority = str(row.priority);
    return {
      content: str(row.content),
      status: STATUSES.has(status) ? (status as TodoStatus) : "pending",
      priority: PRIORITIES.has(priority) ? (priority as TodoPriority) : "medium",
    };
  });
}

/** How the list ranks itself, in the tool's own words. */
function spread(todos: Todo[]): { rank: TodoPriority; n: number }[] {
  return RANKS.map((rank) => ({ rank, n: todos.filter((todo) => todo.priority === rank).length })).filter(
    (share) => share.n > 0,
  );
}

/** The well's interior: the priority spread, then the list as written. It paints
 *  no ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const todos = readTodos(part);
  const shares = spread(todos);
  return (
    <div data-tool="todowrite">
      <Band>
        {shares.length > 0 ? (
          shares.map((share) => (
            <span key={share.rank} className={s.todoShare} data-rank={share.rank}>
              {share.n} {share.rank}
            </span>
          ))
        ) : (
          <span className="chat-tool-band__text">The call carried no todos.</span>
        )}
      </Band>
      <ul className={s.todoList} data-cap="280">
        {todos.map((todo, index) => (
          <li key={index} className={s.todoItem} data-status={todo.status} data-rank={todo.priority}>
            <span className={s.todoItemMark} aria-hidden="true">
              <StatusGlyph status={todo.status} />
            </span>
            <span className={s.todoItemText}>{todo.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Progress, or nothing when the call wrote no list to measure. */
function progress(todos: Todo[]): StepFigure | undefined {
  if (todos.length === 0) return undefined;
  const done = todos.filter((todo) => todo.status === "completed").length;
  return { kind: "count", text: `${done} of ${todos.length} done`, tone: done === todos.length ? "pass" : undefined };
}

/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part) => {
  const todos = readTodos(part);
  return {
    object: count(todos.length, "todo", "todos"),
    data: progress(todos),
    body: <Body part={part} />,
  };
};
