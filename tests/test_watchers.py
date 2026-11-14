from __future__ import annotations

from email.message import EmailMessage

from bot.features.watchers import _decode_header, _email_snippet


def test_email_watcher_decodes_headers_and_body():
    message = EmailMessage()
    message["Subject"] = "deploy failed"
    message["From"] = "CI <ci@example.com>"
    message.set_content("the build is red\n\nsee logs")
    assert _decode_header(message["Subject"]) == "deploy failed"
    assert "ci@example.com" in _decode_header(message["From"])
    assert _email_snippet(message) == "the build is red see logs"


def test_decode_header_handles_missing_and_encoded():
    assert _decode_header(None) == ""
    encoded = "=?utf-8?b?ZGVwbG95IGZhaWxlZA==?="
    assert _decode_header(encoded) == "deploy failed"
