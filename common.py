from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery

from config import FEEDBACK_USERNAME
from db.database import upsert_user, upsert_chat, get_text
from keyboards.main_menu import main_menu_kb, games_menu_kb, back_to_main_kb

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    await upsert_user(user.id, user.username or "", user.first_name or "", user.is_premium or False)
    if message.chat.type in ("group", "supergroup"):
        await upsert_chat(message.chat.id, message.chat.title or "")

    text = await get_text("welcome")
    await message.answer(text, reply_markup=await main_menu_kb())


@router.callback_query(F.data == "menu:main")
async def cb_menu_main(callback: CallbackQuery):
    text = await get_text("welcome")
    await callback.message.edit_text(text, reply_markup=await main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:games")
async def cb_menu_games(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎮 <b>Выбери игру</b>\n\nНажми на кнопку, чтобы запустить игру в этом чате.",
        reply_markup=await games_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def cb_menu_help(callback: CallbackQuery):
    text = await get_text("help")
    await callback.message.edit_text(text, reply_markup=await back_to_main_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:feedback")
async def cb_menu_feedback(callback: CallbackQuery):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Написать", url=f"https://t.me/{FEEDBACK_USERNAME}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
    ])
    await callback.message.edit_text(
        "💬 <b>Обратная связь</b>\n\n"
        "Нашёл баг, хочешь предложить новую игру или есть вопрос?\n"
        "Пиши напрямую — обязательно отвечу.",
        reply_markup=kb,
    )
    await callback.answer()
