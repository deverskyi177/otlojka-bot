import random
import time
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

router = Router()

PRIZES = [
    "🎉 Ничего не выпало, повезёт завтра!",
    "⭐ +10 очков удачи",
    "🍀 Двойная удача завтра",
    "🎁 Секретный приз",
    "💎 Джекпот! Ты счастливчик дня",
    "😅 Пусто",
    "🔥 Огненная серия началась",
]

last_spin: dict[int, float] = {}  # user_id -> timestamp
COOLDOWN = 24 * 3600


def wheel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Крутить колесо", callback_data="wheel:spin")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")],
    ])


@router.callback_query(F.data == "game:wheel")
async def wheel_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎁 <b>Колесо фортуны</b>\n\n"
        "Раз в 24 часа можно крутить колесо и получить случайный приз!",
        reply_markup=wheel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "wheel:spin")
async def wheel_spin(callback: CallbackQuery):
    uid = callback.from_user.id
    now = time.time()
    last = last_spin.get(uid, 0)

    if now - last < COOLDOWN:
        remaining = int(COOLDOWN - (now - last))
        hours, minutes = remaining // 3600, (remaining % 3600) // 60
        await callback.answer(
            f"⏳ Уже крутил(а) сегодня! Повтори через {hours}ч {minutes}м",
            show_alert=True,
        )
        return

    last_spin[uid] = now
    prize = random.choice(PRIZES)

    await callback.message.edit_text(
        f"🎁 <b>Колесо фортуны</b>\n\n"
        f"{callback.from_user.first_name}, твой приз:\n\n"
        f"<b>{prize}</b>\n\n"
        "Возвращайся через 24 часа за новым призом!",
        reply_markup=wheel_kb(),
    )
    await callback.answer()
