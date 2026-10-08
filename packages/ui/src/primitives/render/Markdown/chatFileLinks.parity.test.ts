// The shared file-link cases, read by the web renderer's rule.
//
// `api-core/tests/fixtures/chat_paths/links.json` is read here AND by the rule
// the Slack thread follows (`packages/api-core/tests/test_chat_link_parity.py`),
// so a file the chat opens from a link is a file the thread attaches.

import { describe, expect, it } from "vitest";

import { chatFileLinks } from "./chatFileLinks";
import shared from "../../../../../api-core/tests/fixtures/chat_paths/links.json";

describe("the files a message links to, as every surface reads them", () => {
  for (const testCase of shared.cases) {
    it(testCase.id, () => {
      const links = chatFileLinks(testCase.markdown, shared.chat_id).map(({ label, path }) => ({
        label,
        path,
      }));
      expect(links).toEqual(testCase.links);
    });
  }
});
