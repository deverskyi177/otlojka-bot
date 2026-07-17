import random
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db.database import log_game_result

router = Router()

pending: dict[int, dict] = {}  # chat_id -> {"bets": {user_id: (name, color)}}

COLORS = {"red": "🔴 Красное", "black": "⚫ Чёрное", "green": "🟢 Зеро"}
WEIGHTS = {"red": 45, "black": 45, "green": 10}  # шанс выпадения


def roulette_bet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 Красное", callback_data="roul:bet:red"),
            InlineKeyboardButton(text="⚫ Чёрное", callback_data="roul:bet:black"),
            InlineKeyboardButton(text="🟢 Зеро", callback_data="roul:bet:green"),
        ],
        [InlineKeyboardButton(text="🎡 Крутить!", callback_data="roul:spin")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")],
    ])


@router.callback_query(F.data == "game:roulette")
async def roulette_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    pending[chat_id] = {"bets": {}}
    await callback.message.edit_text(
        "🎡 <b>Рулетка</b>\n\n"
        "Все желающие делают ставку на цвет, потом кто-то крутит колесо.\n"
        "Совпал цвет — победа!\n\n"
        "Ставок сделано: 0",
        reply_markup=roulette_bet_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("roul:bet:"))
async def roulette_bet(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = pending.get(chat_id)
    if not game:
        await callback.answer("Игра не найдена, начните заново", show_alert=True)
        return

    color = callback.data.split(":")[2]
    game["bets"][callback.from_user.id] = (callback.from_user.first_name, color)

    await callback.message.edit_text(
        "🎡 <b>Рулетка</b>\n\n"
        "Все желающие делают ставку на цвет, потом кто-то крутит колесо.\n"
        "Совпал цвет — победа!\n\n"
        f"Ставок сделано: {len(game['bets'])}",
        reply_markup=roulette_bet_kb(),
    )
    await callback.answer(f"Ставка принята: {COLORS[color]}")


@router.callback_query(F.data == "roul:spin")
async def roulette_spin(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = pending.get(chat_id)
    if not game or not game["bets"]:
        await callback.answer("Сначала кто-то должен сделать ставку", show_alert=True)
        return

    result_color = random.choices(list(WEIGHTS.keys()), weights=list(WEIGHTS.values()))[0]
    winners = [name for name, color in game["bets"].values() if color == result_color]

    text = f"🎡 <b>Выпало: {COLORS[result_color]}</b>\n\n"
    if winners:
        text += "🏆 Победители:\n" + "\n".join(f"• {w}" for w in winners)
    else:
        text += "😔 В этот раз никто не угадал."

    await callback.message.edit_text(text)

    winner_id = None
    for uid, (name, color) in game["bets"].items():
        if color == result_color:
            winner_id = uid
            break
    await log_game_result("roulette", chat_id, winner_id)
    del pending[chat_id]
    await callback.answer()
