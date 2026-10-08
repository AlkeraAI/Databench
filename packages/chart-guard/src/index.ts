// The renderer's wall for Alkera charts. See `guard.ts` and `expression.ts`.

export { guardSpec, type GuardResult } from "./guard";
export {
  checkExpression,
  EXPRESSION_CONSTANTS,
  EXPRESSION_FUNCTIONS,
  MAX_EXPRESSION_ARGS,
  MAX_EXPRESSION_CHARS,
  MAX_EXPRESSION_DEPTH,
  MAX_PAD_LENGTH,
  type ExpressionVerdict,
} from "./expression";
