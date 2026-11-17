"""Process-wide logging setup.

Why is this needed at all?
Python's root logger defaults to WARNING and has no handler, so every
`logger.info(...)` in the graph was silently dropped while `logger.warning`
lines appeared via `logging.lastResort`. That made the critic's decision trail
(`critic round 2: sufficient=... stop=...`) invisible in the exact place an
operator looks first - and it hid a real bug for a whole run.

Rules:
- configure ONCE, at import of app.main, before anything can log
- one handler, one format, on stderr (uvicorn's own access log stays on stdout,
  so `docker logs` shows a clean split)
- level comes from LOG_LEVEL so prod can raise it without a code change
"""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_app_handler", False) for h in root.handlers):
        return                                   # already configured (reload)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler._app_handler = True                  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Uvicorn installs its own handlers; keep them but stop them double-printing
    # application lines through the root handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = False
