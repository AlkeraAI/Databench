"""Which files in the chat a reply puts in front of a reader.

The box checks a published reply's file references against the chat's folder,
so it has to read the reply the way the web transcript does: the pictures it
shows, the file links it opens, and every ``blob:`` link it opens through the
file the result was written out to (``ChatResultLink``). The staging reply this
was written against linked a result whose written-out file the agent had
deleted, and the web said the file "has not arrived" for good.
"""

from __future__ import annotations

import pytest
from alkera_core.chat_paths import ChatBlobLink, chat_blob_links, reply_files

CHAT = "5e76966f-9423-484b-8b7f-0ec781609d7e"
HANDLE = "9a21e98bc2c79b9d281d573492f1cb28875e2cfefebdf89fdd187ee4372f8bf8"
#: Where ``blob.materialize`` said it wrote the result, word for word.
WRITTEN = f"/opt/alkera-work/.alkera/chats/{CHAT}/scratch/result-9a21e98b.csv"
#: The reply as the agent published it on staging.
STAGING_REPLY = (
    "Read from pg (postgres): `target_prompts` (status, category, locale) lives there.\n\n"
    "![Active target prompts by category and locale](active_prompts_by_category_locale.png)\n\n"
    "1,071 active prompts total. Full breakdown:\n\n"
    f"[Active prompts by category/locale](blob:{HANDLE})"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(f"[Rows](blob:{HANDLE})", [ChatBlobLink("Rows", HANDLE)], id="own-line"),
        pytest.param(
            f"See [the rows](blob:{HANDLE}) for detail.",
            [ChatBlobLink("the rows", HANDLE)],
            id="inline",
        ),
        pytest.param(f"[Rows](blob://{HANDLE})", [ChatBlobLink("Rows", HANDLE)], id="double-slash"),
        pytest.param(
            f"[A](blob:{HANDLE}) and again [B](blob:{HANDLE})",
            [ChatBlobLink("A", HANDLE)],
            id="once-per-handle",
        ),
        pytest.param(f"```\n[Rows](blob:{HANDLE})\n```", [], id="inside-a-fence"),
        pytest.param(f"`[Rows](blob:{HANDLE})`", [], id="inside-a-code-span"),
        pytest.param(f"![Rows](blob:{HANDLE})", [], id="written-as-an-image"),
        pytest.param("[Rows](rows.csv)", [], id="a-file-link-is-not-a-result"),
        pytest.param("[Rows](blob:)", [], id="no-handle"),
    ],
)
def test_chat_blob_links(text: str, expected: list[ChatBlobLink]) -> None:
    assert chat_blob_links(text) == expected


def test_the_staging_reply_names_its_chart_and_the_file_its_result_link_opens() -> None:
    found = reply_files(STAGING_REPLY, chat_id=CHAT, result_files={HANDLE: WRITTEN})

    assert [(f.label, f.path.anchor, f.path.path, f.handle) for f in found] == [
        (
            "Active target prompts by category and locale",
            "working",
            "active_prompts_by_category_locale.png",
            None,
        ),
        ("Active prompts by category/locale", "chat", "scratch/result-9a21e98b.csv", HANDLE),
    ]
    # Code that only looks like a word is not a reference the reader can open.
    assert all(f.path.path != "target_prompts" for f in found)


def test_a_result_never_written_out_is_not_a_file_reference() -> None:
    """The web shows such a link as its label and waits for nothing, so there is
    no file for the box to check."""
    found = reply_files(STAGING_REPLY, chat_id=CHAT, result_files={})

    assert [f.path.path for f in found] == ["active_prompts_by_category_locale.png"]


def test_a_result_written_into_another_chats_folder_is_not_this_chats_file() -> None:
    other = WRITTEN.replace(CHAT, "00000000-0000-4000-8000-000000000000")

    found = reply_files(STAGING_REPLY, chat_id=CHAT, result_files={HANDLE: other})

    assert all(f.handle is None for f in found)


def test_a_file_shown_and_linked_is_one_reference() -> None:
    text = "![Chart](c.png)\n\nOpen [the chart](c.png) or [again](./c.png)."

    found = reply_files(text, chat_id=CHAT)

    assert [(f.label, f.path.path) for f in found] == [("Chart", "c.png")]
