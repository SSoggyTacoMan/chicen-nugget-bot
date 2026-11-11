from datetime import datetime, timedelta, timezone

from bot.db import Database


def make_db(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    return db


def test_members_and_stats(tmp_path):
    db = make_db(tmp_path)
    db.record_join("U1", "alice", "C1")
    db.record_join("U1", "alice", "C1")
    db.record_join("U2", "bob", "C1")
    db.record_leave("U2", "C1")

    member = db.member("U1")
    assert member["joins"] == 2
    assert db.was_welcomed("U1") is False
    db.mark_welcomed("U1")
    assert db.was_welcomed("U1") is True

    stats = db.stats()
    assert stats["members"] == 2
    assert stats["joins_24h"] == 3
    assert stats["leaves_24h"] == 1


def test_access_requests(tmp_path):
    db = make_db(tmp_path)
    request_id = db.create_access_request("U1", "let me in")
    assert db.pending_access_for_user("U1")["id"] == request_id
    assert len(db.pending_access()) == 1

    db.decide_access(request_id, "approved", "Uowner")
    assert db.pending_access_for_user("U1") is None
    assert db.access_counts()["approved"] == 1
    assert db.access_request(request_id)["status"] == "approved"


def test_reminders(tmp_path):
    db = make_db(tmp_path)
    now = datetime.now(timezone.utc)
    reminder_id = db.add_reminder(
        user_id="U1",
        target="dm:U1",
        text="stretch",
        next_run=now - timedelta(minutes=1),
        recurrence="every:10",
    )
    due = db.due_reminders(now)
    assert [row["id"] for row in due] == [reminder_id]
    db.advance_reminder(reminder_id, now + timedelta(minutes=10))
    assert db.due_reminders(now) == []
    assert len(db.list_reminders("U1")) == 1
    assert db.cancel_reminder(reminder_id) is True
    assert db.list_reminders("U1") == []


def test_polls(tmp_path):
    db = make_db(tmp_path)
    db.create_poll("abc123", "C1", "lunch?", ["pizza", "tacos"], "U1")
    db.vote_poll("abc123", "U1", 0)
    db.vote_poll("abc123", "U2", 1)
    db.vote_poll("abc123", "U3", 1)
    counts, total = db.poll_counts("abc123")
    assert counts == [1, 2]
    assert total == 3

    db.vote_poll("abc123", "U1", 1)
    counts, total = db.poll_counts("abc123")
    assert counts == [0, 3]
    db.unvote_poll("abc123", "U1")
    counts, total = db.poll_counts("abc123")
    assert counts == [0, 2]
    assert total == 2


def test_threads_and_kv(tmp_path):
    db = make_db(tmp_path)
    assert db.is_thread("C1", "123.456") is False
    assert db.thread_owner("C1", "123.456") is None

    db.mark_thread("C1", "123.456", owner="bot")
    assert db.is_thread("C1", "123.456") is True
    assert db.is_thread("C1", "123.456", owner="bot") is True
    assert db.is_thread("C1", "123.456", owner="user") is False
    assert db.thread_owner("C1", "123.456") == "bot"

    # Switching owner
    db.mark_thread("C1", "123.456", owner="user")
    assert db.is_thread("C1", "123.456", owner="user") is True
    assert db.is_thread("C1", "123.456", owner="bot") is False
    assert db.thread_owner("C1", "123.456") == "user"

    db.kv_set("k", "v")
    assert db.kv_get("k") == "v"


def test_clear_ai_history(tmp_path):
    db = make_db(tmp_path)
    db.add_message("dm:U1", "user", "U1", "hello")
    db.add_message("dm:U1", "assistant", "B1", "hi")
    db.add_message("ch:C1:111", "user", "U1", "thread one")
    db.add_message("ch:C1:222", "user", "U1", "thread two")
    db.add_message("ch:C2:333", "user", "U1", "other channel")

    assert db.clear_history("dm:U1") == 2
    assert db.history("dm:U1", 10) == []
    assert db.clear_history_prefix("ch:C1:") == 2
    assert db.history("ch:C1:111", 10) == []
    assert db.history("ch:C2:333", 10)

