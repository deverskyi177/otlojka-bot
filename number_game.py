import random
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db.database import log_game_result
from keyboards.main_menu import games_menu_kb

router = Router()

# Состояние игр в памяти: ключ — chat_id, значение — данные игры
# Для простого продакшена SQLite-персистентность не нужна — игры короткие,
# но если хочешь переживать рестарт бота, можно перенести в БД.
active_games: dict[int, dict] = {}


def number_start_kb(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Против бота", callback_data=f"num:new:bot")],
        [InlineKeyboardButton(text="👥 Против игрока", callback_data=f"num:new:player")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")],
    ])


def number_play_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬇️ Меньше", callback_data="num:guess:low"),
            InlineKeyboardButton(text="⬆️ Больше", callback_data="num:guess:high"),
        ],
        [InlineKeyboardButton(text="🎯 Угадать точно", callback_data="num:guess:exact")],
    ])


@router.callback_query(F.data == "game:number")
async def number_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔢 <b>Игра «Число»</b>\n\n"
        "Один игрок загадывает число от 1 до 100, остальные пытаются угадать "
        "с помощью подсказок «больше» / «меньше».\n\n"
        "С кем играем?",
        reply_markup=number_start_kb("choose"),
    )
    await callback.answer()


@router.callback_query(F.data == "num:new:bot")
async def number_vs_bot(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    secret = random.randint(1, 100)
    active_games[chat_id] = {
        "mode": "bot",
        "secret": secret,
        "guesser": callback.from_user.id,
        "low": 1,
        "high": 100,
        "attempts": 0,
    }
    await callback.message.edit_text(
        "🔢 <b>Число загадано ботом (1–100)</b>\n\n"
        f"{callback.from_user.first_name}, попробуй угадать!\n"
        "Напиши число сообщением в чат.",
    )
    await callback.answer()


@router.callback_query(F.data == "num:new:player")
async def number_vs_player(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    active_games[chat_id] = {
        "mode": "player",
        "secret": None,
        "setter": callback.from_user.id,
        "attempts": 0,
    }
    await callback.message.edit_text(
        f"🔢 <b>{callback.from_user.first_name} загадывает число от 1 до 100</b>\n\n"
        "Отправь его мне в личные сообщения боту — остальные не увидят.\n\n"
        "⚠️ Внимание: чтобы число осталось в секрете, загадай его в ЛС боту "
        "командой <code>/setnumber 42</code>, затем возвращайся в чат — "
        "игроки будут угадывать здесь.",
    )
    await callback.answer()


@router.message(F.text.regexp(r"^/setnumber\s+\d+$"))
async def set_number_private(message):
    if message.chat.type != "private":
        return
    value = int(message.text.split()[1])
    if not (1 <= value <= 100):
        await message.reply("⚠️ Число должно быть от 1 до 100.")
        return
    # В реальном проекте здесь нужно сопоставление user -> group chat_id,
    # которое запрашивается на предыдущем шаге. Для MVP сохраняем per-user.
    active_games.setdefault("_pending_secrets", {})
    active_games["_pending_secrets"][message.from_user.id] = value
    await message.reply(f"✅ Число {value} сохранено в секрете. Возвращайся в чат!")


@router.message(F.text.regexp(r"^\d+$"))
async def number_guess_message(message):
    chat_id = message.chat.id
    game = active_games.get(chat_id)
    if not game or game.get("mode") != "bot":
        return

    guess = int(message.text)
    game["attempts"] += 1
    secret = game["secret"]

    if guess == secret:
        winner = message.from_user
        await message.reply(
            f"🎉 <b>{winner.first_name} угадал(а) число {secret}!</b>\n"
            f"Попыток: {game['attempts']}"
        )
        await log_game_result("number", chat_id, winner.id)
        del active_games[chat_id]
    elif guess < secret:
        await message.reply("⬆️ Больше!")
    else:
        await message.reply("⬇️ Меньше!")
