"""Semantic memory: embed stored turns and search them by meaning."""

from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI

from bot.config import Settings
from bot.db import Database

logger = logging.getLogger(__name__)


class MemoryIndex:
    def __init__(self, settings: Settings, llm: AsyncOpenAI | None = None) -> None:
        self.settings = settings
        raw_model = getattr(settings, "ai_embedding_model", None)
        self.model = raw_model.strip() if isinstance(raw_model, str) else ""
        self.llm = llm or AsyncOpenAI(
            api_key=settings.ai_api_key or "missing",
            base_url=settings.ai_base_url,
            timeout=30.0,
            max_retries=1,
        )

    @property
    def enabled(self) -> bool:
        key = getattr(self.settings, "ai_api_key", None)
        return bool(self.model and isinstance(key, str) and key)

    async def embed(self, text: str) -> list[float] | None:
        if not self.enabled:
            return None
        value = (text or "").strip()[:8000]
        if not value:
            return None
        try:
            response = await self.llm.embeddings.create(
                model=self.model, input=value
            )
        except Exception:
            logger.debug("embedding request failed", exc_info=True)
            return None
        if not response.data:
            return None
        return list(response.data[0].embedding or [])

    async def remember(
        self,
        db: Database,
        *,
        message_id: int,
        conversation: str,
        role: str,
        user_id: str | None,
        text: str,
    ) -> None:
        if not self.enabled or not message_id:
            return
        vector = await self.embed(text)
        if vector:
            db.add_memory_vector(
                message_id,
                conversation,
                role,
                user_id,
                text,
                self.model,
                vector,
            )

    async def search(
        self, db: Database, query: str, limit: int = 8
    ) -> list[dict]:
        vector = await self.embed(query)
        if not vector:
            return []
        return db.search_memory_vectors(self.model, vector, limit)

    async def backfill(self, db: Database, limit: int = 500) -> int:
        if not self.enabled:
            return 0
        rows = db.messages_without_vectors(self.model, limit)
        gate = asyncio.Semaphore(3)

        async def index_row(row: dict) -> int:
            async with gate:
                vector = await self.embed(row.get("text") or "")
            if not vector:
                return 0
            db.add_memory_vector(
                int(row["id"]),
                str(row["conversation"]),
                str(row["role"]),
                row.get("user_id"),
                str(row["text"]),
                self.model,
                vector,
            )
            return 1

        results = await asyncio.gather(
            *(index_row(row) for row in reversed(rows)), return_exceptions=True
        )
        return sum(1 for result in results if result == 1)
