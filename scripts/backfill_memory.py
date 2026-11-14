"""Embed stored conversation turns for semantic memory.

Run once after enabling AI_EMBEDDING_MODEL, then occasionally if you want to
catch up old history:

    .venv/bin/python scripts/backfill_memory.py 1000
"""

from __future__ import annotations

import asyncio
import sys

from bot.ai.memory import MemoryIndex
from bot.config import settings
from bot.db import Database


async def main() -> None:
    index = MemoryIndex(settings)
    if not index.enabled:
        print("semantic memory is disabled: set AI_EMBEDDING_MODEL first")
        return
    db = Database(settings.db_path)
    db.init()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    indexed = await index.backfill(db, limit=limit)
    print(f"indexed {indexed} turn(s) with {index.model}")


if __name__ == "__main__":
    asyncio.run(main())
