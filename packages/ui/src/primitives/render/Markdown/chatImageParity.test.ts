// The shared picture cases, read by the web renderer's block parser.
//
// `api-core/tests/fixtures/chat_paths/images.json` is read here AND by the
// Slack thread's rule (`packages/api-core/tests/test_chat_image_parity.py`), so
// a picture the chat shows is one the thread gets too.

import { describe, expect, it } from "vitest";

import { parseMarkdownBlocks } from "./markdownBlocks";
import shared from "../../../../../api-core/tests/fixtures/chat_paths/images.json";

describe("the pictures a message shows, as every surface reads them", () => {
  for (const testCase of shared.cases) {
    it(testCase.id, () => {
      const images = parseMarkdownBlocks(testCase.markdown)
        .filter((block) => block.kind === "image")
        .map((block) => (block.kind === "image" ? { alt: block.alt, path: block.path } : null));
      expect(images).toEqual(testCase.images);
    });
  }
});
