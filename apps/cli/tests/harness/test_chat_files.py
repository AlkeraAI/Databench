"""Files inside a chat as a message names them: the stored name of a pasted
file, the one way a relative path resolves, and the FILES AND IMAGES brief that
tells the agent the same contract the renderer and the preview panel enforce."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from alkera_cli.harness import chat_files
from alkera_cli.harness.chat_files import (
    STAGE_FILE_MAX_BYTES,
    UPLOADS_FOLDER,
    relative_chat_path,
    resolve_chat_file,
    stage_file_size_label,
    staged_file_name,
    write_staged_file,
)
from alkera_cli.harness.system_prompt import (
    CHAT_IMAGE_EXTENSIONS,
    FILES_BLOCK,
    compose_main_agent_guidance,
)
from alkera_cli.plugins.plugin_base.blob_write_tools import BlobMaterializeOutput

_REPO = Path(__file__).resolve().parents[4]

_FILES_BLOCK_SNAPSHOT = (
    "FILES AND IMAGES. To show the user an image inline — a chart, a plot, a screenshot — write"
    " it into your working directory and reference it in your reply as markdown with the path "
    "RELATIVE TO THAT DIRECTORY: `![Revenue by month](revenue.png)`, on its own line (a "
    "subfolder works too: `![Plot](charts/q3.png)`). An image written inside a sentence shows as"
    " a plain link, not a picture, in the chat and in Slack alike. Write only the caption and "
    "the path: the chat shows every image at one fixed size, so never add a width, a height or "
    "a size of any kind. Any other file you want the user to open — a report, a CSV, a PDF, "
    "an HTML page — "
    "name it the same way as a link: `[Q3 report](q3-report.html)`; the user clicks it and it "
    "opens in a panel beside the chat, and the file is highlighted in their Files panel. Inline"
    " image types: .png, .jpg, .jpeg, .gif, .webp, .svg; draw a chart as PNG, JPEG, GIF or "
    "WebP, since Slack does not display an SVG. Every message that shows a path shows "
    "the file as it is NOW, so overwriting a path changes every message that shows it: when an "
    "earlier image should keep showing what it showed (the chart before a fix, a first draft), "
    "write the new one under a NEW name (`revenue-v2.png`) instead of overwriting it. Types the"
    " panel opens: images, PDF, HTML, Markdown, CSV, JSON, plain text, code, MP4/WebM video and"
    " MP3/WAV audio; anything else is offered as a download. Only that form renders: a base64 "
    "`data:` URI does not, a web URL (`https://…`) does not, an absolute path or a `..` path "
    "does not, and an inline `<img>` or `<svg>` tag is shown as text. A tool that answers with "
    "an ABSOLUTE path (`blob.materialize`, the plan file) is naming a file inside your working "
    "directory: drop the directory and write the rest. A file a reply shows or links must "
    "still be there when the reply is read, so never delete, move or rename it afterwards, not "
    "even when cleaning up; that includes the file `blob.materialize` wrote for a result whose "
    "`blob:` link you present, which is the file that link opens in the web chat. "
    "What a file IS is decided from its "
    "bytes, never its extension. Keep a file the user will open under 10 MB. A write refused "
    "with `files.quota_bytes` or `files.user_quota_bytes` means the drive is full: say so and "
    "stop, do not retry. What the user hands the chat is in `uploads/` in your working "
    "directory. An image they paste arrives the same way — `![Image "
    "1](uploads/paste-1-ab12.png)` — and the file is readable at that path; a file they attach "
    "arrives as a link — `[File 1: report.csv](uploads/file-1-cd34.csv)` — readable at that "
    "path. An older message may name one at the top level (`paste-1-ab12.png`); it is readable "
    "where it names it."
)


@pytest.mark.parametrize(
    ("kind", "n", "original", "prefix", "ext"),
    [
        pytest.param("image", 1, "Screenshot 2026-09-16.PNG", "paste-1-", ".png", id="image-png"),
        pytest.param("image", 3, "photo.jpeg", "paste-3-", ".jpeg", id="image-jpeg"),
        pytest.param("file", 2, "quarterly report.csv", "file-2-", ".csv", id="file-csv"),
        pytest.param(
            "file", 1, "C:\\Users\\me\\notes.txt", "file-1-", ".txt", id="windows-path-name"
        ),
        pytest.param("file", 1, "Makefile", "file-1-", "", id="no-extension"),
        pytest.param("file", 1, ".env", "file-1-", "", id="dotfile-is-not-an-extension"),
        pytest.param(
            "file", 1, "weird.T@r!", "file-1-", ".tr", id="extension-stripped-to-path-safe"
        ),
    ],
)
def test_staged_file_name_is_deterministic_in_shape(
    kind: str, n: int, original: str, prefix: str, ext: str
) -> None:
    folder, _, name = staged_file_name(kind, n, original).partition("/")  # type: ignore[arg-type]
    assert folder == UPLOADS_FOLDER == "uploads"
    assert name.startswith(prefix)
    assert name.endswith(ext)
    assert "/" not in name
    # Four hex digits between the number and the extension: the web shell's
    # `stagedFileName` draws the same width, so a staged name has one shape
    # whichever shell wrote it.
    assert re.fullmatch(re.escape(prefix) + r"[0-9a-f]{4}" + re.escape(ext), name)


def test_an_upload_whose_drawn_name_is_taken_never_overwrites_the_earlier_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id is four hex digits and every message numbers its pastes from 1,
    so a chat with a few hundred uploads draws a name it already holds. The
    earlier message keeps its image: the writer draws again instead."""
    drawn = iter(["68a1", "68a1", "68a1", "beef"])
    monkeypatch.setattr(chat_files.secrets, "token_hex", lambda _n: next(drawn))

    first = write_staged_file(tmp_path, "image", 1, "a.png", b"first")
    second = write_staged_file(tmp_path, "image", 1, "b.png", b"second")

    assert first == "uploads/paste-1-68a1.png"
    assert second == "uploads/paste-1-beef.png"
    assert (tmp_path / first).read_bytes() == b"first"
    assert (tmp_path / second).read_bytes() == b"second"


def test_an_uploads_folder_with_no_free_name_refuses_instead_of_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_files.secrets, "token_hex", lambda _n: "68a1")
    write_staged_file(tmp_path, "file", 1, "a.csv", b"kept")

    with pytest.raises(FileExistsError):
        write_staged_file(tmp_path, "file", 1, "b.csv", b"lost")
    assert (tmp_path / "uploads" / "file-1-68a1.csv").read_bytes() == b"kept"


def test_a_staged_file_is_written_under_uploads_and_answers_the_path_that_resolves(
    tmp_path: Path,
) -> None:
    path = write_staged_file(tmp_path, "image", 1, "Screenshot.PNG", b"\x89PNG")

    assert path.startswith("uploads/paste-1-")
    assert (tmp_path / "uploads").is_dir()
    assert [p.name for p in (tmp_path / "uploads").iterdir()] == [path.removeprefix("uploads/")]
    assert resolve_chat_file(tmp_path, path) == (tmp_path / path).resolve()
    assert (tmp_path / path).read_bytes() == b"\x89PNG"


def test_a_second_staged_file_reuses_the_uploads_folder(tmp_path: Path) -> None:
    first = write_staged_file(tmp_path, "file", 1, "a.csv", b"a")
    second = write_staged_file(tmp_path, "file", 2, "b.csv", b"b")

    assert sorted(p.name for p in (tmp_path / "uploads").iterdir()) == sorted(
        [first.removeprefix("uploads/"), second.removeprefix("uploads/")]
    )
    assert [p.name for p in tmp_path.iterdir()] == ["uploads"]


def test_a_file_staged_before_uploads_existed_still_resolves_at_the_top_level(
    tmp_path: Path,
) -> None:
    (tmp_path / "paste-1-ab12.png").write_bytes(b"\x89PNG")
    assert (
        resolve_chat_file(tmp_path, "paste-1-ab12.png") == (tmp_path / "paste-1-ab12.png").resolve()
    )


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("../secret.png", id="parent-escape"),
        pytest.param("a/../../x.png", id="buried-escape"),
        pytest.param("/etc/passwd", id="absolute"),
        pytest.param("https://example.com/x.png", id="url"),
        pytest.param("data:image/png;base64,AAAA", id="data-uri"),
        pytest.param("a\\b.png", id="windows-separator"),
        pytest.param("a//b.png", id="empty-segment"),
        pytest.param("", id="empty"),
        pytest.param(" x.png", id="leading-space"),
    ],
)
def test_relative_chat_path_refuses_everything_outside_the_root(target: str) -> None:
    assert relative_chat_path(target) is None


def test_relative_chat_path_normalizes_a_leading_dot_segment() -> None:
    assert str(relative_chat_path("./charts/q3.png")) == "charts/q3.png"


def test_resolve_chat_file_finds_a_file_inside_the_root(tmp_path: Path) -> None:
    (tmp_path / "charts").mkdir()
    (tmp_path / "charts" / "q3.png").write_bytes(b"\x89PNG")
    assert (
        resolve_chat_file(tmp_path, "charts/q3.png") == (tmp_path / "charts" / "q3.png").resolve()
    )


def test_resolve_chat_file_answers_none_for_a_missing_file_and_a_folder(tmp_path: Path) -> None:
    (tmp_path / "charts").mkdir()
    assert resolve_chat_file(tmp_path, "gone.png") is None
    assert resolve_chat_file(tmp_path, "charts") is None


@pytest.mark.parametrize("target", ["../outside.png", "/outside.png", "https://x/y.png"])
def test_resolve_chat_file_refuses_an_escape_even_when_the_file_exists(
    tmp_path: Path, target: str
) -> None:
    root = tmp_path / "chat"
    root.mkdir()
    (tmp_path / "outside.png").write_bytes(b"x")
    assert resolve_chat_file(root, target) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_resolve_chat_file_refuses_a_symlink_that_leaves_the_root(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    root.mkdir()
    (tmp_path / "outside.png").write_bytes(b"x")
    (root / "link.png").symlink_to(tmp_path / "outside.png")
    assert resolve_chat_file(root, "link.png") is None


def test_the_stage_cap_bounds_the_message_and_not_the_deployment_s_file_ceiling() -> None:
    """A staged file crosses the daemon as one base64 JSON-RPC request held
    whole in memory on both sides — it never reaches the Files session API, the
    edge or the object store — so it is bounded by what a message may carry and
    stays far below what Alkera Files accepts. A cap that followed the file
    ceiling would put a terabyte through a single RPC frame."""
    from alkera_core.config import settings

    assert STAGE_FILE_MAX_BYTES == 10 * 1024 * 1024
    assert STAGE_FILE_MAX_BYTES < settings.files_max_file_bytes
    assert stage_file_size_label() == "10 MB"


class TestFilesBrief:
    def test_it_rides_with_the_sandbox_and_only_then(self) -> None:
        with_sandbox = compose_main_agent_guidance(sandbox_dir="/box/chat")
        assert "FILES AND IMAGES." in with_sandbox
        # Just before the working-directory sentence, which stays the last thing read.
        assert with_sandbox.index("FILES AND IMAGES.") < with_sandbox.index("WORKING DIRECTORY.")
        assert "FILES AND IMAGES." not in compose_main_agent_guidance()

    def test_it_states_the_one_form_relative_to_the_working_directory(self) -> None:
        assert "`![Revenue by month](revenue.png)`" in FILES_BLOCK
        assert "RELATIVE TO THAT DIRECTORY" in FILES_BLOCK
        # The working directory IS the root: no folder prefix in the form.
        assert "outputs/" not in FILES_BLOCK
        assert "scratch/" not in FILES_BLOCK

    def test_it_names_exactly_the_types_the_renderer_accepts(self) -> None:
        """The renderer's list lives in TypeScript; read it from there so a type
        added on one side only fails here instead of becoming an image the
        agent never writes (or writes and nobody sees)."""
        source = (_REPO / "packages/ui/src/primitives/render/Markdown/chatPaths.ts").read_text()
        declared = re.search(r"CHAT_IMAGE_EXTENSIONS[^=]*=\s*new Set\(\[(.*?)\]\)", source, re.S)
        assert declared is not None
        renderer = tuple(re.findall(r'"([a-z0-9]+)"', declared.group(1)))
        assert renderer == CHAT_IMAGE_EXTENSIONS
        listed = ", ".join(f".{ext}" for ext in renderer)
        assert f"Inline image types: {listed};" in FILES_BLOCK

    def test_it_never_asks_for_an_image_size(self) -> None:
        assert "never add a width, a height or a size of any kind" in FILES_BLOCK
        assert "width=" not in FILES_BLOCK
        assert "{width" not in FILES_BLOCK

    def test_it_says_to_write_a_new_name_to_keep_an_earlier_image(self) -> None:
        assert "overwriting a path changes every message that shows it" in FILES_BLOCK
        assert "write the new one under a NEW name (`revenue-v2.png`)" in FILES_BLOCK

    @pytest.mark.parametrize(
        "kind",
        [
            pytest.param("images", id="images"),
            pytest.param("PDF", id="pdf"),
            pytest.param("HTML", id="html"),
            pytest.param("Markdown", id="markdown"),
            pytest.param("CSV", id="csv"),
            pytest.param("JSON", id="json"),
            pytest.param("plain text", id="text"),
            pytest.param("code", id="code"),
            pytest.param("MP4/WebM video", id="video"),
            pytest.param("MP3/WAV audio", id="audio"),
        ],
    )
    def test_it_names_every_type_the_panel_opens(self, kind: str) -> None:
        """A type the panel renders but the brief omits is a file the agent
        never offers; a type the brief claims but the panel cannot open is a
        dead link. The list is the panel's."""
        opens = FILES_BLOCK[FILES_BLOCK.index("Types the panel opens:") :]
        assert kind in opens[: opens.index("Only that form renders")]

    def test_a_file_that_is_not_an_image_is_offered_as_a_link_the_panel_opens(self) -> None:
        assert "name it the same way as a link: `[Q3 report](q3-report.html)`" in FILES_BLOCK
        assert "it opens in a panel beside the chat" in FILES_BLOCK
        assert "highlighted in their Files panel" in FILES_BLOCK
        assert "anything else is offered as a download" in FILES_BLOCK

    @pytest.mark.parametrize(
        "refusal",
        [
            pytest.param("a base64 `data:` URI does not", id="base64"),
            pytest.param("a web URL (`https://…`) does not", id="web-url"),
            pytest.param("an absolute path or a `..` path does not", id="escape"),
            pytest.param("an inline `<img>` or `<svg>` tag is shown as text", id="raw-tag"),
        ],
    )
    def test_it_says_what_does_not_render(self, refusal: str) -> None:
        assert refusal in FILES_BLOCK

    def test_it_turns_an_absolute_tool_path_into_the_reference_form(self) -> None:
        """Several tools answer with an absolute path inside the working
        directory. Pasted as-is it renders as nothing, so the brief says to drop
        the directory rather than leaving the model to guess."""
        assert "A tool that answers with an ABSOLUTE path" in FILES_BLOCK
        assert "drop the directory and write the rest" in FILES_BLOCK

    def test_it_says_a_linked_file_must_outlive_the_reply(self) -> None:
        """An agent that wrote a result out, charted it and deleted the file as
        clean-up left a `blob:` link that opened nothing on the web: the link
        opens the file the result was written to."""
        assert "never delete, move or rename it afterwards, not even when cleaning up" in (
            FILES_BLOCK
        )
        assert "the file `blob.materialize` wrote for a result whose `blob:` link" in FILES_BLOCK

    def test_a_full_drive_stops_the_turn_instead_of_retrying(self) -> None:
        assert "`files.quota_bytes` or `files.user_quota_bytes`" in FILES_BLOCK
        assert "say so and stop, do not retry" in FILES_BLOCK

    def test_the_type_of_a_file_is_decided_from_its_bytes(self) -> None:
        assert "What a file IS is decided from its bytes, never its extension." in FILES_BLOCK

    def test_it_states_the_cap_the_composer_enforces(self) -> None:
        """The brief and the composer have to name the same ceiling, or the
        agent writes a file the chat then refuses to hand over."""
        assert f"under {stage_file_size_label()}" in FILES_BLOCK

    def test_it_describes_what_the_user_hands_over_under_uploads(self) -> None:
        assert "What the user hands the chat is in `uploads/` in your working directory." in (
            FILES_BLOCK
        )
        assert "`![Image 1](uploads/paste-1-ab12.png)`" in FILES_BLOCK
        assert "`[File 1: report.csv](uploads/file-1-cd34.csv)`" in FILES_BLOCK
        # The examples are the shapes the stager really writes.
        assert staged_file_name("image", 1, "x.png").startswith("uploads/paste-1-")
        assert staged_file_name("file", 1, "x.csv").startswith("uploads/file-1-")

    def test_the_rendered_block(self) -> None:
        """The whole block as the agent reads it, so any change to it is a
        change someone reviewed."""
        assert FILES_BLOCK == _FILES_BLOCK_SNAPSHOT

    def test_what_it_tells_the_agent_about_placement_is_what_the_chat_and_slack_do(
        self,
    ) -> None:
        """The brief's two claims, checked against the rule the chat and the
        Slack thread both follow (``alkera_core.chat_paths.chat_images``): its
        own-line example is a picture, and the same image written inside a
        sentence is not."""
        from alkera_core.chat_paths import chat_images

        assert "`![Revenue by month](revenue.png)`, on its own line" in FILES_BLOCK
        assert "An image written inside a sentence shows as a plain link" in FILES_BLOCK
        chat = "d6d800e9-5bce-4d9f-b0e0-dd5367ea4298"
        own_line = chat_images("Here:\n\n![Revenue by month](revenue.png)\n", chat_id=chat)
        assert [(i.alt, i.path.path) for i in own_line] == [("Revenue by month", "revenue.png")]
        assert chat_images("See ![Revenue by month](revenue.png) here.", chat_id=chat) == []

    def test_it_tells_the_agent_slack_does_not_display_an_svg(self) -> None:
        assert "svg" in CHAT_IMAGE_EXTENSIONS, "the chat itself still shows one"
        assert "draw a chart as PNG, JPEG, GIF or WebP, since Slack does not display an SVG" in (
            FILES_BLOCK
        )


def test_the_materialized_path_tells_the_model_how_to_reference_it() -> None:
    """`blob.materialize` answers an absolute path. The field that carries it is
    what the model reads through the tool schema, so it is where the reference
    form belongs — not only in a prompt block the tool result never quotes."""
    described = BlobMaterializeOutput.model_json_schema()["properties"]["path"]["description"]
    assert "Reference it in a reply by its name relative to the working directory." in described
