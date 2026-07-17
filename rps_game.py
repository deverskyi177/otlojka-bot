from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db.database import log_game_result

router = Router()

CHOICES = {"rock": "🪨 Камень", "scissors": "✂️ Ножницы", "paper": "📄 Бумага"}
BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}

pending: dict[int, dict] = {}  # chat_id -> {p1_id, p1_name, p1_choice, p2_id, p2_name, p2_choice}


def rps_join_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✊ Присоединиться", callback_data="rps:join")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")],
    ])


def rps_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🪨", callback_data="rps:pick:rock"),
            InlineKeyboardButton(text="✂️", callback_data="rps:pick:scissors"),
            InlineKeyboardButton(text="📄", callback_data="rps:pick:paper"),
        ]
    ])


@router.callback_query(F.data == "game:rps")
async def rps_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    pending[chat_id] = {"p1_id": callback.from_user.id, "p1_name": callback.from_user.first_name}
    await callback.message.edit_text(
        "✂️ <b>Камень-Ножницы-Бумага</b>\n\n"
        f"{callback.from_user.first_name} вызывает соперника!",
        reply_markup=rps_join_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "rps:join")
async def rps_join(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = pending.get(chat_id)
    if not game or "p2_id" in game:
        await callback.answer("Игра уже неактуальна", show_alert=True)
        return
    if callback.from_user.id == game["p1_id"]:
        await callback.answer("Нужен второй игрок 😄", show_alert=True)
        return

    game["p2_id"] = callback.from_user.id
    game["p2_name"] = callback.from_user.first_name
    await callback.message.edit_text(
        "✂️ <b>Камень-Ножницы-Бумага</b>\n\n"
        f"{game['p1_name']} 🆚 {game['p2_name']}\n\n"
        "Оба игрока — выбирайте втайне, кнопки одинаковые для обоих.",
        reply_markup=rps_choice_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rps:pick:"))
async def rps_pick(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = pending.get(chat_id)
    if not game or "p2_id" not in game:
        await callback.answer("Игра ещё не готова", show_alert=True)
        return

    choice = callback.data.split(":")[2]
    uid = callback.from_user.id

    if uid == game["p1_id"]:
        game["p1_choice"] = choice
        await callback.answer(f"Ты выбрал {CHOICES[choice]}", show_alert=True)
    elif uid == game["p2_id"]:
        game["p2_choice"] = choice
        await callback.answer(f"Ты выбрал {CHOICES[choice]}", show_alert=True)
    else:
        await callback.answer("Ты не участвуешь в этой игре", show_alert=True)
        return

    if "p1_choice" in game and "p2_choice" in game:
        c1, c2 = game["p1_choice"], game["p2_choice"]
        if c1 == c2:
            result_text = "🤝 Ничья!"
            winner_id = None
        elif BEATS[c1] == c2:
            result_text = f"🏆 Победитель: <b>{game['p1_name']}</b>"
            winner_id = game["p1_id"]
        else:
            result_text = f"🏆 Победитель: <b>{game['p2_name']}</b>"
            winner_id = game["p2_id"]

        await callback.message.edit_text(
            "✂️ <b>Результат</b>\n\n"
            f"{game['p1_name']}: {CHOICES[c1]}\n"
            f"{game['p2_name']}: {CHOICES[c2]}\n\n"
            f"{result_text}",
        )
        await log_game_result("rps", chat_id, winner_id)
        del pending[chat_id]
