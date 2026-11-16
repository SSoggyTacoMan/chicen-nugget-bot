import asyncio

from bot.features.followup import conversation_lines
from bot.slack_api import message_text, share_text

SHARED = {
    "user": "U_SSOGGY",
    "text": "explain why this is stupid",
    "attachments": [
        {
            "is_share": True,
            "author_name": "pj",
            "channel_name": "hq-engineering",
            "text": "could we have an alternative to hack.club without hack? like an alias like hack_af",
            "fallback": "could we have an alternative to hack.club without hack?",
        }
    ],
}


def test_share_text_reads_shared_message():
    quote = share_text(SHARED)
    assert quote == (
        "[quoted from pj in #hq-engineering] could we have an alternative to "
        "hack.club without hack? like an alias like hack_af"
    )


def test_share_text_ignores_plain_attachments():
    # A link preview or an image has no quoted message behind it.
    assert share_text(
        {
            "text": "look",
            "attachments": [
                {"image_url": "https://example.com/x.png", "fallback": "a picture"},
                {"title": "some link", "text": "unrelated preview"},
            ],
        }
    ) == ""


def test_share_text_reads_rich_text_quote():
    message = {
        "text": "explain why this is stupid",
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_quote",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [
                                    {"type": "user", "user_id": "U_PJ"},
                                    {"type": "text", "text": " blocked everything with "},
                                    {"type": "text", "text": "\"hack\" in it"},
                                ],
                            }
                        ],
                    },
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": "why?"}]},
                ],
            }
        ],
    }
    assert share_text(message) == '[quoted] <@U_PJ> blocked everything with "hack" in it'


def test_share_text_truncates_long_quotes():
    quote = share_text(
        {"attachments": [{"is_msg_unfurl": True, "text": "x" * 2000}]}
    )
    assert quote.startswith("[quoted] xxx")
    assert len(quote) <= len("[quoted] ") + 600


def test_message_text_keeps_own_text_and_quote():
    assert message_text(SHARED).startswith("explain why this is stupid\n\n[quoted from pj")
    assert message_text({"text": "just a message"}) == "just a message"
    # A share with no typed text still carries the quote.
    assert message_text({"attachments": SHARED["attachments"]}).startswith(
        "[quoted from pj"
    )


def test_conversation_lines_includes_quoted_text():
    class FakeClient:
        async def conversations_replies(self, **kwargs):
            return {
                "messages": [
                    {"user": "U_SSOGGY", "ts": "1.1", "text": "what do you think"},
                    {**SHARED, "ts": "1.2"},
                    {"user": "U_BOT", "ts": "1.3", "text": "the alias is fine"},
                ]
            }

    lines, humans, bot_last = asyncio.run(
        conversation_lines(
            FakeClient(),
            "C_HQ",
            thread_ts="1.1",
            agent_user_id="U_BOT",
        )
    )
    assert humans == 1
    assert bot_last is True
    assert any("[quoted from pj in #hq-engineering]" in line for line in lines)
    assert lines[0] == "<@U_SSOGGY>: what do you think"
