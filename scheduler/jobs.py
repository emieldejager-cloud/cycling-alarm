"""
APScheduler jobs for cycling alarm bot.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot

from db.database import EventType, Notification, Race, TickerEvent, get_db
from scrapers.hbo_checker import check_hbo_availability
from scrapers.pcs_scraper import poll_ticker, scrape_race_schedule, scrape_stage_profile
from scorer.rule_scorer import ScoreResult, get_current_threshold, score_situation
from bot.telegram_bot import send_notification, send_warning

logger = logging.getLogger(__name__)

# Track last notification time per race to enforce cooldown
_last_notification: dict[int, datetime] = {}
NOTIFICATION_COOLDOWN_SECONDS = 15 * 60


async def job_scrape_race_schedule(bot: Bot, chat_id: str) -> None:
    """06:00 daily — fetch today's races from PCS."""
    logger.info("Running: scrape_race_schedule")
    try:
        races = await scrape_race_schedule()
        logger.info("Found %d race(s) today", len(races))

        # Trigger stage profile scrape for each new race
        for race in races:
            if not race.finish_type:
                asyncio.create_task(job_scrape_stage_profile(race.id))

    except Exception as e:
        logger.exception("scrape_race_schedule failed: %s", e)


async def job_check_hbo_availability(bot: Bot, chat_id: str) -> None:
    """06:30 daily — check HBO/tvgids for each of today's races."""
    logger.info("Running: check_hbo_availability")
    db = get_db()
    try:
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        races = db.query(Race).filter(Race.date == today).all()

        for race in races:
            try:
                result = await check_hbo_availability(race.name)
                race.hbo_available = result["available"]
                race.hbo_start_time = result.get("start_time")
                db.commit()

                if result["available"]:
                    logger.info(
                        "HBO available for %s at %s on %s",
                        race.name, result.get("start_time"), result.get("channel"),
                    )
                else:
                    warning = (
                        f"⚠️ Kon HBO/tvgids niet bevestigen voor *{race.name}* "
                        f"— controleer zelf of er een uitzending is."
                    )
                    await send_warning(bot, chat_id, warning)
                    logger.warning("No HBO broadcast found for %s", race.name)

            except Exception as e:
                logger.exception("HBO check failed for %s: %s", race.name, e)
                await send_warning(
                    bot, chat_id,
                    f"⚠️ Kon HBO/tvgids niet checken voor *{race.name}* — controleer zelf."
                )

    finally:
        db.close()


async def job_scrape_stage_profile(race_id: int) -> None:
    """Triggered when a new race is found — scrape its stage profile."""
    logger.info("Running: scrape_stage_profile for race_id=%d", race_id)
    try:
        await scrape_stage_profile(race_id)
    except Exception as e:
        logger.exception("scrape_stage_profile failed for race %d: %s", race_id, e)


async def job_poll_active_races(bot: Bot, chat_id: str) -> None:
    """Every 60s — poll tickers for active races and score events."""
    db = get_db()
    try:
        from datetime import date, time as dtime
        today = date.today().strftime("%Y-%m-%d")
        now = datetime.now()

        races = db.query(Race).filter(Race.date == today, Race.ticker_url.isnot(None)).all()

        for race in races:
            # Check race is roughly in its active window
            # We use hbo_start_time as a proxy; if unknown, allow any time 08:00–22:00
            if not _is_race_active(race, now):
                continue

            try:
                new_events = await poll_ticker(race.id)
                if not new_events:
                    continue

                await _evaluate_and_notify(race, new_events, bot, chat_id, db)

            except Exception as e:
                logger.exception("Ticker poll/score failed for race %d: %s", race.id, e)

    finally:
        db.close()


def _is_race_active(race: Race, now: datetime) -> bool:
    """Check if race is currently in its expected window."""
    if race.hbo_start_time:
        try:
            h, m = map(int, race.hbo_start_time.split(":"))
            start = now.replace(hour=h, minute=m, second=0, microsecond=0)
            window_start = start - timedelta(minutes=30)
            window_end = start + timedelta(hours=6)
            return window_start <= now <= window_end
        except (ValueError, AttributeError):
            pass

    # Default: consider active between 08:00 and 22:00
    return 8 <= now.hour < 22


async def _evaluate_and_notify(
    race: Race,
    new_events: list[TickerEvent],
    bot: Bot,
    chat_id: str,
    db,
) -> None:
    """Score events and send notification if warranted."""
    # Get last 10 events (newest first)
    recent_events = (
        db.query(TickerEvent)
        .filter(TickerEvent.race_id == race.id)
        .order_by(TickerEvent.id.desc())
        .limit(10)
        .all()
    )

    stage_profile = race.stage_profile

    threshold = get_current_threshold()
    result = score_situation(recent_events, stage_profile, threshold)

    if not result.should_notify:
        return

    latest = recent_events[0]
    is_crash = any(e.event_type == EventType.crash for e in new_events)

    # Enforce cooldown (skip for crash events)
    last_notif_time = _last_notification.get(race.id)
    if last_notif_time and not is_crash:
        elapsed = (datetime.now() - last_notif_time).total_seconds()
        if elapsed < NOTIFICATION_COOLDOWN_SECONDS:
            logger.debug(
                "Race %d cooldown active (%.0fs remaining)",
                race.id, NOTIFICATION_COOLDOWN_SECONDS - elapsed,
            )
            return

    km_remaining = latest.km_remaining
    event_summary = _build_event_summary(result.reasons, latest)

    if result.notify_in_seconds > 0:
        logger.info(
            "Scheduling notification for race %d in %ds (score=%.1f)",
            race.id, result.notify_in_seconds, result.score,
        )
        await asyncio.sleep(result.notify_in_seconds)

    notification_id = await send_notification(
        bot=bot,
        chat_id=chat_id,
        race_id=race.id,
        score=result.score,
        event_summary=event_summary,
        km_remaining=km_remaining,
        trigger_event_id=latest.id,
    )

    if notification_id:
        _last_notification[race.id] = datetime.now()


def _build_event_summary(reasons: list[str], latest_event: TickerEvent) -> str:
    """Build human-readable event summary for notification."""
    if reasons:
        # Clean up reason strings (remove score suffixes)
        import re
        clean = [re.sub(r"\s*\(\+\d+\)$", "", r) for r in reasons[:2]]
        return " + ".join(clean).capitalize()
    return latest_event.description or latest_event.raw_text[:120]


def setup_scheduler(bot: Bot, chat_id: str) -> AsyncIOScheduler:
    """Create and configure APScheduler with all jobs."""
    scheduler = AsyncIOScheduler(timezone="Europe/Amsterdam")

    # 06:00 daily — race schedule
    scheduler.add_job(
        job_scrape_race_schedule,
        trigger="cron",
        hour=6,
        minute=0,
        kwargs={"bot": bot, "chat_id": chat_id},
        id="scrape_race_schedule",
        replace_existing=True,
    )

    # 06:30 daily — HBO availability
    scheduler.add_job(
        job_check_hbo_availability,
        trigger="cron",
        hour=6,
        minute=30,
        kwargs={"bot": bot, "chat_id": chat_id},
        id="check_hbo_availability",
        replace_existing=True,
    )

    # Every 60s — poll active races
    scheduler.add_job(
        job_poll_active_races,
        trigger="interval",
        seconds=60,
        kwargs={"bot": bot, "chat_id": chat_id},
        id="poll_active_races",
        replace_existing=True,
    )

    return scheduler
