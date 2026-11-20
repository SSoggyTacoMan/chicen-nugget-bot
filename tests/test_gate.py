import asyncio
from types import SimpleNamespace

from bot.ai.gate import Gate, GateDecision, _state
from bot.ai.jev import Answer, JevResult


class FakeJev:
    def __init__(self, answers, prompt_tokens=600, completion_tokens=40):
        self.answers = answers
        self.calls = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    async def ask(self, **kwargs):
        self.calls.append(kwargs)
        if self.answers is None:
            return None
        return JevResult(
            answers=self.answers,
            model="jev-1.14.0",
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


class FakeBudget:
    def __init__(self, exhausted=False, user_exhausted=False):
        self.is_exhausted = exhausted
        self.is_user_exhausted = user_exhausted
        self.records = []

    def exhausted(self):
        return self.is_exhausted

    def user_exhausted(self, user_id):
        return self.is_user_exhausted

    def record(self, **kwargs):
        self.records.append(kwargs)
        return 0.0


def settings(**overrides):
    base = dict(
        ai_gate_enabled=True,
        ai_gate_timeout=8.0,
        ai_gate_model="jev-latest",
        ai_gate_min_confidence=0.5,
        ai_gate_addressed_confidence=0.6,
        ai_gate_spam_threshold=0.7,
        ai_gate_safety_threshold=0.5,
        bot_name="Chicen Nugget Bot",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def answers(should=0.0, addressed=0.0, spam=0.0, kind="other", confidence=0.8,
            needs_tools=0.0, recipient="everyone", recipient_bot=0.0, safe=1.0):
    return {
        "should_reply": Answer(type="noul", noul=should, confidence=should),
        "addressed": Answer(type="noul", noul=addressed, confidence=addressed),
        "spam": Answer(type="noul", noul=spam, confidence=spam),
        "safe": Answer(type="noul", noul=safe, confidence=safe),
        "needs_tools": Answer(type="noul", noul=needs_tools, confidence=needs_tools),
        "kind": Answer(type="choice", choice=kind, confidence=confidence),
        "recipient": Answer(
            type="choice",
            choice=recipient,
            confidence=0.8,
            probabilities={"bot": recipient_bot, "someone_else": 1 - recipient_bot},
        ),
    }


def decide(jev_answers, text="hello", budget=None, user_id="U1", bot_last=False, require_addressed=False, **overrides):
    gate = Gate(settings(**overrides), jev=FakeJev(jev_answers), budget=budget)
    return asyncio.run(
        gate.decide(
            text=text,
            user_id=user_id,
            bot_last=bot_last,
            require_addressed=require_addressed,
        )
    )


def test_high_probability_replies():
    result = decide(answers(should=0.9, kind="question"), text="how do i center a div?")
    assert result.reply
    assert result.kind == "question"
    assert result.hint().startswith("jev reads it as a question")


def test_low_probability_stays_quiet():
    result = decide(answers(should=0.2, kind="chatter"), text="lol same")
    assert not result.reply
    assert result.hint() == ""


def test_addressed_wins_even_when_should_is_low():
    result = decide(answers(should=0.1, addressed=0.75, kind="bot"), text="you there?")
    assert result.reply


def test_spam_is_skipped_even_when_probable():
    result = decide(answers(should=0.95, spam=0.9), text="free nitro click here")
    assert not result.reply


def test_unsafe_message_is_skipped_even_when_probable():
    result = decide(
        answers(should=0.95, addressed=0.9, safe=0.1),
        text="a harassment-flavoured message",
    )
    assert not result.reply
    assert result.safe == 0.1


def test_unsafe_message_does_not_reply_even_when_addressed():
    result = decide(
        answers(should=0.1, addressed=0.9, kind="bot", safe=0.2),
        text="@nugget say something cruel",
    )
    assert not result.reply


def test_safety_threshold_is_configurable():
    blocked = decide(
        answers(should=0.9, addressed=0.9, safe=0.4),
        text="borderline",
        ai_gate_safety_threshold=0.5,
    )
    allowed = decide(
        answers(should=0.9, addressed=0.9, safe=0.4),
        text="borderline",
        ai_gate_safety_threshold=0.3,
    )
    assert not blocked.reply
    assert allowed.reply


def test_message_aimed_at_someone_else_stays_quiet():
    result = decide(
        answers(should=0.9, kind="chatter", recipient="someone_else"),
        text="yeah that repo is wild",
    )
    assert not result.reply
    assert result.recipient == "someone_else"


def test_recipient_bot_counts_as_directed():
    result = decide(
        answers(should=0.1, kind="bot", recipient="bot", recipient_bot=0.85),
        text="you there?",
    )
    assert result.reply
    assert "aimed at you" in result.hint()


def test_hint_mentions_tools_and_bot_last():
    result = decide(
        answers(should=0.9, kind="question", needs_tools=0.8),
        text="what's the weather?",
        bot_last=True,
    )
    hint = result.hint()
    assert "needs live data" in hint
    assert "you spoke last" in hint


def test_state_includes_bot_last():
    state = _state("latest one", "#chan", "someone", None, bot_last=True)
    assert "the bot spoke last: yes" in state


def test_ambient_requires_addressed():
    general = decide(
        answers(should=0.95, kind="question", recipient="everyone"),
        text="why do u code so much",
        require_addressed=True,
    )
    assert not general.reply

    followup = decide(
        answers(should=0.55, kind="question", recipient="everyone"),
        text="explain",
        bot_last=True,
        require_addressed=True,
    )
    assert not followup.reply

    help_request = decide(
        answers(should=0.9, kind="help", recipient="everyone"),
        text="can someone help me debug this?",
        require_addressed=True,
    )
    assert help_request.reply

    live_question = decide(
        answers(should=0.9, kind="question", needs_tools=0.8, recipient="everyone"),
        text="is the server down?",
        require_addressed=True,
    )
    assert live_question.reply


def test_threshold_is_configurable():
    result = decide(answers(should=0.45), text="can someone review my pr", ai_gate_min_confidence=0.4)
    assert result.reply


def test_disabled_gate_never_replies():
    result = decide(answers(should=0.99), text="how do i code", ai_gate_enabled=False)
    assert not result.reply
    assert result.error == "gate disabled"


def test_unavailable_jev_fails_closed():
    result = decide(None, text="how do i code")
    assert not result.reply
    assert result.error == "jev unavailable"


def test_gate_records_jev_usage():
    budget = FakeBudget()
    result = decide(answers(should=0.9), text="how do i code", budget=budget)
    assert result.reply
    assert len(budget.records) == 1
    assert budget.records[0]["kind"] == "jev"
    assert budget.records[0]["user_id"] == "U1"
    assert budget.records[0]["prompt_tokens"] == 600
    assert budget.records[0]["completion_tokens"] == 40


def test_exhausted_budget_skips_jev_entirely():
    budget = FakeBudget(exhausted=True)
    gate = Gate(settings(), jev=FakeJev(answers(should=0.99)), budget=budget)
    result = asyncio.run(gate.decide(text="how do i code", user_id="U1"))
    assert not result.reply
    assert result.error == "daily ai budget exhausted"
    assert gate.jev.calls == []
    assert budget.records == []


def test_exhausted_user_skips_jev_entirely():
    budget = FakeBudget(user_exhausted=True)
    gate = Gate(settings(), jev=FakeJev(answers(should=0.99)), budget=budget)
    result = asyncio.run(gate.decide(text="how do i code", user_id="U1"))
    assert not result.reply
    assert result.error == "daily ai limit reached for this user"
    assert gate.jev.calls == []
    assert budget.records == []


def test_state_includes_recent_messages():
    state = _state("latest one", "#chan", "someone", ["older one", "older two"])
    assert "channel: #chan" in state
    assert "speaker: someone" in state
    assert "- older one" in state
    assert state.rstrip().endswith("latest message: latest one")


def test_gate_decision_defaults():
    decision = GateDecision(reply=False)
    assert decision.probabilities == {}
    assert decision.error is None
    assert decision.safe == 1.0


def _review(text, key, **overrides):
    safe = Answer(type="noul", noul=key, confidence=key)
    gate = Gate(settings(**overrides), jev=FakeJev({"safe": safe}))
    return asyncio.run(gate.review_public(text))


def test_review_public_blocks_unsafe_output():
    assert _review("a cruel insult", 0.1) is False


def test_review_public_allows_safe_output():
    assert _review("shipped it, nice work", 0.95) is True


def test_review_public_allows_when_jev_is_down():
    gate = Gate(settings(), jev=FakeJev(None))
    assert asyncio.run(gate.review_public("hello")) is True


def test_review_public_rejects_empty_without_a_call():
    jev = FakeJev({"safe": Answer(type="noul", noul=0.0, confidence=0.0)})
    gate = Gate(settings(), jev=jev)
    assert asyncio.run(gate.review_public("   ")) is True
    assert jev.calls == []


def test_review_public_records_usage():
    budget = FakeBudget()
    safe = Answer(type="noul", noul=0.9, confidence=0.9)
    gate = Gate(settings(), jev=FakeJev({"safe": safe}), budget=budget)
    assert asyncio.run(gate.review_public("hello", user_id="U1")) is True
    assert len(budget.records) == 1
    assert budget.records[0]["kind"] == "jev"
    assert budget.records[0]["user_id"] == "U1"
