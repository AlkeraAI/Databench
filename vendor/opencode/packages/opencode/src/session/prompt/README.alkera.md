# session/prompt — alkera-local prompt patches

These system-prompt `.txt` files are read **verbatim into the model's system prompt** at
runtime. A `//` line here is NOT a comment — it's prompt text the model reads — so the
alkera-local edits in this directory are **NOT** wrapped in the `// == ALKERA EDIT START/END`
fences used for source files elsewhere in `vendor/opencode/`. They are documented here instead.

On an upstream bump, re-apply each by diffing against the upstream prompt and restoring the
alkera change. See [`vendor/opencode/README.alkera.md`](../../../../README.alkera.md) for the
overall patch-management story.

| File | What | Why |
| --- | --- | --- |
| **all prompts** (`anthropic`, `gpt`, `gemini`, `codex`, `beast`, `kimi`, `trinity`, `default`, `copilot-gpt-5`) | **Persona: Alkera.** `You are OpenCode`/`You are opencode`/`Your name is opencode` → **`You are Alkera`**. In `anthropic.txt` + `default.txt` also removed the opencode product blocks: the "report issues at github.com/anomalyco/opencode" feedback bullet and the "when the user asks about OpenCode, WebFetch opencode.ai/docs" paragraph (which would literally make the model pull opencode docs). | **Product persona.** The model answers as the product's assistant instead of introducing itself as "OpenCode" and fetching opencode.ai docs. No prompt tells it to deny or hide what runs it. A shared capability line ("an AI agent good at coding, but also data engineering and data science tasks") is injected for **every** model in `session/system.ts` `environment()`, so it isn't duplicated per-prompt. |
| `anthropic.txt` | Removed the upstream **Task Management** section + the "use the Task tool for codebase search" guidance, and trimmed the matching lines in **Doing tasks** / **Tool usage policy**. | The alkera harness exposes no `TodoWrite` / `Task` sub-agent delegation, so instructing the model to use them wastes tokens and produces dead tool calls. |
| `gpt.txt` | Fixed the parallel-tool-call instruction (there is no `multi_tool_use.parallel` / `parallel` tool — parallelize by emitting multiple tool calls in one response) and added a "no vision/images in this environment" note. | Stops the model emitting a non-existent `parallel` tool call and telling users they can share screenshots it can't see. |
| `plan.txt` | Added the **Ending a planning turn** section: when/how to call the `plan_present` approval tool, plus discrete-choice (`question` tool) vs open-ended (ask in prose) guidance. | Wires plan-mode to the alkera `plan_present` approval surface (`tool/plan.ts`). |
