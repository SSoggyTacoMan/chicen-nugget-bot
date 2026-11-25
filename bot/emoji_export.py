"""Export custom Slack emoji as downloadable PNGs.

Slack's Web API has no "download emoji" method: ``emoji.list`` gives a name to
image URL map, and the image itself is a normal HTTPS file on
``emoji.slack-edge.com``. This module resolves names (including aliases), fetches
the source image, turns it into a square PNG, and delivers it either as an
ephemeral message in a channel or as a normal file message in a DM.
"""

from __future__ import annotations

import io
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse
from weakref import WeakKeyDictionary

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from bot.slack_api import is_slack_file_url, react, slack_host_filter

logger = logging.getLogger(__name__)

DEFAULT_EXPORT_SIZE = 512
MIN_EXPORT_SIZE = 64
MAX_EXPORT_SIZE = 1024
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_ALIAS_DEPTH = 8
EMOJI_CACHE_TTL = 10 * 60.0
_FLARON_URL = "https://flaron.halceon.dev"

_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-'.]+$")
_MARKUP_RE = re.compile(r"<a?:([A-Za-z0-9_+\-'.]+):[A-Za-z0-9]+>")
_IN_TEXT_RE = re.compile(
    r"<a?:([A-Za-z0-9_+\-'.]+):[A-Za-z0-9]+>|(?<!<):([A-Za-z0-9_+\-'.]+):"
)

_EMOJI_CACHE: WeakKeyDictionary[
    Any, tuple[float, dict[str, str] | None]
] = WeakKeyDictionary()


class EmojiExportError(RuntimeError):
    """A problem the user can fix, such as a missing or misspelled emoji."""


@dataclass(frozen=True)
class EmojiArtifact:
    requested: str
    name: str
    url: str
    png: bytes
    width: int
    height: int
    animated: bool


@dataclass(frozen=True)
class DeliveryResult:
    mode: str
    fallback_dm: bool = False
    dm_copy: bool = False
    file_id: str | None = None


@dataclass(frozen=True)
class ExportOptions:
    names: list[str]
    background: str | None = None
    size: int = DEFAULT_EXPORT_SIZE


def normalize_name(raw: Any) -> str | None:
    """Turn ``:Peak:``, ``<:peak:U123>`` or ``peak`` into ``peak``."""
    text = str(raw or "").strip()
    markup = _MARKUP_RE.fullmatch(text)
    if markup:
        text = markup.group(1)
    text = text.strip().strip(":").strip()
    if not text or not _NAME_RE.fullmatch(text) or not re.search(r"[A-Za-z]", text):
        return None
    return text.lower()


def emoji_names_in_text(text: str | None) -> list[str]:
    """Custom emoji shortcodes in a message, in the order they appear."""
    found: list[str] = []
    for match in _IN_TEXT_RE.finditer(text or ""):
        name = normalize_name(match.group(1) or match.group(2))
        if name and name not in found:
            found.append(name)
    return found


def names_from_argument(raw: str | None) -> list[str]:
    """Pull emoji names out of slash-command text.

    When the text contains shortcodes (``:peak:``), only those are used, so
    "export this :peak:" doesn't try to export ``this``. Otherwise plain words
    are treated as names, allowing ``export peak yay``.
    """
    text = (raw or "").strip()
    found = emoji_names_in_text(text)
    if found:
        return found
    names: list[str] = []
    for part in re.split(r"[\s,]+", text):
        name = normalize_name(part)
        if name and name not in names:
            names.append(name)
    return names


def parse_background(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse the optional PNG background. ``None`` means transparent."""
    text = str(value or "").strip().strip("'\"").lower()
    if text in {"", "transparent", "none", "alpha", "clear"}:
        return None
    if re.fullmatch(r"[0-9a-f]{3}|[0-9a-f]{6}", text):
        text = f"#{text}"
    try:
        from PIL import ImageColor

        rgb = ImageColor.getrgb(text)
    except Exception as exc:
        raise EmojiExportError(
            f"background must be transparent, a color name, or a hex color like `#101010`, "
            f"not `{value}`"
        ) from exc
    if len(rgb) < 3:
        raise EmojiExportError(f"couldn't read background color `{value}`")
    return int(rgb[0]), int(rgb[1]), int(rgb[2]), 255


def parse_export_args(raw: str | None) -> ExportOptions:
    """Parse ``:peak: --bg white --size 512`` style arguments."""
    tokens = (raw or "").split()
    free: list[str] = []
    background: str | None = None
    size = DEFAULT_EXPORT_SIZE
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if lower in {"--bg", "--background", "-b"}:
            if index + 1 < len(tokens):
                background = tokens[index + 1]
                index += 2
                continue
            raise EmojiExportError("`--bg` needs a color value")
        elif lower.startswith(("--bg=", "--background=")):
            background = token.split("=", 1)[1]
        elif lower in {"--size", "-s"}:
            if index + 1 < len(tokens):
                size = _clamp_size(tokens[index + 1])
                index += 2
                continue
            raise EmojiExportError("`--size` needs a pixel value")
        elif lower.startswith("--size="):
            size = _clamp_size(token.split("=", 1)[1])
        elif lower in {"--transparent", "--no-bg", "--no-background"}:
            background = None
        elif lower in {"--white", "--black"}:
            background = lower.removeprefix("--")
        elif lower.startswith("--"):
            raise EmojiExportError(f"unknown emoji export option `{token}`")
        else:
            free.append(token)
        index += 1

    names = names_from_argument(" ".join(free))
    return ExportOptions(names=names, background=background, size=size)


def _clamp_size(value: Any) -> int:
    try:
        size = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_EXPORT_SIZE
    return max(MIN_EXPORT_SIZE, min(MAX_EXPORT_SIZE, size))


def _resolve_alias(mapping: dict[str, str], name: str) -> tuple[str, str] | None:
    current = name
    seen: set[str] = set()
    for _ in range(MAX_ALIAS_DEPTH):
        if current in seen:
            return None
        seen.add(current)
        value = mapping.get(current)
        if not value:
            return None
        if value.startswith("alias:"):
            current = value.removeprefix("alias:").strip().lower()
            continue
        if is_slack_file_url(value):
            return current, value
        return None
    return None


async def _emoji_map(client: AsyncWebClient | None) -> dict[str, str] | None:
    """Custom emoji for a client, or None when the list isn't available.

    ``{}`` means the list was read and the workspace has no custom emoji: that
    is an authoritative miss. ``None`` means missing scope/network/API trouble,
    where the public flaron fallback is allowed.
    """
    if client is None:
        return None
    cached = _EMOJI_CACHE.get(client)
    if cached and time.monotonic() - cached[0] < EMOJI_CACHE_TTL:
        return cached[1]
    try:
        response = await client.emoji_list()
    except Exception as exc:  # noqa: BLE001 - missing emoji:read is normal on old installs
        logger.debug("emoji.list failed: %s", exc)
        _EMOJI_CACHE[client] = (time.monotonic(), None)
        return None
    mapping = getattr(response, "data", None)
    if isinstance(mapping, dict):
        mapping = mapping.get("emoji")
    elif isinstance(response, dict):
        mapping = response.get("emoji")
    else:
        mapping = None
    if not isinstance(mapping, dict):
        _EMOJI_CACHE[client] = (time.monotonic(), None)
        return None
    cleaned = {
        str(name).lower(): str(url)
        for name, url in mapping.items()
        if isinstance(name, str) and isinstance(url, str)
    }
    _EMOJI_CACHE[client] = (time.monotonic(), cleaned)
    if len(_EMOJI_CACHE) > 16:
        oldest = min(_EMOJI_CACHE, key=lambda item: _EMOJI_CACHE[item][0])
        _EMOJI_CACHE.pop(oldest, None)
    return cleaned


def _emoji_team(url: str) -> str:
    """The team id in a Slack emoji URL, e.g. T09V59WQY1E from the path."""
    try:
        parts = urlparse(url).path.split("/")
    except Exception:  # noqa: BLE001
        return ""
    if len(parts) > 1 and re.fullmatch(r"T[A-Z0-9]{2,}", parts[1]):
        return parts[1]
    return ""


async def _flaron_lookup(
    name: str, seen: set[str] | None = None, *, team_id: str = ""
) -> tuple[str, str] | None:
    seen = seen or set()
    if name in seen:
        return None
    seen.add(name)
    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": "chicen-nugget-bot"}
        ) as http:
            response = await http.get(f"{_FLARON_URL}/emoji/{quote(name)}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json().get("data") or {}
    except Exception as exc:  # noqa: BLE001 - flaron is only a fallback
        logger.debug("flaron emoji lookup failed for %s: %s", name, exc)
        return None
    image = str(data.get("url") or "")
    if image and is_slack_file_url(image):
        # flaron is a Hack Club directory. Never hand back another workspace's
        # emoji when the bot knows its own team id.
        if team_id and _emoji_team(image) != team_id:
            logger.debug(
                "ignoring flaron emoji %s from team %s (expected %s)",
                name,
                _emoji_team(image) or "unknown",
                team_id,
            )
            return None
        return name, image
    canonical = normalize_name(data.get("canonical") or "")
    if canonical:
        return await _flaron_lookup(canonical, seen, team_id=team_id)
    return None


async def resolve_emoji_url(
    name: str, clients: Sequence[AsyncWebClient | None], *, team_id: str = ""
) -> tuple[str, str]:
    """Return ``(resolved_name, image_url)`` for a custom emoji."""
    key = normalize_name(name)
    if not key:
        raise EmojiExportError(
            "pass an emoji name like `:peak:` or reply to a message that already has one"
        )
    seen_clients: set[int] = set()
    had_authoritative_map = False
    for client in clients:
        if client is None or id(client) in seen_clients:
            continue
        seen_clients.add(id(client))
        mapping = await _emoji_map(client)
        if mapping is None:
            continue
        had_authoritative_map = True
        resolved = _resolve_alias(mapping, key)
        if resolved:
            return resolved

    if not had_authoritative_map:
        from_flaron = await _flaron_lookup(key, team_id=team_id)
        if from_flaron:
            return from_flaron
    raise EmojiExportError(
        f"no custom workspace emoji named `:{key}:`. check the spelling in the "
        "emoji picker or with `flaron`."
    )


async def _download(url: str) -> bytes:
    if not is_slack_file_url(url):
        raise EmojiExportError("that emoji image isn't hosted on a slack file host")
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            event_hooks={"request": [slack_host_filter]},
        ) as http, http.stream("GET", url) as response:
            if response.status_code != 200:
                raise EmojiExportError(
                    f"slack returned HTTP {response.status_code} for that emoji"
                )
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                raise EmojiExportError("that emoji file is too large to export")
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > MAX_DOWNLOAD_BYTES:
                    raise EmojiExportError("that emoji file is too large to export")
            if not buffer:
                raise EmojiExportError("slack returned an empty emoji file")
            return bytes(buffer)
    except EmojiExportError:
        raise
    except Exception as exc:
        raise EmojiExportError(f"couldn't download that emoji from slack: {exc}") from exc


def render_png(
    raw: bytes,
    *,
    size: int = DEFAULT_EXPORT_SIZE,
    background: str | None = None,
) -> tuple[bytes, int, int, bool]:
    """Render source emoji bytes into a square PNG.

    Transparency is preserved by default. Animated gifs use their first frame.
    Tiny pixel-art emoji are scaled with nearest-neighbour so they stay crisp.
    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - pillow is a hard dependency now
        raise EmojiExportError("image support isn't installed") from exc

    try:
        with Image.open(io.BytesIO(raw)) as image:
            animated = bool(
                getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1
            )
            image.seek(0)
            if image.width * image.height > 40_000_000:
                raise EmojiExportError("that emoji image is too large to export")
            frame = image.convert("RGBA")
    except UnidentifiedImageError as exc:
        raise EmojiExportError("that emoji's file isn't an image") from exc
    except EmojiExportError:
        raise
    except Exception as exc:
        raise EmojiExportError(f"couldn't read that emoji image: {exc}") from exc

    if frame.width < 1 or frame.height < 1:
        raise EmojiExportError("that emoji image is empty")

    target = _clamp_size(size)
    inner = max(1, round(target * 0.92))
    scale = min(inner / frame.width, inner / frame.height)
    resized = (
        max(1, round(frame.width * scale)),
        max(1, round(frame.height * scale)),
    )
    resample = (
        Image.Resampling.NEAREST
        if max(frame.width, frame.height) <= 64
        else Image.Resampling.LANCZOS
    )
    if resized != frame.size:
        frame = frame.resize(resized, resample)

    canvas = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    canvas.alpha_composite(frame, ((target - resized[0]) // 2, (target - resized[1]) // 2))
    color = parse_background(background)
    if color is not None:
        flattened = Image.new("RGBA", canvas.size, color)
        flattened.alpha_composite(canvas)
        canvas = flattened

    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue(), target, target, animated


async def export_emoji(
    name: str,
    clients: Sequence[AsyncWebClient | None],
    *,
    size: int = DEFAULT_EXPORT_SIZE,
    background: str | None = None,
    team_id: str = "",
) -> EmojiArtifact:
    requested = normalize_name(name) or str(name)
    resolved_name, url = await resolve_emoji_url(
        requested, clients, team_id=team_id
    )
    raw = await _download(url)
    png, width, height, animated = render_png(raw, size=size, background=background)
    return EmojiArtifact(
        requested=requested,
        name=resolved_name,
        url=url,
        png=png,
        width=width,
        height=height,
        animated=animated,
    )


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.+-]+", "-", name).strip("-.") or "emoji"
    return cleaned[:80]


def _file_id(response: Any) -> str | None:
    try:
        file_obj = response.get("file")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(file_obj, dict) and file_obj.get("id"):
        return str(file_obj["id"])
    try:
        files = response.get("files") or []
    except Exception:  # noqa: BLE001
        return None
    if isinstance(files, list) and files and isinstance(files[0], dict):
        file_id = files[0].get("id")
        return str(file_id) if file_id else None
    return None


async def _upload(
    client: AsyncWebClient,
    artifact: EmojiArtifact,
    *,
    channel: str,
    thread_ts: str | None = None,
) -> Any:
    filename = f"{_safe_filename(artifact.name)}-{int(time.time())}.png"
    return await client.files_upload_v2(
        file=artifact.png,
        filename=filename,
        title=f":{artifact.name}: emoji export",
        channel=channel,
        thread_ts=thread_ts,
        initial_comment=f":white_check_mark: exported :{artifact.name}: as a png",
    )


async def _open_dm(client: AsyncWebClient, user_id: str) -> str:
    try:
        response = await client.conversations_open(users=user_id)
        channel = response.get("channel") if hasattr(response, "get") else None
        if isinstance(channel, dict) and channel.get("id"):
            return str(channel["id"])
    except Exception as exc:  # noqa: BLE001 - channel=user id often works anyway
        logger.debug("conversations.open failed for %s: %s", user_id, exc)
    return user_id


async def deliver_export(
    client: AsyncWebClient,
    artifact: EmojiArtifact,
    *,
    channel: str,
    user_id: str,
    is_dm: bool,
    thread_ts: str | None = None,
    message_ts: str | None = None,
    reaction_client: AsyncWebClient | None = None,
) -> DeliveryResult:
    """Deliver an export privately and mark the request with ✅.

    Slack only renders a private ``slack_file`` image for someone who can
    already see that file, and an ephemeral message can't carry a file
    attachment. So a channel export goes to the requester's DMs first (a normal
    file message they can download), then the same file id is used for an
    ephemeral inline preview in the channel. If Slack refuses the preview, the
    DM is already there.
    """
    if is_dm:
        await _upload(client, artifact, channel=channel, thread_ts=thread_ts)
        return DeliveryResult(mode="dm")

    dm_channel = await _open_dm(client, user_id)
    try:
        uploaded = await _upload(client, artifact, channel=dm_channel)
        file_id = _file_id(uploaded)
    except Exception as exc:
        raise EmojiExportError(
            f"couldn't save the emoji export to their dms: {exc}"
        ) from exc
    if not file_id:
        raise EmojiExportError("slack uploaded the emoji but didn't return a file id")

    text = f":white_check_mark: exported `:{artifact.name}:` as a png"
    text += " · saved to your dms"
    text += " · only you can see this"
    if artifact.animated:
        text += " · first frame of an animated emoji"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "image",
            "slack_file": {"id": file_id},
            "alt_text": f":{artifact.name}: emoji export",
            "title": {"type": "plain_text", "text": f":{artifact.name}: png"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"{artifact.width}x{artifact.height} png · "
                        "click the image to save it"
                    ),
                }
            ],
        },
    ]
    try:
        await client.chat_postEphemeral(
            channel=channel,
            user=user_id,
            thread_ts=thread_ts,
            text=text,
            blocks=blocks,
        )
        if message_ts:
            await react(
                reaction_client or client,
                channel,
                message_ts,
                "white_check_mark",
            )
        return DeliveryResult(mode="ephemeral", dm_copy=True, file_id=file_id)
    except Exception as exc:  # noqa: BLE001 - the DM is already delivered
        logger.warning("ephemeral emoji delivery failed: %s", exc)

    if message_ts:
        await react(
            reaction_client or client,
            channel,
            message_ts,
            "white_check_mark",
        )
    try:
        await client.chat_postEphemeral(
            channel=channel,
            user=user_id,
            thread_ts=thread_ts,
            text=f":white_check_mark: exported `:{artifact.name}:` to your dms",
        )
    except Exception as exc:  # noqa: BLE001 - the DM is the important part
        logger.debug("couldn't post the emoji fallback note: %s", exc)
    return DeliveryResult(
        mode="dm", fallback_dm=True, dm_copy=True, file_id=file_id
    )


def clear_cache() -> None:
    """Testing hook: forget cached emoji.list responses."""
    _EMOJI_CACHE.clear()


__all__ = [
    "DEFAULT_EXPORT_SIZE",
    "DeliveryResult",
    "EmojiArtifact",
    "EmojiExportError",
    "ExportOptions",
    "clear_cache",
    "deliver_export",
    "emoji_names_in_text",
    "export_emoji",
    "names_from_argument",
    "normalize_name",
    "parse_background",
    "parse_export_args",
    "render_png",
    "resolve_emoji_url",
]
