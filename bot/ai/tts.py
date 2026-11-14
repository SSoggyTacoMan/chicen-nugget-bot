"""Text-to-speech through Hack Club AI's Replicate proxy.

Qwen3-TTS supports preset voices, voice design and voice cloning. The bot uses
the preset voices for /nugget speak and the speak tool, so a request is just
text plus a speaker and language.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from bot.config import Settings
from bot.db import Database

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen/qwen3-tts"

VOICES: tuple[tuple[str, str], ...] = (
    ("Aiden", "Aiden - English male (default)"),
    ("Ryan", "Ryan - English male"),
    ("Serena", "Serena - Chinese female"),
    ("Vivian", "Vivian - Chinese female"),
    ("Dylan", "Dylan - Chinese male (Beijing)"),
    ("Eric", "Eric - Chinese male (Sichuan)"),
    ("Uncle_fu", "Uncle Fu - Chinese male"),
    ("Ono_anna", "Ono Anna - Japanese female"),
    ("Sohee", "Sohee - Korean female"),
)

LANGUAGES: tuple[tuple[str, str], ...] = (
    ("auto", "auto detect"),
    ("English", "English"),
    ("Chinese", "Chinese"),
    ("Japanese", "Japanese"),
    ("Korean", "Korean"),
    ("French", "French"),
    ("German", "German"),
    ("Italian", "Italian"),
    ("Spanish", "Spanish"),
    ("Portuguese", "Portuguese"),
    ("Russian", "Russian"),
)

_VOICE_VALUES = {value for value, _ in VOICES}
_LANGUAGE_VALUES = {value for value, _ in LANGUAGES}


@dataclass(frozen=True)
class VoiceConfig:
    model: str
    voice: str
    language: str
    style: str
    max_chars: int
    cost_per_1k_chars: float


def voice_config(settings: Settings, db: Database | None = None) -> VoiceConfig:
    """Effective TTS settings, with SQLite overrides taking priority over env."""

    def raw(key: str) -> str | None:
        value = db.kv_get(f"config:{key}") if db else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def setting_text(name: str, default: str) -> str:
        value = getattr(settings, name, None)
        return value.strip() if isinstance(value, str) and value.strip() else default

    def number_int(key: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(raw(key) or default)))
        except (TypeError, ValueError):
            return default

    def number_float(key: str, default: float) -> float:
        try:
            return max(0.0, float(raw(key) or default))
        except (TypeError, ValueError):
            return default

    model = raw("tts_model") or setting_text("tts_model", DEFAULT_MODEL)
    voice = raw("tts_voice") or setting_text("tts_voice", "Aiden")
    if voice not in _VOICE_VALUES:
        voice = "Aiden"
    language = raw("tts_language") or setting_text("tts_language", "English")
    if language not in _LANGUAGE_VALUES:
        language = "auto"
    configured_chars = getattr(settings, "tts_max_chars", 500)
    if not isinstance(configured_chars, int) or isinstance(configured_chars, bool):
        configured_chars = 500
    configured_cost = getattr(settings, "tts_cost_per_1k_chars", 0.02)
    if not isinstance(configured_cost, (int, float)) or isinstance(
        configured_cost, bool
    ):
        configured_cost = 0.02
    return VoiceConfig(
        model=model.strip(),
        voice=voice,
        language=language,
        style=raw("tts_style") or setting_text("tts_style", ""),
        max_chars=number_int("tts_max_chars", configured_chars, 50, 1500),
        cost_per_1k_chars=number_float("tts_cost_per_1k_chars", configured_cost),
    )


class TTSError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def clip_text(text: str, limit: int) -> str:
    """Trim at a word boundary so the model never gets a 10 minute paragraph."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def _extension(content_type: str, url: str) -> str:
    mime = (content_type or "").lower().split(";")[0].strip()
    known = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/wave": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mp4": "m4a",
        "audio/x-m4a": "m4a",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "audio/flac": "flac",
    }
    if mime in known:
        return known[mime]
    suffix = PurePosixPath(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in {"wav", "mp3", "m4a", "ogg", "webm", "flac"}:
        return suffix
    return "wav"


def _audio_url(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, list):
        output = output[0] if output else None
    if isinstance(output, str) and output.startswith(("http://", "https://")):
        return output
    return ""


async def synthesize(
    text: str,
    settings: Settings,
    *,
    voice: str | None = None,
    language: str | None = None,
    style: str | None = None,
    db: Database | None = None,
) -> tuple[bytes, str, str]:
    """Return ``(audio_bytes, content_type, extension)`` for one voice clip."""
    cfg = voice_config(settings, db)
    clean = clip_text(text, cfg.max_chars)
    if not clean:
        raise TTSError(400, "nothing to say")
    if not settings.ai_api_key:
        raise TTSError(401, "AI_API_KEY is not configured")
    model = cfg.model
    if not model:
        raise TTSError(400, "TTS_MODEL is empty")

    speaker = (voice or cfg.voice).strip()
    if speaker not in _VOICE_VALUES:
        speaker = "Aiden"
    lang = (language or cfg.language).strip()
    if lang not in _LANGUAGE_VALUES:
        lang = "auto"
    style_text = (style if style is not None else cfg.style).strip()

    body: dict[str, Any] = {
        "mode": "custom_voice",
        "text": clean,
        "speaker": speaker,
        "language": lang,
    }
    if style_text:
        body["style_instruction"] = style_text

    url = f"{settings.ai_base_url}/replicate/models/{model}/predictions"
    headers = {
        "Authorization": f"Bearer {settings.ai_api_key}",
        "Content-Type": "application/json",
        "Prefer": "wait",
    }

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
        try:
            response = await http.post(url, headers=headers, json={"input": body})
        except httpx.HTTPError as exc:
            raise TTSError(0, f"tts request failed: {exc}") from exc
        if response.status_code not in {200, 201}:
            detail = response.text[:400]
            raise TTSError(response.status_code, detail or "tts request failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TTSError(502, "tts returned invalid json") from exc

        status = str(payload.get("status") or "")
        if status and status != "succeeded":
            error = payload.get("error") or payload.get("detail") or status
            raise TTSError(502, str(error)[:400])

        audio_url = _audio_url(payload)
        if not audio_url:
            raise TTSError(502, "tts returned no audio url")

        try:
            audio = await http.get(audio_url)
        except httpx.HTTPError as exc:
            raise TTSError(0, f"audio download failed: {exc}") from exc
        if audio.status_code != 200 or not audio.content:
            raise TTSError(audio.status_code, "audio download failed")

        content_type = audio.headers.get("content-type") or "audio/wav"
        extension = _extension(content_type, audio_url)
        logger.info(
            "tts: model=%s voice=%s lang=%s chars=%s bytes=%s",
            model,
            speaker,
            lang,
            len(clean),
            len(audio.content),
        )
        return audio.content, content_type, extension


__all__ = [
    "DEFAULT_MODEL",
    "LANGUAGES",
    "VOICES",
    "TTSError",
    "VoiceConfig",
    "clip_text",
    "synthesize",
    "voice_config",
]
