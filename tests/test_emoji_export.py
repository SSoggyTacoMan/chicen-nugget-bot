import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from bot import emoji_export
from bot.ai.tools import Toolbox, ToolContext
from bot.config import Settings
from bot.features import commands, router


def _png(*, color=(255, 80, 40, 128), size=(64, 64), frame_count=1) -> bytes:
    frames = []
    for index in range(frame_count):
        shade = max(0, color[0] - index * 40)
        frame = Image.new("RGBA", size, (shade, color[1], color[2], color[3]))
        frames.append(frame)
    out = io.BytesIO()
    if frame_count > 1:
        frames[0].save(
            out,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
    else:
        frames[0].save(out, format="PNG")
    return out.getvalue()


def _artifact(**overrides) -> emoji_export.EmojiArtifact:
    values = {
        "requested": "peak",
        "name": "peak",
        "url": "https://emoji.slack-edge.com/T1/peak/abc.png",
        "png": _png(),
        "width": 512,
        "height": 512,
        "animated": False,
    }
    values.update(overrides)
    return emoji_export.EmojiArtifact(**values)


def _ctx(client, **overrides) -> ToolContext:
    defaults = {
        "db": None,
        "settings": Settings.__new__(Settings),
        "client": client,
        "user_client": None,
        "bot_user_id": "U_BOT",
        "user_id": "U_OWNER",
        "channel_id": "C1",
        "thread_ts": None,
        "is_dm": False,
        "is_owner": True,
        "is_admin": True,
    }
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_export_is_not_on_the_fast_chat_path():
    from bot.ai.agent import SIMPLE_CHAT_RE

    assert SIMPLE_CHAT_RE.search("export :peak:")
    assert SIMPLE_CHAT_RE.search("boi export this")


def test_name_normalization_and_extraction():
    assert emoji_export.normalize_name(":Peak:") == "peak"
    assert emoji_export.normalize_name("<:peak:U123>") == "peak"
    assert emoji_export.normalize_name("<a:partyparrot:U123>") == "partyparrot"
    assert emoji_export.normalize_name("not a name") is None
    assert emoji_export.emoji_names_in_text("look <:yay:U1> :peak: :tada:") == [
        "yay",
        "peak",
        "tada",
    ]
    assert emoji_export.emoji_names_in_text("meet at 12:30:45") == []
    assert emoji_export.names_from_argument("export this :peak:") == ["peak"]
    assert emoji_export.names_from_argument("peak, yay") == ["peak", "yay"]


def test_parse_export_args():
    options = emoji_export.parse_export_args(":peak: --bg white --size 256")
    assert options.names == ["peak"]
    assert options.background == "white"
    assert options.size == 256

    options = emoji_export.parse_export_args("peak --transparent --size=2048")
    assert options.names == ["peak"]
    assert options.background is None
    assert options.size == emoji_export.MAX_EXPORT_SIZE

    assert emoji_export.parse_background("#101010") == (16, 16, 16, 255)
    assert emoji_export.parse_background("'#101010'") == (16, 16, 16, 255)
    assert emoji_export.parse_background("transparent") is None
    try:
        emoji_export.parse_background("not-a-color")
    except emoji_export.EmojiExportError as exc:
        assert "background" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid background should fail")
    try:
        emoji_export.parse_export_args(":peak: --bogus")
    except emoji_export.EmojiExportError as exc:
        assert "unknown" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown options should fail")


def test_render_png_keeps_transparency_and_can_flatten():
    transparent, width, height, animated = emoji_export.render_png(_png(), size=128)
    assert (width, height) == (128, 128)
    assert animated is False
    with Image.open(io.BytesIO(transparent)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((64, 64))[3] == 128

    white, _, _, _ = emoji_export.render_png(_png(), size=128, background="white")
    with Image.open(io.BytesIO(white)) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255, 255)


def test_render_png_marks_animated_sources():
    _, _, _, animated = emoji_export.render_png(_png(frame_count=2), size=64)
    assert animated is True


def test_alias_resolution():
    mapping = {
        "peak": "https://emoji.slack-edge.com/T1/peak/a.png",
        "best": "alias:peak",
        "goat": "alias:best",
        "broken": "alias:missing",
    }
    assert emoji_export._resolve_alias(mapping, "goat") == (
        "peak",
        "https://emoji.slack-edge.com/T1/peak/a.png",
    )
    assert emoji_export._resolve_alias(mapping, "broken") is None


def test_resolve_emoji_from_slack_emoji_list():
    async def run():
        emoji_export.clear_cache()
        client = AsyncMock()
        client.emoji_list.return_value = {
            "emoji": {
                "peak": "https://emoji.slack-edge.com/T1/peak/a.png",
                "best": "alias:peak",
            }
        }
        resolved = await emoji_export.resolve_emoji_url("best", [client])
        assert resolved == (
            "peak",
            "https://emoji.slack-edge.com/T1/peak/a.png",
        )
        # The list is cached instead of hammering emoji.list.
        await emoji_export.resolve_emoji_url("peak", [client])
        assert client.emoji_list.await_count == 1

    asyncio.run(run())


def test_authoritative_map_miss_does_not_use_flaron(monkeypatch):
    async def run():
        emoji_export.clear_cache()
        client = AsyncMock()
        client.emoji_list.return_value = {
            "emoji": {"peak": "https://emoji.slack-edge.com/T1/peak/a.png"}
        }
        called: list[str] = []

        async def fake_flaron(name, seen=None, *, team_id=""):
            called.append(name)
            return ("wrong", "https://emoji.slack-edge.com/T9/wrong/b.png")

        monkeypatch.setattr(emoji_export, "_flaron_lookup", fake_flaron)
        try:
            await emoji_export.resolve_emoji_url("missing", [client], team_id="T1")
        except emoji_export.EmojiExportError as exc:
            assert "missing" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("missing emoji should fail")
        assert called == []

    asyncio.run(run())


def test_missing_emoji_scope_is_cached(monkeypatch):
    async def run():
        emoji_export.clear_cache()
        client = AsyncMock()
        client.emoji_list.side_effect = RuntimeError("missing_scope")

        async def fake_flaron(name, seen=None, *, team_id=""):
            return None

        monkeypatch.setattr(emoji_export, "_flaron_lookup", fake_flaron)
        for _ in range(2):
            try:
                await emoji_export.resolve_emoji_url("peak", [client])
            except emoji_export.EmojiExportError:
                pass
        assert client.emoji_list.await_count == 1

    asyncio.run(run())


def test_emoji_team_from_url():
    assert (
        emoji_export._emoji_team(
            "https://emoji.slack-edge.com/T09V59WQY1E/peak/abc.png"
        )
        == "T09V59WQY1E"
    )
    assert emoji_export._emoji_team("https://example.com/not-slack.png") == ""


def test_deliver_dm_uploads_a_normal_file():
    async def run():
        client = AsyncMock()
        client.files_upload_v2.return_value = {"file": {"id": "F1"}}
        artifact = _artifact()
        result = await emoji_export.deliver_export(
            client,
            artifact,
            channel="D1",
            user_id="U1",
            is_dm=True,
        )
        assert result.mode == "dm"
        upload = client.files_upload_v2.await_args.kwargs
        assert upload["channel"] == "D1"
        assert upload["file"] == artifact.png
        assert upload["filename"].endswith(".png")
        client.chat_postEphemeral.assert_not_awaited()
        client.reactions_add.assert_not_awaited()

    asyncio.run(run())


def test_deliver_channel_is_ephemeral_and_reacts():
    async def run():
        client = AsyncMock()
        client.files_upload_v2.return_value = {"file": {"id": "F1"}}
        client.conversations_open.return_value = {"channel": {"id": "D9"}}
        artifact = _artifact()
        result = await emoji_export.deliver_export(
            client,
            artifact,
            channel="C1",
            user_id="U1",
            is_dm=False,
            thread_ts="100.1",
            message_ts="123.456",
            reaction_client=client,
        )
        assert result.mode == "ephemeral"
        assert result.dm_copy is True
        upload = client.files_upload_v2.await_args.kwargs
        assert upload["channel"] == "D9"
        assert upload["file"] == artifact.png
        ephemeral = client.chat_postEphemeral.await_args.kwargs
        assert ephemeral["channel"] == "C1"
        assert ephemeral["user"] == "U1"
        assert ephemeral["thread_ts"] == "100.1"
        assert "saved to your dms" in ephemeral["text"]
        blocks = ephemeral["blocks"]
        assert any(block.get("slack_file", {}).get("id") == "F1" for block in blocks)
        client.reactions_add.assert_awaited_once_with(
            channel="C1", timestamp="123.456", name="white_check_mark"
        )

    asyncio.run(run())


def test_deliver_channel_falls_back_to_the_dm_when_preview_fails():
    async def run():
        client = AsyncMock()
        client.files_upload_v2.return_value = {"file": {"id": "F1"}}
        client.conversations_open.return_value = {"channel": {"id": "D9"}}
        client.chat_postEphemeral.side_effect = RuntimeError("invalid_blocks")
        result = await emoji_export.deliver_export(
            client,
            _artifact(),
            channel="C1",
            user_id="U1",
            is_dm=False,
            message_ts="123.456",
        )
        assert result.mode == "dm"
        assert result.fallback_dm is True
        assert result.dm_copy is True
        uploads = [call.kwargs for call in client.files_upload_v2.await_args_list]
        assert len(uploads) == 1
        assert uploads[0]["channel"] == "D9"
        client.reactions_add.assert_awaited_once()

    asyncio.run(run())


def test_tool_export_emoji_detects_from_trigger_text(monkeypatch):
    async def run():
        client = AsyncMock()
        calls = {}

        async def fake_export(name, clients, *, size=512, background=None, team_id=""):
            calls["name"] = name
            return _artifact(name=name)

        async def fake_deliver(client, artifact, **kwargs):
            calls["deliver"] = kwargs
            return emoji_export.DeliveryResult(mode="ephemeral")

        monkeypatch.setattr(emoji_export, "export_emoji", fake_export)
        monkeypatch.setattr(emoji_export, "deliver_export", fake_deliver)
        box = Toolbox(Settings.__new__(Settings))
        out = await box.execute(
            "export_emoji",
            {},
            _ctx(client, trigger_text="export this :peak:", message_ts="123.456"),
        )
        assert calls["name"] == "peak"
        assert calls["deliver"]["message_ts"] == "123.456"
        assert "exported :peak:" in out
        assert "[NO_REPLY]" in out

    asyncio.run(run())


def test_tool_export_emoji_detects_from_context(monkeypatch):
    async def run():
        client = AsyncMock()
        calls = {}

        async def fake_export(name, clients, *, size=512, background=None, team_id=""):
            calls["name"] = name
            return _artifact(name=name)

        async def fake_deliver(client, artifact, **kwargs):
            return emoji_export.DeliveryResult(mode="ephemeral")

        monkeypatch.setattr(emoji_export, "export_emoji", fake_export)
        monkeypatch.setattr(emoji_export, "deliver_export", fake_deliver)
        box = Toolbox(Settings.__new__(Settings))
        out = await box.execute(
            "export_emoji",
            {},
            _ctx(
                client,
                trigger_text="export this",
                context_text="someone: :yay: then :peak:",
            ),
        )
        assert calls["name"] == "peak"
        assert "[NO_REPLY]" in out

    asyncio.run(run())


def test_tool_export_emoji_treats_this_as_context(monkeypatch):
    async def run():
        client = AsyncMock()
        calls = {}

        async def fake_export(name, clients, *, size=512, background=None, team_id=""):
            calls["name"] = name
            return _artifact(name=name)

        async def fake_deliver(client, artifact, **kwargs):
            return emoji_export.DeliveryResult(mode="ephemeral")

        monkeypatch.setattr(emoji_export, "export_emoji", fake_export)
        monkeypatch.setattr(emoji_export, "deliver_export", fake_deliver)
        box = Toolbox(Settings.__new__(Settings))
        await box.execute(
            "export_emoji",
            {"name": "this"},
            _ctx(client, context_text="someone: :peak:"),
        )
        assert calls["name"] == "peak"

    asyncio.run(run())


def test_tool_export_emoji_requires_a_name():
    box = Toolbox(Settings.__new__(Settings))
    out = asyncio.run(box.execute("export_emoji", {}, _ctx(AsyncMock())))
    assert "pass an emoji name" in out


def test_export_emoji_is_a_guest_tool_and_registered():
    box = Toolbox(MagicMock())
    names = {tool["function"]["name"] for tool in box.definitions}
    assert "export_emoji" in names
    assert "export_emoji" in Toolbox.GUEST_TOOLS
    assert router.SUBCOMMANDS["emoji"] is commands.cmd_emoji
    assert router.SUBCOMMANDS["export"] is commands.cmd_emoji


def test_emoji_slash_command_uses_private_delivery(monkeypatch):
    async def run():
        client = AsyncMock()
        ack = AsyncMock()
        calls = {}

        async def fake_export(name, clients, *, size=512, background=None, team_id=""):
            return _artifact(name=name, width=size, height=size)

        async def fake_deliver(client, artifact, **kwargs):
            calls.update(kwargs)
            return emoji_export.DeliveryResult(mode="ephemeral")

        monkeypatch.setattr(emoji_export, "export_emoji", fake_export)
        monkeypatch.setattr(emoji_export, "deliver_export", fake_deliver)
        command = {
            "text": "export :peak: --size 256",
            "channel_id": "C1",
            "user_id": "U1",
            "thread_ts": "",
        }
        await commands.cmd_emoji(ack, command, client)
        ack.assert_awaited_once()
        assert ":white_check_mark:" in ack.await_args.kwargs["text"]
        assert calls["channel"] == "C1"
        assert calls["is_dm"] is False
        assert calls["thread_ts"] is None

    asyncio.run(run())


def test_emoji_slash_command_uses_a_normal_message_in_dms(monkeypatch):
    async def run():
        client = AsyncMock()
        ack = AsyncMock()
        calls = {}

        async def fake_export(name, clients, *, size=512, background=None, team_id=""):
            return _artifact(name=name)

        async def fake_deliver(client, artifact, **kwargs):
            calls.update(kwargs)
            return emoji_export.DeliveryResult(mode="dm")

        monkeypatch.setattr(emoji_export, "export_emoji", fake_export)
        monkeypatch.setattr(emoji_export, "deliver_export", fake_deliver)
        command = {
            "text": ":peak:",
            "channel_id": "D1",
            "user_id": "U1",
            "thread_ts": None,
        }
        await commands.cmd_emoji(ack, command, client)
        assert calls["channel"] == "D1"
        assert calls["is_dm"] is True

    asyncio.run(run())
