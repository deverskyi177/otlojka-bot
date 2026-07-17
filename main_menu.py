from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.database import get_text, get_emoji


async def main_menu_kb() -> InlineKeyboardMarkup:
    games_btn = await get_text("btn_games")
    help_btn = await get_text("btn_help")
    feedback_btn = await get_text("btn_feedback")
    reminders_btn = await get_text("btn_reminders")

    kb = [
        [InlineKeyboardButton(text=games_btn, callback_data="menu:games")],
        [
            InlineKeyboardButton(text=reminders_btn, callback_data="menu:reminders"),
            InlineKeyboardButton(text=help_btn, callback_data="menu:help"),
        ],
        [InlineKeyboardButton(text=feedback_btn, callback_data="menu:feedback")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def games_menu_kb() -> InlineKeyboardMarkup:
    e = lambda k: None  # placeholder, заменяется ниже
    emoji = {}
    for key in [
        "number", "mafia", "roulette", "dice", "rps",
        "hangman", "bulls_cows", "reaction", "tictactoe", "wheel",
    ]:
        emoji[key] = await get_emoji(key)

    back_emoji = await get_emoji("back")
    back_btn = await get_text("btn_back")

    kb = [
        [
            InlineKeyboardButton(text=f"{emoji['number']} Число", callback_data="game:number"),
            InlineKeyboardButton(text=f"{emoji['mafia']} Мафия", callback_data="game:mafia"),
        ],
        [
            InlineKeyboardButton(text=f"{emoji['roulette']} Рулетка", callback_data="game:roulette"),
            InlineKeyboardButton(text=f"{emoji['dice']} Кости", callback_data="game:dice"),
        ],
        [
            InlineKeyboardButton(text=f"{emoji['rps']} КНБ", callback_data="game:rps"),
            InlineKeyboardButton(text=f"{emoji['hangman']} Виселица", callback_data="game:hangman"),
        ],
        [
            InlineKeyboardButton(text=f"{emoji['bulls_cows']} Быки и коровы", callback_data="game:bulls_cows"),
            InlineKeyboardButton(text=f"{emoji['reaction']} На реакцию", callback_data="game:reaction"),
        ],
        [
            InlineKeyboardButton(text=f"{emoji['tictactoe']} Крестики-нолики", callback_data="game:tictactoe"),
            InlineKeyboardButton(text=f"{emoji['wheel']} Колесо фортуны", callback_data="game:wheel"),
        ],
        [InlineKeyboardButton(text=f"{back_emoji} {back_btn}", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def back_to_main_kb() -> InlineKeyboardMarkup:
    back_emoji = await get_emoji("back")
    back_btn = await get_text("btn_back")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{back_emoji} {back_btn}", callback_data="menu:main")]
    ])
