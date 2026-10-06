"""Entry point: ``python -m modmail``."""

from __future__ import annotations

import asyncio
import logging
import sys

from modmail.bot import ModmailBot
from modmail.config import ConfigurationError, get_settings
from modmail.db import create_all, dispose_engine, init_engine
from modmail.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def main() -> int:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    log_path = setup_logging(settings.log_level, settings.log_dir)
    logger.info("Starting ModMail. Log file: %s", log_path)
    logger.debug("Database: %s", settings.database_url)

    async def runner() -> None:
        init_engine(settings.database_url)
        await create_all()

        bot = ModmailBot(settings)
        try:
            await bot.start(settings.discord_token)
        finally:
            await dispose_engine()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    except Exception:
        logger.exception("Fatal error.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
