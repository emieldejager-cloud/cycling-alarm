"""
Cycling Alarm Bot — main entrypoint.
Starts APScheduler + Telegram bot polling.
"""
import asyncio
import logging
import os
import signal
import sys
from datetime import date

from dotenv import load_dotenv
from telegram import Bot

from db.database import init_db
from bot.telegram_bot import build_application
from scheduler.jobs import (
    job_check_hbo_availability,
    job_scrape_race_schedule,
    setup_scheduler,
)

load_dotenv()

# --- Logging setup ---
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("cycling-alarm.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.critical("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


async def _run_startup_jobs(bot: Bot, chat_id: str) -> None:
    """Run schedule + HBO check immediately on startup if today's data is missing."""
    from db.database import Race, get_db
    db = get_db()
    try:
        today = date.today().strftime("%Y-%m-%d")
        count = db.query(Race).filter(Race.date == today).count()
    finally:
        db.close()

    if count == 0:
        logger.info("No races in DB for today — running startup scrape")
        await job_scrape_race_schedule(bot, chat_id)
        await job_check_hbo_availability(bot, chat_id)
    else:
        logger.info("Found %d race(s) already in DB for today", count)


async def main() -> None:
    token = _require_env("TELEGRAM_BOT_TOKEN")
    chat_id = _require_env("TELEGRAM_CHAT_ID")

    # Initialise database
    logger.info("Initialising database")
    init_db()

    # Build Telegram application
    app = build_application(token)
    bot: Bot = app.bot

    # Run startup data fetch
    await _run_startup_jobs(bot, chat_id)

    # Configure and start scheduler
    scheduler = setup_scheduler(bot, chat_id)
    scheduler.start()
    logger.info("Scheduler started")

    # Graceful shutdown handler
    stop_event = asyncio.Event()

    def _handle_signal(sig, frame):
        logger.info("Received signal %s — shutting down", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Start polling
    logger.info("Starting Telegram bot polling")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot is running. Press Ctrl+C to stop.")

        await stop_event.wait()

        logger.info("Stopping bot and scheduler")
        await app.updater.stop()
        await app.stop()
        scheduler.shutdown(wait=False)

    logger.info("Cycling Alarm Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
