import path from "path"
import { Effect, Schema } from "effect"
import * as Tool from "./tool"
import { Question } from "../question"
import { Session } from "@/session/session"
import { MessageV2 } from "../session/message-v2"
import { Provider } from "@/provider/provider"
import { InstanceState } from "@/effect/instance-state"
import { MessageID, PartID } from "../session/schema"
import EXIT_DESCRIPTION from "./plan-exit.txt"
// == ALKERA EDIT START
import PRESENT_DESCRIPTION from "./plan-present.txt"
// == ALKERA EDIT END

export const Parameters = Schema.Struct({})

// == ALKERA EDIT START
// plan-approval is FILE-BASED: the model writes the plan to a `plan.md` file in
// its sandbox (which it can edit incrementally), then passes that file's PATH
// here. The tool reads the file and the host (alkera-cli) renders its content as
// the approval prompt body — so the full plan is surfaced to the user WITHOUT
// being re-fed into the model's context as a tool argument/result.
const PlanPresentParameters = Schema.Struct({
  path: Schema.String.annotate({
    description:
      "The PATH to your plan file. Write the full plan as Markdown to a `plan.md` file " +
      "in your sandbox directory using the write tool FIRST (you can edit it as you " +
      "refine), then pass that file's path here. The user reads the file and approves it.",
  }),
})

// plan-approval surface. The host (alkera-cli) tags this question by
// its `header` and maps the chosen accept label → its own permission mode.
// These three labels MUST stay byte-identical to alkera_cli's
// PLAN_ACCEPT_OPTIONS (apps/cli/alkera_cli/harness/permission_mode.py).
const PLAN_APPROVAL_HEADER = "alkera:plan-approval"
const PLAN_ACCEPT_LABELS = [
  "Accept — run normally (ask before each change)",
  "Accept — auto mode (run automatically, pause for risky steps)",
  "Accept — bypass all permission prompts",
] as const
// == ALKERA EDIT END

export const PlanExitTool = Tool.define(
  "plan_exit",
  Effect.gen(function* () {
    const session = yield* Session.Service
    const question = yield* Question.Service
    const provider = yield* Provider.Service

    return {
      description: EXIT_DESCRIPTION,
      parameters: Parameters,
      execute: (_params: {}, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const instance = yield* InstanceState.context
          const info = yield* session.get(ctx.sessionID)
          const plan = path.relative(instance.worktree, Session.plan(info, instance))
          const answers = yield* question.ask({
            sessionID: ctx.sessionID,
            questions: [
              {
                question: `Plan at ${plan} is complete. Would you like to switch to the build agent and start implementing?`,
                header: "Build Agent",
                custom: false,
                options: [
                  { label: "Yes", description: "Switch to build agent and start implementing the plan" },
                  { label: "No", description: "Stay with plan agent to continue refining the plan" },
                ],
              },
            ],
            tool: ctx.callID ? { messageID: ctx.messageID, callID: ctx.callID } : undefined,
          })

          if (answers[0]?.[0] === "No") yield* new Question.RejectedError()

          const messages = yield* session.messages({ sessionID: ctx.sessionID }).pipe(Effect.orDie)
          const lastUser = messages.findLast((item) => item.info.role === "user" && item.info.model)
          const model =
            lastUser?.info.role === "user" && lastUser.info.model ? lastUser.info.model : yield* provider.defaultModel()

          const msg: MessageV2.User = {
            id: MessageID.ascending(),
            sessionID: ctx.sessionID,
            role: "user",
            time: { created: Date.now() },
            agent: "build",
            model,
          }
          yield* session.updateMessage(msg)
          yield* session.updatePart({
            id: PartID.ascending(),
            messageID: msg.id,
            sessionID: ctx.sessionID,
            type: "text",
            text: `The plan at ${plan} has been approved, you can now edit files. Execute the plan`,
            synthetic: true,
          } satisfies MessageV2.TextPart)

          return {
            title: "Switching to build agent",
            output: "User approved switching to build agent. Wait for further instructions.",
            metadata: {},
          }
        }).pipe(Effect.orDie),
    }
  }),
)

// == ALKERA EDIT START
// present a finished plan for approval. The user either accepts
// (picking how much autonomy the executing agent gets) or types free-form
// feedback to reject. On accept we switch to the build agent and tell the
// model to execute; on reject we hand the feedback back so the model revises
// while staying in plan mode. The host (alkera-cli) reads the same answer off
// the question's sentinel header to flip its own permission mode.
export const PlanPresentTool = Tool.define(
  "plan_present",
  Effect.gen(function* () {
    const session = yield* Session.Service
    const question = yield* Question.Service
    const provider = yield* Provider.Service

    return {
      description: PRESENT_DESCRIPTION,
      parameters: PlanPresentParameters,
      execute: (params: { path: string }, ctx: Tool.Context) =>
        Effect.gen(function* () {
          // Read the model's plan FILE (it wrote + edited it in its sandbox). The
          // content becomes the question body — never re-fed to the model.
          const instance = yield* InstanceState.context
          const planPath = path.isAbsolute(params.path)
            ? params.path
            : path.resolve(instance.worktree, params.path)
          const planText = yield* Effect.promise(() => Bun.file(planPath).text().catch(() => ""))
          if (!planText.trim()) {
            return {
              title: "No plan file",
              output:
                `No plan found at "${params.path}". Write your plan as Markdown to a ` +
                `plan.md file in your sandbox first (use the write tool), then call ` +
                `plan_present with that path. Do not start implementing yet.`,
              metadata: {},
            }
          }
          const answers = yield* question.ask({
            sessionID: ctx.sessionID,
            questions: [
              {
                // The plan file's content is the question body — the host renders it
                // as Markdown above the accept/reject choices, guaranteeing the user
                // always sees the plan.
                question: planText,
                header: PLAN_APPROVAL_HEADER,
                custom: true,
                options: PLAN_ACCEPT_LABELS.map((label) => ({
                  label,
                  description: "Approve the plan and start implementing",
                })),
              },
            ],
            tool: ctx.callID ? { messageID: ctx.messageID, callID: ctx.callID } : undefined,
          })

          const answer = answers[0]?.[0] ?? ""
          const accepted = (PLAN_ACCEPT_LABELS as readonly string[]).includes(answer)
          // The host carries a reader's note after the chosen label; the model
          // gets it with either outcome.
          const note = (answers[0]?.[1] ?? "").trim()
          const withNote = (text: string) => (note ? `${text} Note from the user: ${note}` : text)

          if (!accepted) {
            // Free-form answer (or dismissal) = rejection. Keep the plan
            // agent; hand the feedback back so the model revises + re-presents.
            const feedback = answer.trim()
            return {
              title: "Plan not approved",
              output: withNote(
                feedback
                  ? `The user did not approve the plan. Their feedback: "${feedback}". ` +
                      `Revise the plan to address this, present the revised plan, and call ` +
                      `plan_present again. Do not start implementing yet.`
                  : `The user did not approve the plan. Refine it and call plan_present again ` +
                      `when ready. Do not start implementing yet.`,
              ),
              metadata: {},
            }
          }

          // Accepted — switch to the build agent and instruct execution.
          const messages = yield* session.messages({ sessionID: ctx.sessionID }).pipe(Effect.orDie)
          const lastUser = messages.findLast((item) => item.info.role === "user" && item.info.model)
          const model =
            lastUser?.info.role === "user" && lastUser.info.model ? lastUser.info.model : yield* provider.defaultModel()

          const msg: MessageV2.User = {
            id: MessageID.ascending(),
            sessionID: ctx.sessionID,
            role: "user",
            time: { created: Date.now() },
            agent: "build",
            model,
          }
          yield* session.updateMessage(msg)
          yield* session.updatePart({
            id: PartID.ascending(),
            messageID: msg.id,
            sessionID: ctx.sessionID,
            type: "text",
            text: withNote("The plan has been approved. You can now edit files and run commands. Execute the plan."),
            synthetic: true,
          } satisfies MessageV2.TextPart)

          return {
            title: "Plan approved",
            output: withNote(`User approved the plan ("${answer}"). Switching to the build agent — execute the plan.`),
            metadata: {},
          }
        }).pipe(Effect.orDie),
    }
  }),
)
// == ALKERA EDIT END
