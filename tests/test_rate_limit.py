from app.rate_limit import RateLimiter


def test_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter(limit=3, window_seconds=60, clock=lambda: 0.0)
    assert limiter.allow("a")
    assert limiter.allow("a")
    assert limiter.allow("a")
    assert not limiter.allow("a")


def test_different_keys_have_independent_limits():
    limiter = RateLimiter(limit=1, window_seconds=60, clock=lambda: 0.0)
    assert limiter.allow("a")
    assert limiter.allow("b")
    assert not limiter.allow("a")


def test_window_expiry_frees_up_capacity():
    now = [0.0]
    limiter = RateLimiter(limit=1, window_seconds=60, clock=lambda: now[0])
    assert limiter.allow("a")
    assert not limiter.allow("a")
    now[0] = 61.0
    assert limiter.allow("a")


def test_blocked_attempt_does_not_itself_count_as_a_hit():
    now = [0.0]
    limiter = RateLimiter(limit=1, window_seconds=60, clock=lambda: now[0])
    assert limiter.allow("a")
    for _ in range(5):
        assert not limiter.allow("a")
    now[0] = 61.0
    assert limiter.allow("a")
