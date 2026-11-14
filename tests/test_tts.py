import asyncio
from types import SimpleNamespace

import pytest

from bot.ai import tts


def _settings(**overrides):
    values = {
        "ai_api_key": "test-key",
        "ai_base_url": "https://ai.example/proxy/v1",
        "tts_model": "qwen/qwen3-tts",
        "tts_voice": "Serena",
        "tts_language": "English",
        "tts_style": None,
        "tts_max_chars": 500,
        "tts_cost_per_1k_chars": 0.02,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Response:
    def __init__(
        self,
        status_code=200,
        payload=None,
        content=b"",
        headers=None,
        text="",
    ):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    def __init__(self, prediction=None, audio=None):
        self.prediction = prediction or {}
        self.audio = audio or _Response(
            200, content=b"RIFFdata", headers={"content-type": "audio/wav"}
        )
        self.post_url = ""
        self.post_headers = {}
        self.post_json = {}
        self.get_url = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.post_url = url
        self.post_headers = headers or {}
        self.post_json = json or {}
        return self.prediction

    async def get(self, url):
        self.get_url = url
        return self.audio


def test_clip_text_keeps_short_text_and_trims_long_text():
    assert tts.clip_text("  hello   world  ", 50) == "hello world"
    clipped = tts.clip_text("one two three four five six seven", 20)
    assert len(clipped) <= 21
    assert clipped.endswith("…")
    assert "seven" not in clipped


def test_synthesize_posts_qwen_payload_and_downloads_audio(monkeypatch):
    prediction = _Response(
        201,
        payload={
            "status": "succeeded",
            "output": "https://replicate.delivery/x/output.wav",
        },
    )
    client = _Client(prediction=prediction)
    monkeypatch.setattr(tts.httpx, "AsyncClient", lambda **kwargs: client)

    audio, mime, extension = asyncio.run(
        tts.synthesize(
            "hello from the nugget",
            _settings(),
            voice="Aiden",
            language="French",
            style="speak slowly",
        )
    )

    assert audio == b"RIFFdata"
    assert mime == "audio/wav"
    assert extension == "wav"
    assert client.post_url.endswith(
        "/replicate/models/qwen/qwen3-tts/predictions"
    )
    assert client.post_headers["Prefer"] == "wait"
    assert client.post_json == {
        "input": {
            "mode": "custom_voice",
            "text": "hello from the nugget",
            "speaker": "Aiden",
            "language": "French",
            "style_instruction": "speak slowly",
        }
    }
    assert client.get_url == "https://replicate.delivery/x/output.wav"


def test_synthesize_rejects_empty_text_and_missing_key():
    with pytest.raises(tts.TTSError) as empty:
        asyncio.run(tts.synthesize("   ", _settings()))
    assert empty.value.status == 400

    with pytest.raises(tts.TTSError) as keyless:
        asyncio.run(tts.synthesize("hello", _settings(ai_api_key=None)))
    assert keyless.value.status == 401


def test_synthesize_surfaces_provider_error(monkeypatch):
    client = _Client(
        prediction=_Response(502, payload={}, text="upstream fell over")
    )
    monkeypatch.setattr(tts.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(tts.TTSError) as exc:
        asyncio.run(tts.synthesize("hello", _settings()))
    assert exc.value.status == 502
    assert "upstream" in str(exc.value)


def test_synthesize_falls_back_for_unknown_voice_and_language(monkeypatch):
    prediction = _Response(
        200,
        payload={"status": "succeeded", "output": "https://example.com/out.wav"},
    )
    client = _Client(prediction=prediction)
    monkeypatch.setattr(tts.httpx, "AsyncClient", lambda **kwargs: client)

    asyncio.run(
        tts.synthesize("hello", _settings(), voice="Nope", language="Klingon")
    )
    assert client.post_json["input"]["speaker"] == "Serena"
    assert client.post_json["input"]["language"] == "auto"


def test_voice_config_prefers_db_over_env(tmp_path):
    from bot.db import Database

    db = Database(tmp_path / "tts.db")
    db.init()
    settings = _settings(tts_voice="Serena", tts_language="English", tts_max_chars=500)

    base = tts.voice_config(settings, db)
    assert base.model == "qwen/qwen3-tts"
    assert base.max_chars == 500

    db.kv_set("config:tts_model", "qwen/qwen3-tts-fast")
    db.kv_set("config:tts_voice", "Aiden")
    db.kv_set("config:tts_language", "French")
    db.kv_set("config:tts_style", "speak slowly")
    db.kv_set("config:tts_max_chars", "20")

    cfg = tts.voice_config(settings, db)
    assert cfg.model == "qwen/qwen3-tts-fast"
    assert cfg.voice == "Aiden"
    assert cfg.language == "French"
    assert cfg.style == "speak slowly"
    assert cfg.max_chars == 50
