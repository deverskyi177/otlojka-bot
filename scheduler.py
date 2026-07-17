import time
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db.database import get_due_reminders, mark_reminder_done


async def check_reminders(bot: Bot):
    now_ts = int(time.time())
    due = await get_due_reminders(now_ts)
    for reminder_id, user_id, chat_id, text in due:
        try:
            await bot.send_message(chat_id, f"⏰ <b>Напоминание:</b> {text}")
        except Exception:
            pass
        await mark_reminder_done(reminder_id)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", seconds=20, args=[bot])
    return scheduler
