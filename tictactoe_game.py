from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db.database import log_game_result

router = Router()

pending: dict[int, dict] = {}

WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]


def render_board(board: list, chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            symbol = board[i] if board[i] else " "
            row.append(InlineKeyboardButton(text=symbol, callback_data=f"ttt:move:{i}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def check_winner(board: list) -> str | None:
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


@router.callback_query(F.data == "game:tictactoe")
async def ttt_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    pending[chat_id] = {"p1_id": callback.from_user.id, "p1_name": callback.from_user.first_name}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Присоединиться", callback_data="ttt:join")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")],
    ])
    await callback.message.edit_text(
        "❌⭕ <b>Крестики-нолики</b>\n\n"
        f"{callback.from_user.first_name} ждёт соперника!",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "ttt:join")
async def ttt_join(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = pending.get(chat_id)
    if not game or "p2_id" in game:
        await callback.answer("Игра неактуальна", show_alert=True)
        return
    if callback.from_user.id == game["p1_id"]:
        await callback.answer("Нужен второй игрок 😄", show_alert=True)
        return

    game.update({
        "p2_id": callback.from_user.id,
        "p2_name": callback.from_user.first_name,
        "board": [""] * 9,
        "turn": game["p1_id"],  # p1 ходит крестиком
    })

    await callback.message.edit_text(
        f"❌ {game['p1_name']} 🆚 ⭕ {game['p2_name']}\n\n"
        f"Ходит: <b>{game['p1_name']}</b> (❌)",
        reply_markup=render_board(game["board"], chat_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ttt:move:"))
async def ttt_move(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    game = pending.get(chat_id)
    if not game or "board" not in game:
        await callback.answer("Игра не найдена", show_alert=True)
        return

    uid = callback.from_user.id
    if uid not in (game["p1_id"], game["p2_id"]):
        await callback.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    if uid != game["turn"]:
        await callback.answer("Сейчас не твой ход", show_alert=True)
        return

    idx = int(callback.data.split(":")[2])
    if game["board"][idx]:
        await callback.answer("Клетка занята", show_alert=True)
        return

    symbol = "❌" if uid == game["p1_id"] else "⭕"
    game["board"][idx] = symbol

    winner_symbol = check_winner(game["board"])
    is_draw = all(game["board"]) and not winner_symbol

    if winner_symbol:
        winner_name = game["p1_name"] if winner_symbol == "❌" else game["p2_name"]
        winner_id = game["p1_id"] if winner_symbol == "❌" else game["p2_id"]
        await callback.message.edit_text(
            f"❌⭕ <b>Игра окончена!</b>\n\n🏆 Победитель: <b>{winner_name}</b>",
            reply_markup=render_board(game["board"], chat_id),
        )
        await log_game_result("tictactoe", chat_id, winner_id)
        del pending[chat_id]
    elif is_draw:
        await callback.message.edit_text(
            "❌⭕ <b>Ничья!</b>",
            reply_markup=render_board(game["board"], chat_id),
        )
        await log_game_result("tictactoe", chat_id, None)
        del pending[chat_id]
    else:
        game["turn"] = game["p2_id"] if uid == game["p1_id"] else game["p1_id"]
        next_name = game["p1_name"] if game["turn"] == game["p1_id"] else game["p2_name"]
        next_symbol = "❌" if game["turn"] == game["p1_id"] else "⭕"
        await callback.message.edit_text(
            f"❌ {game['p1_name']} 🆚 ⭕ {game['p2_name']}\n\n"
            f"Ходит: <b>{next_name}</b> ({next_symbol})",
            reply_markup=render_board(game["board"], chat_id),
        )

    await callback.answer()
