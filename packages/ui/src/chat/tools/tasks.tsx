// The task list as a plan in motion. The band answers "how far along" with a
// meter built from the summary counts; the roster answers "what holds the rest
// up": a task whose predecessor sits directly above it is joined to it by a
// rail, and a blocked task names what it waits on. The band carries the STATE
// rather than the call's input, because manage_tasks returns the same roster it
// was sent. Status is the only thing in the card that carries ink.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { Text } from "../sharedUi";
import { IconHourglassLow } from "@tabler/icons-react";

import { count, isRecord, num, readResult, records, str, strings } from "./alkeraPayload";
import type { CardHead } from "./step";
import { Band, StatusGlyph } from "./shared";
import "./shared.css";
import s from "./tasks.module.css";

type TaskStatus = "pending" | "in_progress" | "completed" | "cancelled";

const STATUSES = new Set<string>(["pending", "in_progress", "completed", "cancelled"]);

interface Task {
  id: string;
  title: string;
  status: TaskStatus;
  blocked: boolean;
  blockedBy: string[];
  dependsOn: string[];
}

function readTasks(result: Record<string, unknown> | null): Task[] {
  return records(result?.tasks).map((task, index) => {
    const status = str(task.status);
    return {
      id: str(task.id) || `task-${index}`,
      title: str(task.title) || str(task.id),
      status: STATUSES.has(status) ? (status as TaskStatus) : "pending",
      blocked: task.blocked === true,
      blockedBy: strings(task.blocked_by),
      dependsOn: strings(task.depends_on),
    };
  });
}

/** The meter's segments, in the order work moves through them. A status with no
 *  tasks in it contributes no segment, and counts that report no work at all
 *  draw no track — an empty rail measures nothing and reads as a stalled one. */
const METER_ORDER: TaskStatus[] = ["completed", "in_progress", "pending", "cancelled"];

function Meter({ counts }: { counts: Record<string, number> }): ReactElement {
  return (
    <span className={s.meter} aria-hidden="true">
      {METER_ORDER.filter((status) => (counts[status] ?? 0) > 0).map((status) => (
        <span
          key={status}
          className={s.meterSeg}
          data-seg={status}
          style={{ flexGrow: counts[status] ?? 0, flexBasis: 0 }}
        />
      ))}
    </span>
  );
}

interface PlanView {
  tasks: Task[];
  counts: Record<string, number>;
  blocked: number;
  /** How much work the summary accounts for. Zero means there is no progress to
   *  meter: a call still in flight, an empty plan, or a payload that carried a
   *  roster without counting it. */
  total: number;
}

function derivePlan(part: ToolConversationPart): PlanView {
  const result = readResult(part.output);
  const tasks = readTasks(result);
  const summary = result?.summary;
  const rawCounts = isRecord(summary) ? summary.counts : null;
  const counts: Record<string, number> = {};
  for (const status of METER_ORDER) {
    const declared = isRecord(rawCounts) ? num(rawCounts[status]) : null;
    counts[status] = declared ?? tasks.filter((task) => task.status === status).length;
  }
  return {
    tasks,
    counts,
    blocked: tasks.filter((task) => task.blocked).length,
    total: METER_ORDER.reduce((sum, status) => sum + counts[status], 0),
  };
}

/** The well's interior: the plan's state, then the plan. It paints no ground,
 *  edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const { tasks, counts } = derivePlan(part);
  return (
    <div data-tool="tasks">
      <Band className={s.band}>
        <Meter counts={counts} />
      </Band>
      <ul className={s.roster} data-cap="280">
        {tasks.map((task, index) => {
          const previous = tasks[index - 1];
          const chained = Boolean(previous && task.dependsOn.includes(previous.id));
          return (
            <li
              key={task.id}
              className={s.task}
              data-status={task.status}
              data-blocked={task.blocked ? "" : undefined}
              data-chained={chained ? "" : undefined}
            >
              <span className={s.taskMark} aria-hidden="true">
                <StatusGlyph status={task.status} />
              </span>
              <span className={s.taskBody}>
                <span className={s.taskTitle}>{task.title}</span>
                {task.blocked && task.blockedBy.length > 0 ? (
                  <span className={s.taskWait}>
                    <IconHourglassLow size={14} stroke={1.8} />
                    <Text className={s.taskWaittext} tooltip="truncate">
                      waits on {task.blockedBy.join(", ")}
                    </Text>
                  </span>
                ) : null}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part) => {
  const { tasks, counts, blocked, total } = derivePlan(part);
  // An empty plan has no progress to report and no roster to open: the step
  // stays one line rather than metering a track with no segments in it and
  // offering a toggle over an empty well. A call still in flight carries no
  // plan yet, so it reads the same way until its roster lands.
  if (tasks.length === 0 && total === 0) return { object: count(0, "task", "tasks") };
  const figures = [count(counts.completed, "done", "done")];
  if (blocked > 0) figures.push(count(blocked, "blocked", "blocked"));
  return {
    object: count(tasks.length, "task", "tasks"),
    data: { kind: "count", text: figures.join(", ") },
    body: <Body part={part} />,
  };
};
