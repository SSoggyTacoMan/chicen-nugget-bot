"""AI-powered slash commands: /ask, /summarize, /forget and /imagine."""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx

from bot.runtime import runtime
from bot.ai.limits import user_retry_after
from bot.slack_api import error_code, post, post_ephemeral
from bot.utils.text import chunk_text, disarm_broadcasts, escape_mrkdwn
from bot.utils.tz import tzinfo

logger = logging.getLogger(__name__)

MAX_SUMMARY_MESSAGES = 200
MAX_TRANSCRIPT_CHARS = 45_000

ASK_SYSTEM = """you are {bot}, answering a one-off /ask in a Slack workspace.
be direct, casual and useful. answer first, then a little color if it helps.
use Slack mrkdwn: *bold*, _italic_, `code`. no markdown headings. keep it under 250 words.
if you are unsure, say so instead of inventing facts. current time: {now}."""

SUMMARY_SYSTEM = """you are {bot}, writing a short catch-up for a Slack channel.
summarise the messages into 3-6 crisp bullets: decisions, questions, links, and anything
someone should act on. skip small talk and bot noise. use Slack mrkdwn. no headings.
if nothing important happened, say that in one line. current time: {now}."""


# ---------------------------------------------------------------------- /ask
async def cmd_ask(ack, command, client) -> None:
    await ack()
    channel = command["channel_id"]
    user = command["user_id"]
    question = (command.get("text") or "").strip()
    if not await _ai_ready(client, channel, user):
        return
    if not _ai_allowed(channel):
        await post_ephemeral(
            client,
            channel,
            user,
            "i'm not allowed to use ai in this channel. add it to `AI_CHANNELS` first.",
        )
        return
    if not question:
        await post_ephemeral(client, channel, user, "usage: `/ask <question>`")
        return
    retry = user_retry_after(user)
    if retry:
        await post_ephemeral(client, channel, user, f"slow down a lil — try again in {retry}s :3")
        return

    await post_ephemeral(client, channel, user, ":brain: thinking…")
    answer = await runtime.agent.complete(
        system=ASK_SYSTEM.format(
            bot=runtime.settings.bot_name,
            now=datetime.now(tzinfo()).strftime("%A %d %B %Y, %H:%M %Z"),
        ),
        text=question,
        max_tokens=min(runtime.settings.ai_max_tokens, 900),
        user_id=user,
    )
    await _post_answer(client, channel, user, question, answer)


async def _post_answer(client, channel: str, user: str, question: str, answer: str) -> None:
    chunks = chunk_text(disarm_broadcasts(answer))
    if not chunks:
        chunks = ["i blanked on that one. try rephrasing?"]
    header = f":speech_balloon: <@{user}> asked: {escape_mrkdwn(question)}"
    for index, chunk in enumerate(chunks):
        text = f"{header}\n\n{chunk}" if index == 0 else chunk
        await post(client, channel, text)


# ---------------------------------------------------------------- /summarize
async def cmd_summarize(ack, command, client) -> None:
    await ack()
    channel = command["channel_id"]
    user = command["user_id"]
    if not await _ai_ready(client, channel, user):
        return
    if channel.startswith("D"):
        await post_ephemeral(client, channel, user, "run `/summarize` in a channel, not in DMs.")
        return
    if not _ai_allowed(channel):
        await post_ephemeral(
            client,
            channel,
            user,
            "i'm not allowed to use ai in this channel. add it to `AI_CHANNELS` first.",
        )
        return
    retry = user_retry_after(user)
    if retry:
        await post_ephemeral(client, channel, user, f"slow down a lil — try again in {retry}s :3")
        return

    limit = _summary_limit(command.get("text"))
    await post_ephemeral(client, channel, user, f":mag: reading the last {limit} messages…")
    try:
        transcript = await _fetch_transcript(client, channel, limit)
    except Exception as exc:  # noqa: BLE001
        await post_ephemeral(
            client,
            channel,
            user,
            f"couldn't read this channel: `{error_code(exc)}`. am i invited here?",
        )
        return
    if len(transcript) < 3:
        await post_ephemeral(
            client, channel, user, "not enough recent messages to summarise. talk more :3"
        )
        return

    body = "\n".join(transcript)[:MAX_TRANSCRIPT_CHARS]
    summary = await runtime.agent.complete(
        system=SUMMARY_SYSTEM.format(
            bot=runtime.settings.bot_name,
            now=datetime.now(tzinfo()).strftime("%A %d %B %Y, %H:%M %Z"),
        ),
        text=f"channel: <#{channel}>\n\n{body}",
        max_tokens=min(runtime.settings.ai_max_tokens, 900),
        user_id=user,
    )
    header = f":newspaper: *catch-up for <#{channel}>* — last {len(transcript)} messages"
    chunks = chunk_text(disarm_broadcasts(summary))
    if not chunks:
        chunks = ["nothing memorable happened."]
    for index, chunk in enumerate(chunks):
        text = f"{header}\n\n{chunk}" if index == 0 else chunk
        await post(client, channel, text)


def _summary_limit(text: str | None) -> int:
    match = re.search(r"\d+", text or "")
    if not match:
        return 50
    return max(10, min(MAX_SUMMARY_MESSAGES, int(match.group(0))))


async def _fetch_transcript(client, channel: str, limit: int) -> list[str]:
    response = await client.conversations_history(channel=channel, limit=limit)
    messages = list(response.get("messages") or [])
    messages.reverse()
    tz = tzinfo()
    lines: list[str] = []
    for message in messages:
        if message.get("subtype") in {"channel_join", "channel_leave", "channel_topic", "channel_purpose"}:
            continue
        text = (message.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        speaker = message.get("user") or message.get("bot_id") or "unknown"
        stamp = message.get("ts") or ""
        try:
            when = datetime.fromtimestamp(float(stamp), tz).strftime("%a %H:%M")
        except (TypeError, ValueError, OSError):
            when = "??:??"
        lines.append(f"[{when}] <@{speaker}>: {text[:700]}")
    return lines


# ------------------------------------------------------------------- /forget
async def cmd_forget(ack, command, client) -> None:
    await ack()
    channel = command["channel_id"]
    user = command["user_id"]
    settings = runtime.settings
    if channel.startswith("D"):
        cleared = runtime.db.clear_history(f"dm:{user}")
        await post_ephemeral(
            client,
            channel,
            user,
            f"wiped *{cleared}* stored turns from our DM. notes are untouched. :broom:",
        )
        return
    if not settings.is_admin(user):
        await post_ephemeral(client, channel, user, "only admins can wipe channel memory.")
        return
    cleared = runtime.db.clear_history_prefix(f"ch:{channel}:")
    await post_ephemeral(
        client,
        channel,
        user,
        f"wiped *{cleared}* stored turns from every ai thread in this channel. :broom:",
    )


# ------------------------------------------------------------------ /imagine
class ImageError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def cmd_imagine(ack, command, client) -> None:
    await ack()
    channel = command["channel_id"]
    user = command["user_id"]
    settings = runtime.settings
    if not settings.is_admin(user):
        await post_ephemeral(client, channel, user, "only admins can make images — they cost tokens.")
        return
    if not settings.imagine_enabled:
        await post_ephemeral(
            client,
            channel,
            user,
            "image generation is off. set `AI_API_KEY` and `IMAGINE_MODEL` first.",
        )
        return
    if await _budget_blocked(client, channel, user):
        return
    prompt, ratio = _parse_imagine(command.get("text") or "")
    if not prompt:
        await post_ephemeral(
            client,
            channel,
            user,
            "usage: `/imagine a tiny nugget astronaut --wide` (ratios: `--square`, `--wide`, `--tall`, `--ratio 4:3`)",
        )
        return
    retry = user_retry_after(user)
    if retry:
        await post_ephemeral(client, channel, user, f"slow down a lil — try again in {retry}s :3")
        return

    await post_ephemeral(client, channel, user, ":art: painting… this takes a few seconds.")
    try:
        image, mime = await _generate_image(prompt, ratio)
    except ImageError as exc:
        await post_ephemeral(client, channel, user, _image_error(exc))
        return
    except Exception:  # noqa: BLE001
        logger.exception("image generation failed")
        await post_ephemeral(client, channel, user, "image generation broke. check my logs?")
        return

    if runtime.budget is not None:
        runtime.budget.record(
            kind="image",
            model=settings.imagine_model or "image",
            user_id=user,
            cost_usd=settings.ai_image_cost_usd,
        )

    extension = "png" if "png" in mime else "jpg"
    try:
        await client.files_upload_v2(
            channel=channel,
            file=image,
            filename=f"nugget-{int(time.time())}.{extension}",
            initial_comment=f":art: <@{user}> imagined: {prompt}",
        )
    except Exception as exc:  # noqa: BLE001
        code = error_code(exc)
        if code == "missing_scope":
            hint = "i need the `files:write` bot scope — reinstall the app from the manifest."
        elif code == "not_in_channel":
            hint = "invite me to this channel first."
        else:
            hint = "check my logs for the slack error."
        await post_ephemeral(client, channel, user, f"couldn't upload the image (`{code}`). {hint}")


def _parse_imagine(text: str) -> tuple[str, str]:
    ratio = "1:1"
    prompt = (text or "").strip()
    flags = {
        "--square": "1:1",
        "--wide": "16:9",
        "--landscape": "16:9",
        "--tall": "9:16",
        "--portrait": "9:16",
    }
    for flag, value in flags.items():
        if flag in prompt:
            prompt = re.sub(rf"\s*{re.escape(flag)}\b", "", prompt)
            ratio = value
    match = re.search(r"--ratio\s+(\d{1,2}:\d{1,2})", prompt)
    if match:
        ratio = match.group(1)
        prompt = prompt[: match.start()] + prompt[match.end() :]
    return prompt.strip(), ratio


async def _generate_image(prompt: str, ratio: str) -> tuple[bytes, str]:
    settings = runtime.settings
    models = [settings.imagine_model]
    if settings.imagine_fallback_model and settings.imagine_fallback_model != settings.imagine_model:
        models.append(settings.imagine_fallback_model)

    last: ImageError | None = None
    for model in models:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": ratio},
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                response = await http.post(
                    f"{settings.ai_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001
            last = ImageError(0, f"request failed: {exc}")
            logger.warning("image model %s failed: %s", model, exc)
            continue
        if response.status_code != 200:
            last = ImageError(response.status_code, response.text[:400])
            logger.warning(
                "image model %s returned %s: %s", model, response.status_code, response.text[:200]
            )
            continue
        message = (((response.json().get("choices") or [{}])[0]).get("message") or {})
        images = message.get("images") or []
        if not images:
            last = ImageError(502, "the model returned no image")
            continue
        url = ((images[0].get("image_url") or {}).get("url")) or ""
        mime = "image/png"
        if url.startswith("data:"):
            header, _, data = url.partition(",")
            mime = header[5:].split(";")[0] or mime
        else:
            data = url
        try:
            return base64.b64decode(data), mime
        except (ValueError, TypeError) as exc:
            last = ImageError(502, f"could not decode the image: {exc}")
    if last:
        raise last
    raise ImageError(502, "no image model configured")


def _image_error(exc: ImageError) -> str:
    if exc.status == 402:
        return "my hack club ai daily budget is used up. it resets at 00:00 UTC."
    if exc.status == 429:
        return "the image model is rate limited right now. give it a minute."
    if exc.status in {502, 503, 504, 0}:
        return "the image model is having a moment. try again in a bit."
    if exc.status == 401:
        return "my ai key got rejected. check `AI_API_KEY`."
    return f"image generation failed (`{exc.status}`). try a different prompt?"


# ------------------------------------------------------------------- helpers
async def _ai_ready(client, channel: str, user: str) -> bool:
    if not runtime.settings.ai_enabled or runtime.agent is None:
        await post_ephemeral(
            client,
            channel,
            user,
            "my ai brain isn't configured. set `AI_API_KEY`, `AI_BASE_URL` and `AI_MODEL`, then restart me.",
        )
        return False
    if await _budget_blocked(client, channel, user):
        return False
    return True


async def _budget_blocked(client, channel: str, user: str) -> bool:
    budget = runtime.budget
    if budget is None:
        return False
    status = budget.status()
    if status.exhausted:
        await post_ephemeral(
            client,
            channel,
            user,
            f"i've used up my ai budget for today (${status.spent:.2f} of ${status.limit:.2f}). "
            "it resets at 00:00 UTC.",
        )
        return True
    user_status = budget.user_status(user)
    if user_status.exhausted:
        await post_ephemeral(
            client,
            channel,
            user,
            f"you've hit today's ai limit (${user_status.spent:.2f} of ${user_status.limit:.2f}). "
            "it resets at 00:00 UTC.",
        )
        return True
    return False


def _ai_allowed(channel: str) -> bool:
    return channel.startswith("D") or runtime.settings.ai_allowed(channel)


__all__ = ["_parse_imagine", "_summary_limit"]
