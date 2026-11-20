"""The Jev gate: typed decisions about a message before spending an agent turn.

Jev answers several questions about the same state in one pass:

- should_reply: probability that a reply would genuinely add something
- addressed:    probability that the message is aimed at the bot
- spam:         probability that it is spam, a link dump, or not worth engaging
- kind:         what sort of message it is (question, help, celebration, chatter…)

The bot only runs the expensive agent turn when should_reply clears its
threshold (or the message is clearly for the bot) and it doesn't look like
spam. Everything fails closed: a broken gate means no unprompted replies.

Inspired by noodle's unprompted-reply gate
(https://github.com/MatthiasLubbertsen/noodle).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from bot.ai.budget import Budget
from bot.ai.jev import Jev, JevResult
from bot.config import Settings

logger = logging.getLogger(__name__)

SHOULD_REPLY = """\
Would a short reply from this bot genuinely add something to the conversation?

True when someone:
- asks a question, especially about programming, code, tools, servers, APIs or bots
- explicitly asks for help, a review, feedback, a link, an opinion, or a recommendation
- seems stuck, confused, or blocked and is asking for a way forward
- is talking to or about the bot, or replying to something it said
- shares a win, a project, or something they clearly want acknowledged

False when:
- it is chatter between other people that doesn't involve the bot
- the speaker is narrating their own day, progress, mood, hours, or plans without asking the bot for anything
- they are venting or thinking out loud, even if the topic is code or projects
- it is a joke, meme, reaction, or one-word message that needs no reply
- it is spam, a link dump, a mass ping, or low-effort noise
- a reply would just repeat what someone already said or ask a question they did not ask
"""

ADDRESSED = """\
Is this message aimed at the bot? True when it addresses the bot by name
("nugget", "chicen"), thanks it, asks it something directly, or replies to it.
False when it is a general message to the channel or a conversation between
other people.
"""

RECIPIENT_INSTRUCTIONS = """\
Who is the speaker talking to in the latest message?
"""

RECIPIENT_CRITERIA = {
    "bot": "The bot itself: asking it, thanking it, correcting it, or replying directly to it. Note: in threads with multiple humans, mark 'someone_else' unless clearly addressing the bot.",
    "someone_else": "Another person in the conversation, even if the bot is in the thread.",
    "everyone": "The whole channel or nobody in particular.",
}

NEEDS_TOOLS = """\
Would a good answer need live data, a lookup, or an action (web search, weather,
Slack history, code execution, a file, a chart, a reminder) rather than general
knowledge? True for anything time-sensitive, personal, or workspace-specific.
"""

SPAM = """\
Is this message spam, a scam, a mass ping, a bare link dump, or something a
community bot should not engage with? False for normal conversation, jokes,
questions, and people sharing their work.
"""

SAFETY = """\
Would this be appropriate in an all-ages Hack Club Slack?
True for normal conversation, jokes, questions, code, projects, respectful
disagreement, and someone asking for help with or reporting harassment.
Judge the intent, not the topic: describing or reporting a problem is not the
same as causing it.
False when it contains harassment, bullying, slurs, hate, threats,
discrimination, sexual content, unwelcome romantic or sexual advances, someone
else's private personal information, spam, ads, fraud, or an attempt to
escalate a conflict or rally people against someone.
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
    "safe": {"type": "noul", "instructions": SAFETY},
    "needs_tools": {"type": "noul", "instructions": NEEDS_TOOLS},
    "kind": {
        "type": "choice",
        "instructions": KIND_INSTRUCTIONS,
        "criteria": KIND_CRITERIA,
    },
    "recipient": {
        "type": "choice",
        "instructions": RECIPIENT_INSTRUCTIONS,
        "criteria": RECIPIENT_CRITERIA,
    },
}


@dataclass(frozen=True)
class GateDecision:
    """What Jev thinks about one message."""

    reply: bool
    should_reply: float = 0.0
    addressed: float = 0.0
    spam: float = 0.0
    safe: float = 1.0
    needs_tools: float = 0.0
    kind: str = "other"
    kind_confidence: float = 0.0
    recipient: str = "everyone"
    recipient_bot: float = 0.0
    bot_last: bool = False
    probabilities: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    def hint(self) -> str:
        """A short line the agent can use to pitch its reply."""
        bits: list[str] = []
        if self.kind in {"question", "help", "feedback", "celebration", "bot"}:
            bits.append(f"jev reads it as a {self.kind} message ({self.kind_confidence:.0%} confident)")
        if self.recipient == "someone_else" and self.recipient_bot < 0.5:
            bits.append("it looks aimed at another person, not you")
        elif self.recipient == "bot":
            bits.append("it looks aimed at you")
        if self.needs_tools >= 0.6:
            bits.append("a correct answer needs live data or a tool call, not memory")
        if self.bot_last:
            bits.append("you spoke last in this conversation, so only reply if someone is clearly asking you")
        return "; ".join(bits)


class Gate:
    def __init__(
        self,
        settings: Settings,
        jev: Jev | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.settings = settings
        self.jev = jev or Jev(settings)
        self.budget = budget

    async def decide(
        self,
        *,
        text: str,
        channel_name: str = "a channel",
        user_name: str = "someone",
        user_id: str | None = None,
        recent: list[str] | None = None,
        bot_last: bool = False,
        require_addressed: bool = False,
    ) -> GateDecision:
        if not self.settings.ai_gate_enabled or not text.strip():
            return GateDecision(reply=False, error="gate disabled")
        if self.budget is not None:
            if self.budget.exhausted():
                return GateDecision(reply=False, error="daily ai budget exhausted")
            if user_id and self.budget.user_exhausted(user_id):
                return GateDecision(reply=False, error="daily ai limit reached for this user")

        state = _state(text, channel_name, user_name, recent, bot_last=bot_last)
        result: JevResult | None = None
        for attempt in (1, 2):
            try:
                async with asyncio.timeout(self.settings.ai_gate_timeout + 1):
                    result = await self.jev.ask(state=state, questions=QUESTIONS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("jev gate attempt %s failed: %s", attempt, exc)
                result = None
            if result is not None:
                break
            if attempt == 1:
                await asyncio.sleep(0.3)

        if result is not None and self.budget is not None:
            self.budget.record(
                kind="jev",
                model=result.model or self.settings.ai_gate_model,
                user_id=user_id or "",
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )

        answers = result.answers if result is not None else None
        if not answers:
            return GateDecision(reply=False, error="jev unavailable")

        should = answers.get("should_reply")
        addressed = answers.get("addressed")
        spam = answers.get("spam")
        safe = answers.get("safe")
        needs_tools = answers.get("needs_tools")
        kind = answers.get("kind")
        recipient = answers.get("recipient")
        if should is None:
            return GateDecision(reply=False, error="jev returned no should_reply answer")

        p_should = should.probability
        p_addressed = addressed.probability if addressed else 0.0
        p_spam = spam.probability if spam else 0.0
        # Older/partial responses without a safety answer are treated as safe;
        # the question is new, but a missing answer should not silence the bot.
        p_safe = safe.probability if safe else 1.0
        p_needs_tools = needs_tools.probability if needs_tools else 0.0
        recipient_choice = recipient.choice if recipient and recipient.choice else "everyone"
        kind_choice = kind.choice if kind and kind.choice else "other"
        p_recipient_bot = (recipient.probabilities.get("bot", 0.0) if recipient else 0.0)

        directed = p_addressed >= self.settings.ai_gate_addressed_confidence
        aimed = directed or p_recipient_bot >= self.settings.ai_gate_addressed_confidence
        spammy = p_spam >= self.settings.ai_gate_spam_threshold
        unsafe = p_safe < _safety_threshold(self.settings)
        helpful = p_should >= self.settings.ai_gate_min_confidence
        # Ambient messages must be aimed at the bot (or answer a bot turn).
        # Thread follow-ups can still be helpful without a direct mention.
        if require_addressed:
            assist = kind_choice in {"help", "feedback"} or (
                kind_choice == "question" and p_needs_tools >= 0.6
            )
            reply = not spammy and not unsafe and (
                aimed
                or (
                    assist
                    and helpful
                    and p_should >= 0.7
                    and recipient_choice != "someone_else"
                )
            )
        else:
            # Never spend an agent turn on a message aimed at someone else unless the
            # speaker is clearly directing it at the bot too.
            reply = not spammy and not unsafe and (
                aimed or (helpful and recipient_choice != "someone_else")
            )

        decision = GateDecision(
            reply=reply,
            should_reply=p_should,
            addressed=p_addressed,
            spam=p_spam,
            safe=p_safe,
            needs_tools=p_needs_tools,
            kind=kind_choice,
            kind_confidence=kind.confidence if kind else 0.0,
            recipient=recipient_choice,
            recipient_bot=p_recipient_bot,
            bot_last=bot_last,
            probabilities=kind.probabilities if kind else {},
        )
        logger.info(
            "jev %s p=%.2f addressed=%.2f aimed_at=%s spam=%.2f safe=%.2f needs_tools=%.2f kind=%s (%.2f) for %r",
            "YES" if reply else "NO",
            p_should,
            p_addressed,
            recipient_choice,
            p_spam,
            p_safe,
            p_needs_tools,
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

    async def review_public(self, text: str, *, user_id: str = "") -> bool:
        """True when the bot's own text is safe to post in a public channel.

        This is the output half of the conduct check: the gate screens incoming
        ambient messages, but wrapped mentions and DMs can still produce a reply
        that should not be broadcast. A confident "not appropriate" suppresses
        the message. If Jev is unavailable we let it through: the provider and
        the prompt are still there, and going silent on every classifier hiccup
        would break normal mentions.
        """
        threshold = _safety_threshold(self.settings)
        if not text.strip() or threshold <= 0:
            return True
        if not getattr(self.settings, "ai_gate_enabled", True):
            # The screen rides on the classifier; if an operator turns the
            # classifier off entirely, don't add a timeout to every reply.
            return True
        result: JevResult | None = None
        try:
            async with asyncio.timeout(self.settings.ai_gate_timeout + 1):
                result = await self.jev.ask(
                    state=_state(text, "a public channel", "the bot", None),
                    questions={"safe": QUESTIONS["safe"]},
                )
        except Exception as exc:  # noqa: BLE001 - allow on transport failure
            logger.warning("safety review failed: %s", exc)
            return True
        if result is None:
            return True
        if self.budget is not None:
            self.budget.record(
                kind="jev",
                model=result.model or self.settings.ai_gate_model,
                user_id=user_id,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )
        answer = result.answers.get("safe")
        if answer is None:
            return True
        safe = answer.probability >= threshold
        if not safe:
            logger.warning("safety review blocked a public reply (p=%.2f)", answer.probability)
        return safe


def _safety_threshold(settings: Any) -> float:
    try:
        return float(getattr(settings, "ai_gate_safety_threshold", 0.5))
    except (TypeError, ValueError):
        return 0.5


def _state(
    text: str,
    channel_name: str,
    user_name: str,
    recent: list[str] | None,
    bot_last: bool = False,
) -> str:
    lines = [f"channel: {channel_name}", f"speaker: {user_name}"]
    lines.append(f"the bot spoke last: {'yes' if bot_last else 'no'}")
    if recent:
        lines.append("recent messages:")
        lines.extend(f"- {line}" for line in recent[-6:])
    lines.append(f"latest message: {text.strip()}")
    return "\n".join(lines)


__all__ = [
    "ADDRESSED",
    "NEEDS_TOOLS",
    "QUESTIONS",
    "RECIPIENT_CRITERIA",
    "RECIPIENT_INSTRUCTIONS",
    "SAFETY",
    "SHOULD_REPLY",
    "SPAM",
    "Gate",
    "GateDecision",
    "_state",
]
