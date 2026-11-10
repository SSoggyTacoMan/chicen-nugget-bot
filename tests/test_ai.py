from bot.ai.limits import RateLimiter
from bot.features.ai_commands import _parse_imagine, _summary_limit
from bot.features.nuggets import FACTS


def test_rate_limiter_blocks_and_recovers():
    limiter = RateLimiter(2, window_seconds=60)
    assert limiter.allow("U1", now=0) == (True, 0)
    assert limiter.allow("U1", now=1) == (True, 0)
    allowed, retry = limiter.allow("U1", now=2)
    assert allowed is False
    assert retry >= 58
    assert limiter.allow("U2", now=2) == (True, 0)
    assert limiter.allow("U1", now=61) == (True, 0)


def test_rate_limiter_disabled():
    limiter = RateLimiter(0)
    for _ in range(50):
        assert limiter.allow("U1") == (True, 0)


def test_parse_imagine_flags():
    assert _parse_imagine("a nugget astronaut --wide") == ("a nugget astronaut", "16:9")
    assert _parse_imagine("a nugget castle --tall") == ("a nugget castle", "9:16")
    assert _parse_imagine("a nugget --ratio 4:3") == ("a nugget", "4:3")
    assert _parse_imagine("just a nugget") == ("just a nugget", "1:1")


def test_summary_limit():
    assert _summary_limit(None) == 50
    assert _summary_limit("10") == 10
    assert _summary_limit("0") == 10
    assert _summary_limit("999") == 200
    assert _summary_limit("wat") == 50


def test_nugget_facts_exist():
    assert len(FACTS) >= 20
    assert all(fact.strip() for fact in FACTS)
