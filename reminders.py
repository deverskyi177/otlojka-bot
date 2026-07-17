import re
import time
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from db.database import add_reminder
from keyboards.main_menu import back_to_main_kb

router = Router()

# /remind 10m Не забыть проверить пиццу
# /remind 2h Позвонить другу
# /remind 1d30m Купить корм коту
UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
DURATION_RE = re.compile(r"(\d+)([dhms])")


def parse_duration(raw: str) -> int | None:
    matches = DURATION_RE.findall(raw)
    if not matches:
        return None
    total = 0
    for value, unit in matches:
        total += int(value) * UNIT_SECONDS[unit]
    return total if total > 0 else None


@router.callback_query(F.data == "menu:reminders")
async def cb_menu_reminders(callback: CallbackQuery):
    await callback.message.edit_text(
        "⏰ <b>Напоминания</b>\n\n"
        "Команда:\n"
        "<code>/remind [время] [текст]</code>\n\n"
        "Примеры:\n"
        "<code>/remind 10m Проверить пиццу</code>\n"
        "<code>/remind 2h Позвонить другу</code>\n"
        "<code>/remind 1d30m Купить корм коту</code>\n\n"
        "d — дни, h — часы, m — минуты, s — секунды",
        reply_markup=await back_to_main_kb(),
    )
    await callback.answer()


@router.message(Command("remind"))
async def cmd_remind(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply(
            "⚠️ Формат: <code>/remind 10m текст напоминания</code>"
        )
        return

    duration_raw, text = args[1], args[2]
    seconds = parse_duration(duration_raw)
    if seconds is None:
        await message.reply(
            "⚠️ Не понял время. Пример: <code>10m</code>, <code>2h</code>, <code>1d30m</code>"
        )
        return

    remind_at = int(time.time()) + seconds
    await add_reminder(message.from_user.id, message.chat.id, text, remind_at)

    await message.reply(
        f"✅ Напомню через {duration_raw}: <b>{text}</b>"
    )
