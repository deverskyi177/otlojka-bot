import random
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

from db.database import log_game_result

router = Router()

WORDS = [
    "телеграм", "питон", "ракета", "клавиатура", "гитара",
    "вертолет", "мороженое", "шоколад", "велосипед", "фонарик",
    "апельсин", "дракон", "космос", "пиратство", "холодильник",
]

active: dict[int, dict] = {}  # chat_id -> {"word", "guessed": set(), "wrong": int, "max_wrong": int}

MAX_WRONG = 6
STAGES = ["🙂", "😐", "😟", "😨", "😰", "😵", "💀"]


def render_word(word: str, guessed: set) -> str:
    return " ".join(c if c in guessed else "▁" for c in word)


@router.callback_query(F.data == "game:hangman")
async def hangman_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    word = random.choice(WORDS)
    active[chat_id] = {"word": word, "guessed": set(), "wrong": 0}

    await callback.message.edit_text(
        "📝 <b>Виселица</b>\n\n"
        f"Слово: {render_word(word, set())}\n"
        f"Состояние: {STAGES[0]}\n"
        f"Ошибок: 0/{MAX_WRONG}\n\n"
        "Пишите буквы в чат по одной!",
    )
    await callback.answer()


@router.message(F.text.regexp(r"^[а-яёa-z]$"))
async def hangman_letter(message: Message):
    chat_id = message.chat.id
    game = active.get(chat_id)
    if not game:
        return

    letter = message.text.lower()
    word = game["word"]

    if letter in game["guessed"]:
        await message.reply("Эта буква уже была 🙂")
        return

    game["guessed"].add(letter)

    if letter not in word:
        game["wrong"] += 1

    stage_idx = min(game["wrong"], MAX_WRONG)
    display = render_word(word, game["guessed"])

    if all(c in game["guessed"] for c in word):
        await message.reply(
            f"🎉 <b>Слово отгадано: {word}!</b>\n"
            f"Последнюю букву назвал(а): {message.from_user.first_name}"
        )
        await log_game_result("hangman", chat_id, message.from_user.id)
        del active[chat_id]
    elif game["wrong"] >= MAX_WRONG:
        await message.reply(
            f"💀 <b>Поражение! Слово было: {word}</b>"
        )
        await log_game_result("hangman", chat_id, None)
        del active[chat_id]
    else:
        await message.reply(
            f"Слово: {display}\n"
            f"Состояние: {STAGES[stage_idx]}\n"
            f"Ошибок: {game['wrong']}/{MAX_WRONG}"
        )
