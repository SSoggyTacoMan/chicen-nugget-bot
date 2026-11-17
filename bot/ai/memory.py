"""Semantic memory: embed stored turns and search them by meaning.

Approach inspired by noodle (https://github.com/MatthiasLubbertsen/noodle).
"""

from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI

from bot.config import Settings
from bot.db import Database

logger = logging.getLogger(__name__)

# Short acknowledgements ("ok", "thanks", "lol") are not worth an embedding
# round-trip; they dominate chat volume without adding retrievable meaning.
MIN_EMBED_CHARS = 8


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

    async def embed_many(
        self,
        texts: list[str],
        *,
        batch_size: int = 32,
        concurrency: int = 3,
    ) -> list[list[float] | None]:
        """Embed many strings, preserving order.

        Long-thread retrieval embeds every reply once; doing that one request
        per message would be slow and rude, so this batches and runs a few
        batches at a time. A failed batch returns ``None`` for its items
        instead of taking the whole search down.
        """
        if not self.enabled or not texts:
            return [None] * len(texts)
        prepared = [((text or "").strip() or "(empty)")[:4000] for text in texts]
        batches = [
            (start, prepared[start : start + batch_size])
            for start in range(0, len(prepared), batch_size)
        ]
        semaphore = asyncio.Semaphore(max(1, concurrency))
        vectors: list[list[float] | None] = [None] * len(prepared)

        async def one(start: int, batch: list[str]) -> None:
            async with semaphore:
                try:
                    response = await self.llm.embeddings.create(
                        model=self.model, input=batch
                    )
                except Exception:
                    logger.debug("batch embedding failed", exc_info=True)
                    return
                for item in response.data or []:
                    index = start + int(getattr(item, "index", 0) or 0)
                    if 0 <= index < len(vectors):
                        vectors[index] = list(getattr(item, "embedding", None) or [])

        await asyncio.gather(*(one(start, batch) for start, batch in batches))
        return vectors

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
        if len((text or "").strip()) < MIN_EMBED_CHARS:
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
        # The scan is CPU-heavy (thousands of embeddings); keep it off the event
        # loop so a memory search cannot stall message handling.
        return await asyncio.to_thread(
            db.search_memory_vectors, self.model, vector, limit
        )

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
