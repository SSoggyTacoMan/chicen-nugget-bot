"""Entry point: `python main.py`."""

from __future__ import annotations

import asyncio

from bot.app import run


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
