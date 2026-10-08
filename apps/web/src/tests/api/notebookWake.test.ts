// A notebook request asked again while the server wakes the notebook's
// machine: the wait the server names is kept, the waiting is said once each
// way, any other answer ends it as it came, and a wake past the patience is
// given up with the server's last answer.

import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { NotebookRequestError, untilAwake } from "@/api/notebooks";

const waking = (retryAfter: string | null = "3") =>
  new NotebookRequestError(503, { code: "notebook.waking", message: "Waking the machine…" }, new Headers(retryAfter === null ? {} : { "Retry-After": retryAfter }));

function script(answers: ReadonlyArray<unknown>) {
  let asked = 0;
  return {
    attempt: async () => {
      const answer = answers[asked++];
      if (answer instanceof Error) throw answer;
      return answer;
    },
    asked: () => asked,
  };
}

function clock() {
  let at = 0;
  const slept: number[] = [];
  return {
    now: () => at,
    sleep: async (ms: number) => {
      slept.push(ms);
      at += ms;
    },
    slept,
  };
}

describe("untilAwake", () => {
  it("asks again after each wait the server names, and says once that it waited", async () => {
    const asks = script([waking("3"), waking("5"), "ran"]);
    const time = clock();
    const said: boolean[] = [];
    await expect(untilAwake(asks.attempt, { onWaking: (w) => said.push(w), ...time })).resolves.toBe("ran");
    expect(asks.asked()).toBe(3);
    expect(time.slept).toEqual([3_000, 5_000]);
    expect(said).toEqual([true, false]);
  });

  it("waits three seconds when the server names no wait", async () => {
    const time = clock();
    await untilAwake(script([waking(null), "ran"]).attempt, { onWaking: () => {}, ...time });
    expect(time.slept).toEqual([3_000]);
  });

  it("says nothing of a wake when the first answer is the result", async () => {
    const said: boolean[] = [];
    await expect(untilAwake(script(["ran"]).attempt, { onWaking: (w) => said.push(w), ...clock() })).resolves.toBe("ran");
    expect(said).toEqual([]);
  });

  it("ends on any other refusal as it came, and stops saying it waits", async () => {
    const refused = new NotebookRequestError(409, { code: "notebook.no_machine", message: "no machine holds this notebook's folder" });
    const asks = script([waking(), refused]);
    const said: boolean[] = [];
    await expect(untilAwake(asks.attempt, { onWaking: (w) => said.push(w), ...clock() })).rejects.toBe(refused);
    expect(said).toEqual([true, false]);
  });

  it("does not take a 503 of another code for a wake", async () => {
    const busy = new ApiError(503, { code: "notebook.kernel_silent", message: "The machine did not answer. Try again." });
    const asks = script([busy, "ran"]);
    await expect(untilAwake(asks.attempt, { onWaking: () => {}, ...clock() })).rejects.toBe(busy);
    expect(asks.asked()).toBe(1);
  });

  it("gives a wake up past its patience with the server's last answer", async () => {
    const last = waking();
    const asks = script([waking(), waking(), last, "never"]);
    const said: boolean[] = [];
    await expect(untilAwake(asks.attempt, { onWaking: (w) => said.push(w), patienceMs: 6_000, ...clock() })).rejects.toBe(last);
    expect(asks.asked()).toBe(3);
    expect(said).toEqual([true, false]);
  });
});
