import random
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

from db.database import log_game_result

router = Router()

active: dict[int, dict] = {}  # chat_id -> {"secret": "1234", "attempts": 0}


def generate_secret(length: int = 4) -> str:
    digits = list("0123456789")
    random.shuffle(digits)
    return "".join(digits[:length])


def evaluate(secret: str, guess: str) -> tuple[int, int]:
    bulls = sum(1 for s, g in zip(secret, guess) if s == g)
    cows = sum(1 for g in guess if g in secret) - bulls
    return bulls, cows


@router.callback_query(F.data == "game:bulls_cows")
async def bulls_cows_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    active[chat_id] = {"secret": generate_secret(), "attempts": 0}
    await callback.message.edit_text(
        "🐮 <b>Быки и коровы</b>\n\n"
        "Я загадал число из 4 разных цифр (0-9).\n"
        "Пиши свой вариант в чат, например: <code>1234</code>\n\n"
        "🎯 Бык — цифра на своём месте\n"
        "🔄 Корова — цифра есть, но не на своём месте",
    )
    await callback.answer()


@router.message(F.text.regexp(r"^\d{4}$"))
async def bulls_cows_guess(message: Message):
    chat_id = message.chat.id
    game = active.get(chat_id)
    if not game:
        return

    guess = message.text
    if len(set(guess)) != 4:
        await message.reply("⚠️ Все 4 цифры должны быть разными.")
        return

    game["attempts"] += 1
    bulls, cows = evaluate(game["secret"], guess)

    if bulls == 4:
        await message.reply(
            f"🎉 <b>{message.from_user.first_name} угадал(а)!</b>\n"
            f"Число: {game['secret']}, попыток: {game['attempts']}"
        )
        await log_game_result("bulls_cows", chat_id, message.from_user.id)
        del active[chat_id]
    else:
        await message.reply(f"🎯 Быков: {bulls} | 🔄 Коров: {cows}")
