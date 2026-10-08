// The web's twin of `alkera_core.permission_presentation.outcome`, held to the
// same vectors the Python side is (`test_permission_outcome.py`), so a settled
// ask and a mode change read the same in the browser and in a Slack thread.

import { describe, expect, it } from "vitest";

import { OUTCOME_VECTORS, modeChangePresentation, resolutionLine } from "./permissionPresentation";

describe("a settled ask reads the same on every surface", () => {
  for (const vector of OUTCOME_VECTORS.resolutions) {
    it(vector.line, () => {
      expect(
        resolutionLine(vector.optionId, vector.decidedBy, vector.deciderName, vector.surface),
      ).toBe(vector.line);
    });
  }
});

describe("a mode change reads the same on every surface", () => {
  for (const vector of OUTCOME_VECTORS.modeChanges) {
    it(vector.presentation.attribution + " " + vector.mode, () => {
      expect(
        modeChangePresentation(vector.mode, vector.previousMode, vector.changerName, vector.surface),
      ).toEqual(vector.presentation);
    });
  }
});
