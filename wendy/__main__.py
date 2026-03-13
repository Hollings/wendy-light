"""Entry point: python -m wendy"""
from __future__ import annotations

import logging
import os
import sys


def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logging.error("DISCORD_TOKEN environment variable is required")
        sys.exit(1)

    from .discord_client import Bot

    bot = Bot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
