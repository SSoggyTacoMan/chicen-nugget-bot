from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from bot.ai.memory import MemoryIndex
from bot.db import Database, iso, utcnow


class FakeEmbeddings:
    async def create(self, *, model: str, input: str):
        # tiny deterministic vectors: sentiment-ish keyword counts
        vector = [
            float(input.lower().count("deploy")),
            float(input.lower().count("memory")),
            float(input.lower().count("pr")),
            1.0,
        ]
        return SimpleNamespace(data=[SimpleNamespace(embedding=vector)])


class FakeLLM:
    embeddings = FakeEmbeddings()


def test_vector_store_roundtrip_and_search(tmp_path):
    db = Database(str(tmp_path / "memory.db"))
    db.init()
    first = db.add_message("dm:U1", "user", "U1", "the deploy is broken")
    second = db.add_message("dm:U1", "assistant", None, "memory search is useful")
    db.add_memory_vector(first, "dm:U1", "user", "U1", "the deploy is broken", "m", [3, 0, 0, 1])
    db.add_memory_vector(second, "dm:U1", "assistant", None, "memory search is useful", "m", [0, 3, 0, 1])

    results = db.search_memory_vectors("m", [3, 0, 0, 1], limit=2)
    assert results[0]["message_id"] == first
    assert "deploy" in results[0]["text"]
    assert len(results) == 2

    assert db.clear_all_history() == 2
    assert db.search_memory_vectors("m", [3, 0, 0, 1]) == []


def test_memory_index_embeds_and_searches(tmp_path):
    db = Database(str(tmp_path / "memory2.db"))
    db.init()
    message_id = db.add_message("dm:U1", "user", "U1", "deploy notes")
    settings = SimpleNamespace(
        ai_embedding_model="fake-embed",
        ai_api_key="test",
        ai_base_url="https://example.com/v1",
    )
    index = MemoryIndex(settings, llm=FakeLLM())

    async def run():
        await index.remember(
            db,
            message_id=message_id,
            conversation="dm:U1",
            role="user",
            user_id="U1",
            text="deploy notes",
        )
        hits = await index.search(db, "deploy", limit=3)
        assert hits and hits[0]["message_id"] == message_id

    asyncio.run(run())


def test_prune_history_clears_old_messages_and_vectors(tmp_path):
    db = Database(str(tmp_path / "prune.db"))
    db.init()
    old_message = db.add_message("dm:U1", "user", "U1", "old deploy notes")
    new_message = db.add_message("dm:U1", "user", "U1", "new notes")
    old_time = iso(utcnow() - timedelta(days=400))
    db._execute("UPDATE messages SET created_at = ? WHERE id = ?", (old_time, old_message))
    db.add_memory_vector(old_message, "dm:U1", "user", "U1", "old deploy notes", "m", [1, 0])
    db.add_memory_vector(new_message, "dm:U1", "user", "U1", "new notes", "m", [0, 1])

    counts = db.prune_history(180)
    assert counts["messages"] == 1
    assert counts["vectors"] == 1
    assert db.count_memory_vectors("m") == 1
