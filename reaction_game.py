import asyncio
import random
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db.database import log_game_result

router = Router()

active: dict[int, dict] = {}  # chat_id -> {"ready": bool, "winner": None}


def waiting_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Жди сигнала...", callback_data="reaction:early")],
    ])


def ready_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ ЖМИ!", callback_data="reaction:press")],
    ])


@router.callback_query(F.data == "game:reaction")
async def reaction_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    active[chat_id] = {"ready": False, "winner": None}

    await callback.message.edit_text(
        "⚡ <b>Дуэль на реакции</b>\n\n"
        "Кнопка появится в случайный момент — жми первым!\n"
        "Жать раньше времени нельзя 😉",
        reply_markup=waiting_kb(),
    )
    await callback.answer()

    delay = random.uniform(2, 6)
    await asyncio.sleep(delay)

    game = active.get(chat_id)
    if game is None:
        return
    game["ready"] = True
    await callback.message.edit_text(
        "⚡ <b>ЖМИ СЕЙЧАС!</b>",
        reply_markup=ready_kb(),
    )


@router.callback_query(F.data == "reaction:early")
async def reaction_early(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = active.get(chat_id)
    if game and not game["ready"]:
        await callback.answer("🚫 Рано! Дисквалификация за фальстарт", show_alert=True)
        del active[chat_id]
        await callback.message.edit_text("🚫 <b>Фальстарт!</b> Игра прервана.")
    else:
        await callback.answer()


@router.callback_query(F.data == "reaction:press")
async def reaction_press(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = active.get(chat_id)
    if not game or not game["ready"] or game["winner"]:
        await callback.answer()
        return

    game["winner"] = callback.from_user.id
    await callback.message.edit_text(
        f"🏆 <b>{callback.from_user.first_name} победил(а) в реакции!</b>"
    )
    await log_game_result("reaction", chat_id, callback.from_user.id)
    del active[chat_id]
    await callback.answer("Ты первый!")
