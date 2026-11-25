"""Test-wide environment, applied before any app module is imported.

Rate limiting is a production concern. Unit tests that exercise a route should
not have to budget their requests or clear a Redis key, so the default here is
"off"; `test_ratelimit.py` constructs its own limiter and tests the counter
directly. CI sets the same value for the same reason.
"""
import os

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
