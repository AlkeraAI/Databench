// ipydatagrid's VegaExprModel compiles cell expressions with Function(),
// which the output frame's CSP refuses: the grid then draws headers and leaves
// every cell empty. The patch evaluates the same expression with an
// interpreter instead.
import type { ModulePatch } from "./registry";
import { compileExpression } from "./vegaInterpreter";

interface VegaExprModelLike {
  get(key: string): unknown;
  _function: (cell: unknown, defaultValue: unknown, functions: Record<string, unknown>) => unknown;
}

export const ipydatagridVegaExpr: ModulePatch = {
  name: "VegaExprModel.updateFunction",
  module: "ipydatagrid",
  range: ">=1.0.0 <2.0.0",
  apply(exports) {
    const model = exports.VegaExprModel as { prototype?: Record<string, unknown> } | undefined;
    if (!model?.prototype || typeof model.prototype.updateFunction !== "function") return false;
    model.prototype.updateFunction = function updateFunction(this: VegaExprModelLike): void {
      const evaluate = compileExpression(String(this.get("value")));
      this._function = (cell, defaultValue, functions) =>
        evaluate({ vars: { cell, default_value: defaultValue }, functions });
    };
    return true;
  },
};
