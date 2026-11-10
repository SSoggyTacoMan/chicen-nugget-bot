"""Thin client for TypeSafe's Jev (System One) endpoint.

Jev does not write text. It reads a `state` and a map of questions and returns
a typed answer per question in one parallel pass, each with a probability.
That makes it the right tool for "should the bot do X?" decisions: no prompt
parsing, no truncated answers, no invented options.

Hack Club AI serves it at `<AI_BASE_URL>/jev/systemone` with model `jev-latest`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from bot.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Answer:
    """One typed answer from Jev."""

    type: str
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def probability(self) -> float:
        """Best single confidence number for this answer."""
        if self.type == "noul" and self.noul is not None:
            return self.noul
        return self.confidence


def parse_answers(payload: dict[str, Any] | None) -> dict[str, Answer]:
    """Turn a raw Jev response into Answer objects, skipping anything unexpected."""
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("answers")
    if not isinstance(raw, dict):
        return {}

    answers: dict[str, Answer] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        probabilities = {
            str(key): float(value)
            for key, value in (item.get("probabilities") or {}).items()
            if isinstance(value, (int, float))
        }
        if kind == "noul":
            value = item.get("noul")
            if not isinstance(value, (int, float)):
                continue
            answers[name] = Answer(
                type="noul",
                noul=float(value),
                confidence=float(value),
                probabilities=probabilities,
            )
        elif kind == "choice":
            chosen = item.get("choice")
            if not isinstance(chosen, str) or not chosen:
                continue
            answers[name] = Answer(
                type="choice",
                choice=chosen,
                confidence=float(item.get("confidence") or 0.0),
                probabilities=probabilities,
            )
        elif kind == "score":
            value = item.get("score")
            if not isinstance(value, (int, float)):
                continue
            answers[name] = Answer(
                type="score",
                score=float(value),
                confidence=float(item.get("confidence") or 0.0),
                probabilities=probabilities,
            )
    return answers


class Jev:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def url(self) -> str:
        if self.settings.ai_jev_url:
            return self.settings.ai_jev_url.rstrip("/")
        return f"{self.settings.ai_base_url}/jev/systemone"

    async def ask(
        self,
        *,
        state: str,
        questions: dict[str, dict[str, Any]],
        timeout: float | None = None,
    ) -> dict[str, Answer] | None:
        """Ask Jev every question at once. Returns None on any failure."""
        body = {
            "model": self.settings.ai_gate_model,
            "state": state,
            "questions": questions,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.ai_api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout or self.settings.ai_gate_timeout) as client:
                response = await client.post(self.url, headers=headers, json=body)
            if response.status_code != 200:
                logger.warning(
                    "jev returned %s: %s", response.status_code, response.text[:200]
                )
                return None
            return parse_answers(response.json())
        except Exception as exc:  # noqa: BLE001 - callers fail closed
            logger.warning("jev request failed: %s", exc)
            return None


__all__ = ["Answer", "Jev", "parse_answers"]
