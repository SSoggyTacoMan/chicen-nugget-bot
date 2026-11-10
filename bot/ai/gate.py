"""The Jev gate: typed decisions about a message before spending an agent turn.

Jev answers several questions about the same state in one pass:

- should_reply: probability that a reply would genuinely add something
- addressed:    probability that the message is aimed at the bot
- spam:         probability that it is spam, a link dump, or not worth engaging
- kind:         what sort of message it is (question, help, celebration, chatter…)

The bot only runs the expensive agent turn when should_reply clears its
threshold (or the message is clearly for the bot) and it doesn't look like
spam. Everything fails closed: a broken gate means no unprompted replies.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from bot.ai.jev import Answer, Jev
from bot.config import Settings

logger = logging.getLogger(__name__)

SHOULD_REPLY = """\
Would a short reply from this bot genuinely add something to the conversation?

True when someone:
- asks a question, especially about programming, code, tools, servers, APIs or bots
- asks for help, a review, feedback, a link, an opinion, or a recommendation
- seems stuck, confused, or blocked on something
- is talking to or about the bot, or replying to something it said
- shares a win, a project, or something they clearly want acknowledged

False when:
- it is chatter between other people that doesn't involve the bot
- it is a joke, meme, reaction, or one-word message that needs no reply
- it is spam, a link dump, a mass ping, or low-effort noise
- a reply would just repeat what someone already said
"""

ADDRESSED = """\
Is this message aimed at the bot? True when it addresses the bot by name
("nugget", "chicen"), thanks it, asks it something directly, or replies to it.
False when it is a general message to the channel or a conversation between
other people.
"""

SPAM = """\
Is this message spam, a scam, a mass ping, a bare link dump, or something a
community bot should not engage with? False for normal conversation, jokes,
questions, and people sharing their work.
"""

KIND_INSTRUCTIONS = "What kind of message is this?"

KIND_CRITERIA = {
    "question": "A question the bot could answer",
    "help": "Someone is stuck, blocked, or asking for help",
    "feedback": "A request for review, feedback, a link, or an opinion",
    "celebration": "Someone sharing a win or something they're proud of",
    "bot": "Someone is talking to or about the bot",
    "chatter": "Casual conversation between other people",
    "spam": "Spam, a scam, a mass ping, or a link dump",
    "other": "Nothing above fits",
}

QUESTIONS: dict[str, dict[str, Any]] = {
    "should_reply": {"type": "noul", "instructions": SHOULD_REPLY},
    "addressed": {"type": "noul", "instructions": ADDRESSED},
    "spam": {"type": "noul", "instructions": SPAM},
    "kind": {
        "type": "choice",
        "instructions": KIND_INSTRUCTIONS,
        "criteria": KIND_CRITERIA,
    },
}


@dataclass(frozen=True)
class GateDecision:
    """What Jev thinks about one message."""

    reply: bool
    should_reply: float = 0.0
    addressed: float = 0.0
    spam: float = 0.0
    kind: str = "other"
    kind_confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    def hint(self) -> str:
        """A short line the agent can use to pitch its reply."""
        if self.kind in {"question", "help", "feedback", "celebration", "bot"}:
            return f"jev reads it as a {self.kind} message ({self.kind_confidence:.0%} confident)"
        return ""


class Gate:
    def __init__(self, settings: Settings, jev: Jev | None = None) -> None:
        self.settings = settings
        self.jev = jev or Jev(settings)

    async def decide(
        self,
        *,
        text: str,
        channel_name: str = "a channel",
        user_name: str = "someone",
        recent: list[str] | None = None,
    ) -> GateDecision:
        if not self.settings.ai_gate_enabled or not text.strip():
            return GateDecision(reply=False, error="gate disabled")

        state = _state(text, channel_name, user_name, recent)
        answers: dict[str, Answer] | None = None
        for attempt in (1, 2):
            try:
                async with asyncio.timeout(self.settings.ai_gate_timeout + 1):
                    answers = await self.jev.ask(state=state, questions=QUESTIONS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("jev gate attempt %s failed: %s", attempt, exc)
                answers = None
            if answers is not None:
                break
            if attempt == 1:
                await asyncio.sleep(0.3)

        if not answers:
            return GateDecision(reply=False, error="jev unavailable")

        should = answers.get("should_reply")
        addressed = answers.get("addressed")
        spam = answers.get("spam")
        kind = answers.get("kind")
        if should is None:
            return GateDecision(reply=False, error="jev returned no should_reply answer")

        p_should = should.probability
        p_addressed = addressed.probability if addressed else 0.0
        p_spam = spam.probability if spam else 0.0

        directed = p_addressed >= self.settings.ai_gate_addressed_confidence
        spammy = p_spam >= self.settings.ai_gate_spam_threshold
        reply = (p_should >= self.settings.ai_gate_min_confidence or directed) and not spammy

        decision = GateDecision(
            reply=reply,
            should_reply=p_should,
            addressed=p_addressed,
            spam=p_spam,
            kind=kind.choice if kind and kind.choice else "other",
            kind_confidence=kind.confidence if kind else 0.0,
            probabilities=kind.probabilities if kind else {},
        )
        logger.info(
            "jev %s p=%.2f addressed=%.2f spam=%.2f kind=%s (%.2f) for %r",
            "YES" if reply else "NO",
            p_should,
            p_addressed,
            p_spam,
            decision.kind,
            decision.kind_confidence,
            text[:70],
        )
        return decision

    async def should_reply(
        self,
        *,
        text: str,
        channel_name: str = "a channel",
        user_name: str = "someone",
        recent: list[str] | None = None,
    ) -> bool:
        return (
            await self.decide(
                text=text,
                channel_name=channel_name,
                user_name=user_name,
                recent=recent,
            )
        ).reply


def _state(
    text: str,
    channel_name: str,
    user_name: str,
    recent: list[str] | None,
) -> str:
    lines = [f"channel: {channel_name}", f"speaker: {user_name}"]
    if recent:
        lines.append("recent messages:")
        lines.extend(f"- {line}" for line in recent[-4:])
    lines.append(f"latest message: {text.strip()}")
    return "\n".join(lines)


__all__ = [
    "Gate",
    "GateDecision",
    "QUESTIONS",
    "SHOULD_REPLY",
    "ADDRESSED",
    "SPAM",
    "_state",
]
