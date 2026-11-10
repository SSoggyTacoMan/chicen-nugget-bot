from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from bot.config import settings

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file = RotatingFileHandler(
            settings.log_dir / "bot.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file.setFormatter(fmt)
        root.addHandler(file)
    except OSError:
        pass  # console-only is fine

    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    _CONFIGURED = True
