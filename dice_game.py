import random
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db.database import log_game_result

router = Router()

pending: dict[int, dict] = {}  # chat_id -> {"p1": id, "p1_name": str}


def dice_join_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Присоединиться", callback_data="dice:join")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")],
    ])


@router.callback_query(F.data == "game:dice")
async def dice_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    pending[chat_id] = {"p1": callback.from_user.id, "p1_name": callback.from_user.first_name}
    await callback.message.edit_text(
        "🎲 <b>Кости</b>\n\n"
        f"{callback.from_user.first_name} бросает вызов!\n"
        "Кто присоединится — у кого сумма больше, тот побеждает.",
        reply_markup=dice_join_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "dice:join")
async def dice_join(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = pending.get(chat_id)
    if not game:
        await callback.answer("Игра уже неактуальна, начните заново", show_alert=True)
        return
    if callback.from_user.id == game["p1"]:
        await callback.answer("Нельзя играть самому с собой 😄", show_alert=True)
        return

    p1_roll = random.randint(1, 6) + random.randint(1, 6)
    p2_roll = random.randint(1, 6) + random.randint(1, 6)

    if p1_roll > p2_roll:
        result = f"🏆 Победитель: <b>{game['p1_name']}</b>"
        winner_id = game["p1"]
    elif p2_roll > p1_roll:
        result = f"🏆 Победитель: <b>{callback.from_user.first_name}</b>"
        winner_id = callback.from_user.id
    else:
        result = "🤝 Ничья!"
        winner_id = None

    await callback.message.edit_text(
        "🎲 <b>Результаты броска</b>\n\n"
        f"{game['p1_name']}: {p1_roll}\n"
        f"{callback.from_user.first_name}: {p2_roll}\n\n"
        f"{result}",
    )
    await log_game_result("dice", chat_id, winner_id)
    del pending[chat_id]
    await callback.answer()
