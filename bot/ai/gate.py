"""Jev gate: should the bot answer this message at all?

Reading every message in a busy channel and replying to all of them is how a
bot becomes annoying. `typesafe/jev-router` is a decision-only model, so we ask
it one yes/no question per ambient message and only run the expensive agent
turn when it says yes. Mentions and DMs skip the gate entirely.
"""

from __future__ import annotations

import asyncio
import logging
import re

from openai import AsyncOpenAI

from bot.config import Settings

logger = logging.getLogger(__name__)

SYSTEM = """\
You are the decision layer for a Slack bot called {bot} in a Hack Club community.

Decide whether {bot} should reply to the latest message in {channel}, sent by {user}.

Say YES if any of these is true:
- someone asks a programming or tech question — code, tools, servers, APIs, or building bots (Slack bots included)
- someone asks any other question the bot could usefully answer
- someone seems stuck and asking for help
- someone is talking to or about the bot
- a short friendly reaction, congratulations, or joke genuinely adds something

Say NO if any of these is true:
- it is chatter between other people that doesn't involve the bot
- it's a joke, meme, or reaction that doesn't need a reply
- it's spam, a bot message, or a link dump
- replying would just repeat what someone already said

When genuinely unsure, answer NO.

Examples:
"anyone know why my docker build fails?" -> YES
"how do i make a slack bot in python?" -> YES
"just shipped my first website!!" -> YES
"lol that's so true" -> NO
"omg same" -> NO
"bruh" -> NO

Reply with exactly YES or NO and nothing else."""

_TRUE = {"y", "yes", "yeah", "yep", "true", "1"}


def decision(raw: str | None) -> bool:
    """Parse a jev answer into a boolean, defaulting to no."""
    if not raw:
        return False
    word = re.sub(r"[^a-z]", "", raw.strip().lower().split(" ")[0])
    return word in _TRUE or word.startswith("yes")


class Gate:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.llm = AsyncOpenAI(
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url,
            timeout=settings.ai_gate_timeout,
            max_retries=0,
        )

    async def should_reply(
        self,
        *,
        text: str,
        channel_name: str = "a channel",
        user_name: str = "someone",
        recent: list[str] | None = None,
    ) -> bool:
        if not self.settings.ai_gate_enabled or not text.strip():
            return False

        system = SYSTEM.format(
            bot=self.settings.bot_name,
            channel=channel_name,
            user=user_name,
        )
        user = text.strip()
        if recent:
            user = "Recent messages:\n" + "\n".join(recent[-4:]) + f"\n\nLatest message:\n{text.strip()}"

        for attempt in (1, 2):
            try:
                async with asyncio.timeout(self.settings.ai_gate_timeout + 1):
                    response = await self.llm.chat.completions.create(
                        model=self.settings.ai_gate_model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=256,
                        temperature=0,
                    )
                answer = response.choices[0].message.content if response.choices else None
                if answer is None:
                    logger.warning("gate attempt %s returned no content", attempt)
                    if attempt == 1:
                        await asyncio.sleep(0.4)
                    continue
                result = decision(answer)
                logger.info("gate %s for %r -> %s", "YES" if result else "NO", text[:60], answer)
                return result
            except Exception as exc:  # noqa: BLE001
                logger.warning("gate attempt %s failed: %s", attempt, exc)
                if attempt == 1:
                    await asyncio.sleep(0.4)

        # fail closed: a broken gate means no unprompted replies
        return False


__all__ = ["Gate", "decision"]
