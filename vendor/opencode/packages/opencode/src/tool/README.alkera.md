# tool — alkera-local prompt/tool patches

The `.txt` tool-description files here are read **verbatim into the model's tool definitions**
at runtime, so (unlike the `.ts` sources) the alkera edits are **NOT** wrapped in the
`// == ALKERA EDIT START/END` fences — a `//` line would just become tool-description text the
model reads. They are documented here instead. See
[`vendor/opencode/README.alkera.md`](../../../../README.alkera.md).

The matching `plan.ts` source IS fenced inline (it's TypeScript), so look there for the
`plan_present` implementation; this note covers only the prompt `.txt` companions.

| File | New/Modified | What | Why |
| --- | --- | --- | --- |
| `plan-present.txt` | **New** | Tool description for the alkera `plan_present` tool (defined in `plan.ts`). | The plan-approval surface used by the harness — the model calls it to present a finished plan for the user to accept/reject. |
| `question.txt` | Modified | Added: use `question` only for a DISCRETE choice; for an OPEN-ENDED answer, ask in prose instead of fabricating a placeholder option. | Stops the model forcing free-form questions into meaningless multiple-choice prompts. |
