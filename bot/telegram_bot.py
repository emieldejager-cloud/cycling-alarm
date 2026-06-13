"""
Telegram bot — notifications and feedback handling.
"""
import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from db.database import Feedback, FeedbackType, Notification, Race, get_db
from scorer.rule_scorer import (
    calculate_new_threshold,
    get_current_threshold,
    set_threshold,
)

logger = logging.getLogger(__name__)

_feedback_since_last_adjust: int = 0
FEEDBACK_ADJUST_INTERVAL = 20


def _build_feedback_keyboard(notification_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Goed moment", callback_data=f"fb:{notification_id}:good"),
            InlineKeyboardButton("⏰ Te vroeg", callback_data=f"fb:{notification_id}:too_early"),
        ],
        [
            InlineKeyboardButton("⏳ Te laat", callback_data=f"fb:{notification_id}:too_late"),
            InlineKeyboardButton("❌ Onnodig", callback_data=f"fb:{notification_id}:unnecessary"),
        ],
    ])


def _format_notification(
    race: Race,
    km_remaining: Optional[float],
    event_summary: str,
) -> str:
    stage_text = f" — Etappe {race.stage_number}" if race.stage_number else ""
    km_text = f"{km_remaining:.0f}" if km_remaining is not None else "?"
    channel_text = ""
    if race.hbo_available and race.hbo_start_time:
        channel_text = f"\n📺 Zet HBO Max aan! (start was om {race.hbo_start_time})"
    else:
        channel_text = "\n📺 Zet HBO Max aan!"

    return (
        f"🚴 *{race.name}*{stage_text}\n"
        f"📍 Nog *{km_text} km* te gaan\n"
        f"⚡ {event_summary}"
        f"{channel_text}\n\n"
        f"_Hoe was deze notificatie?_"
    )


async def send_notification(
    bot: Bot,
    chat_id: str,
    race_id: int,
    score: float,
    event_summary: str,
    km_remaining: Optional[float],
    trigger_event_id: Optional[int] = None,
) -> Optional[int]:
    """Send a race notification to Telegram and store in DB."""
    db = get_db()
    try:
        race = db.query(Race).filter(Race.id == race_id).first()
        if not race:
            logger.error("Race %d not found for notification", race_id)
            return None

        text = _format_notification(race, km_remaining, event_summary)

        # Store notification first (to get ID for keyboard)
        notification = Notification(
            race_id=race_id,
            triggered_at=datetime.utcnow(),
            score=score,
            event_summary=event_summary,
            trigger_event_id=trigger_event_id,
        )
        db.add(notification)
        db.commit()
        db.refresh(notification)

        keyboard = _build_feedback_keyboard(notification.id)

        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            notification.telegram_message_id = msg.message_id
            db.commit()
            logger.info(
                "Notification sent for race %d (score=%.1f, msg_id=%d)",
                race_id, score, msg.message_id,
            )
            return notification.id
        except TelegramError as e:
            logger.error("Failed to send Telegram message: %s", e)
            return None

    finally:
        db.close()


async def send_warning(bot: Bot, chat_id: str, message: str) -> None:
    """Send a plain warning message."""
    try:
        await bot.send_message(chat_id=chat_id, text=message)
    except TelegramError as e:
        logger.error("Failed to send Telegram warning: %s", e)


async def _handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle InlineKeyboard feedback callbacks."""
    global _feedback_since_last_adjust

    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "fb":
        return

    notification_id = int(parts[1])
    feedback_str = parts[2]

    feedback_map = {
        "good": FeedbackType.good,
        "too_early": FeedbackType.too_early,
        "too_late": FeedbackType.too_late,
        "unnecessary": FeedbackType.unnecessary,
    }
    feedback_value = feedback_map.get(feedback_str)
    if not feedback_value:
        return

    db = get_db()
    try:
        # Check notification exists
        notification = db.query(Notification).filter(Notification.id == notification_id).first()
        if not notification:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        # Don't allow duplicate feedback
        existing = db.query(Feedback).filter(Feedback.notification_id == notification_id).first()
        if existing:
            await query.answer("Je hebt al feedback gegeven.", show_alert=True)
            return

        fb = Feedback(
            notification_id=notification_id,
            feedback=feedback_value,
            received_at=datetime.utcnow(),
        )
        db.add(fb)
        db.commit()

        _feedback_since_last_adjust += 1

        # Adjust threshold every 20 feedbacks
        if _feedback_since_last_adjust >= FEEDBACK_ADJUST_INTERVAL:
            all_feedback = db.query(Feedback).all()
            new_threshold = calculate_new_threshold(get_current_threshold(), all_feedback)
            if new_threshold != get_current_threshold():
                set_threshold(new_threshold)
                logger.info("Notification threshold updated to %.0f", new_threshold)
            _feedback_since_last_adjust = 0

    finally:
        db.close()

    # Remove keyboard and confirm
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("👍 Bedankt! Ik leer ervan.")
    except TelegramError as e:
        logger.warning("Could not edit message after feedback: %s", e)


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    db = get_db()
    try:
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        races = db.query(Race).filter(Race.date == today).all()

        if not races:
            await update.message.reply_text("📭 Geen koersen gevonden voor vandaag.")
            return

        lines = [f"🚴 *Koersen vandaag* (drempel: {get_current_threshold():.0f})\n"]
        for race in races:
            hbo = f"📺 {race.hbo_start_time}" if race.hbo_available else "❌ Niet op TV"
            ticker = "🟢 Ticker" if race.ticker_url else "⚪ Geen ticker"
            lines.append(f"• *{race.name}* | {hbo} | {ticker}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    finally:
        db.close()


async def _handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🚴 *Cycling Alarm Bot*\n\n"
        "/status — Koersen van vandaag\n"
        "/drempel — Huidige notificatiedrempel\n"
        "/help — Dit bericht",
        parse_mode="Markdown",
    )


async def _handle_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"📊 Huidige notificatiedrempel: *{get_current_threshold():.0f}* (0–100)\n"
        "Drempel wordt automatisch bijgesteld op basis van jouw feedback.",
        parse_mode="Markdown",
    )


def build_application(token: str) -> Application:
    """Build and configure the Telegram bot application."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _handle_help))
    app.add_handler(CommandHandler("help", _handle_help))
    app.add_handler(CommandHandler("status", _handle_status))
    app.add_handler(CommandHandler("drempel", _handle_threshold))
    app.add_handler(CallbackQueryHandler(_handle_feedback, pattern=r"^fb:"))
    return app
