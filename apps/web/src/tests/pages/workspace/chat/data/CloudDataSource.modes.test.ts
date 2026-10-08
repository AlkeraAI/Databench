// The stances the browser offers a cloud chat.
//
// `auto` is the ceiling a reader raises a chat to when the model needs a page
// no search of its own returned: the list is what the picker, the home chip and
// the preferences page all read, so its contents and order are pinned here
// against the editor's own vocabulary rather than against a copy.

import { describe, expect, it } from "vitest";
import { PERMISSION_MODES, PERMISSION_MODE_VALUES } from "@alkera/chat-model";

import { PERMISSION_MODE_OPTIONS } from "@/pages/workspace/chat/adapters";
import { switchableModes } from "@/pages/workspace/chat/data/ChatDataSource";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

describe("the cloud stance list", () => {
  it("offers auto as the ceiling below bypass", () => {
    expect(PERMISSION_MODE_VALUES).toContain("auto");
    expect(PERMISSION_MODE_VALUES.indexOf("auto")).toBeLessThan(
      PERMISSION_MODE_VALUES.indexOf("bypass"),
    );
  });

  it("is the generated list, in its order, and what the menu offers", () => {
    const generated = PERMISSION_MODES.map((mode) => mode.value);
    expect([...(new CloudDataSource().caps.permissionModes ?? [])]).toEqual(generated);
    expect(PERMISSION_MODE_OPTIONS.map((mode) => mode.value)).toEqual(generated);
  });

  it("is every mode an editor-backed source may switch to", () => {
    expect([...switchableModes({ opencodeActive: true } as Parameters<typeof switchableModes>[0])]).toEqual(
      PERMISSION_MODES.map((mode) => mode.value),
    );
  });
});
