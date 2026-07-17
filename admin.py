from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_ID
from db.database import get_stats_summary, get_setting, set_setting_value, DEFAULT_TEXTS, DEFAULT_EMOJI

router = Router()


class AdminEdit(StatesGroup):
    waiting_text_value = State()
    waiting_emoji_value = State()


def admin_only(func):
    async def wrapper(event, *args, **kwargs):
        user_id = event.from_user.id
        if user_id != ADMIN_ID:
            return
        return await func(event, *args, **kwargs)
    return wrapper


def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="✏️ Тексты и кнопки", callback_data="adm:texts")],
        [InlineKeyboardButton(text="🎨 Эмодзи", callback_data="adm:emoji")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="adm:close")],
    ])


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return  # молчим — панель секретная, никто не должен знать о её существовании
    await message.answer(
        "🔐 <b>Админ-панель</b>\n\nВыбери раздел:",
        reply_markup=admin_main_kb(),
    )


@router.callback_query(F.data == "adm:close")
async def adm_close(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    stats = await get_stats_summary()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:back")],
    ])
    await callback.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: {stats['users']}\n"
        f"💬 Чатов: {stats['chats']}\n"
        f"🎮 Сыграно игр: {stats['games']}",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "adm:back")
async def adm_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.message.edit_text(
        "🔐 <b>Админ-панель</b>\n\nВыбери раздел:",
        reply_markup=admin_main_kb(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Редактирование текстов и кнопок
# ---------------------------------------------------------------------------
FIELD_LABELS = {
    "welcome": "Текст приветствия (/start)",
    "farewell": "Текст прощания",
    "help": "Текст помощи",
    "btn_games": "Кнопка «Игры»",
    "btn_help": "Кнопка «Помощь»",
    "btn_feedback": "Кнопка «Обратная связь»",
    "btn_back": "Кнопка «Назад»",
    "btn_reminders": "Кнопка «Напоминания»",
}


def texts_list_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"adm:text:{key}")]
        for key, label in FIELD_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm:texts")
async def adm_texts(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.message.edit_text(
        "✏️ <b>Тексты и кнопки</b>\n\nВыбери, что изменить:",
        reply_markup=texts_list_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:text:"))
async def adm_text_field(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    field = callback.data.split(":")[2]
    texts = await get_setting("texts")
    current = texts.get(field, DEFAULT_TEXTS.get(field, ""))

    await state.update_data(field=field)
    await state.set_state(AdminEdit.waiting_text_value)

    await callback.message.edit_text(
        f"✏️ <b>{FIELD_LABELS.get(field, field)}</b>\n\n"
        f"Текущее значение:\n<code>{current}</code>\n\n"
        "Пришли новое значение сообщением. HTML-теги (например &lt;b&gt;) поддерживаются.\n"
        "Команда /cancel — отмена."
    )
    await callback.answer()


@router.message(AdminEdit.waiting_text_value)
async def adm_text_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb())
        return

    data = await state.get_data()
    field = data["field"]
    await set_setting_value("texts", field, message.text)
    await state.clear()
    await message.answer(
        f"✅ Значение «{FIELD_LABELS.get(field, field)}» обновлено.",
        reply_markup=admin_main_kb(),
    )


# ---------------------------------------------------------------------------
# Редактирование эмодзи (обычных и премиум)
# ---------------------------------------------------------------------------
EMOJI_LABELS = {
    "number": "Число",
    "mafia": "Мафия",
    "roulette": "Рулетка",
    "dice": "Кости",
    "rps": "КНБ",
    "hangman": "Виселица",
    "bulls_cows": "Быки и коровы",
    "reaction": "На реакцию",
    "tictactoe": "Крестики-нолики",
    "wheel": "Колесо фортуны",
    "back": "Кнопка Назад",
}


def emoji_list_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"adm:emo:{key}")]
        for key, label in EMOJI_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm:emoji")
async def adm_emoji(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.message.edit_text(
        "🎨 <b>Эмодзи кнопок</b>\n\n"
        "Выбери, для какой игры сменить эмодзи.\n\n"
        "⚠️ Важно: Telegram технически не позволяет ставить премиум-эмодзи "
        "(анимированные, из наборов) на inline-кнопки — это ограничение API, "
        "не бота. На кнопках можно использовать только обычные юникод-эмодзи. "
        "Премиум-эмодзи можно вставлять в тексты сообщений (приветствие, помощь) — "
        "там они отображаются корректно.",
        reply_markup=emoji_list_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:emo:"))
async def adm_emoji_field(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    field = callback.data.split(":")[2]
    emoji = await get_setting("emoji")
    current = emoji.get(field, DEFAULT_EMOJI.get(field, ""))

    await state.update_data(field=field)
    await state.set_state(AdminEdit.waiting_emoji_value)

    await callback.message.edit_text(
        f"🎨 <b>{EMOJI_LABELS.get(field, field)}</b>\n\n"
        f"Текущий эмодзи: {current}\n\n"
        "Пришли новый эмодзи сообщением (обычный или премиум).\n"
        "Команда /cancel — отмена."
    )
    await callback.answer()


@router.message(AdminEdit.waiting_emoji_value)
async def adm_emoji_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb())
        return

    data = await state.get_data()
    field = data["field"]

    # Если это премиум-эмодзи, в message.entities будет entity типа custom_emoji —
    # aiogram сохранит его как обычный текст-плейсхолдер, этого достаточно для
    # отображения в кнопках (Bot API рендерит custom emoji по entity в тексте
    # сообщений, но inline-кнопки поддерживают только текст/юникод-эмодзи).
    await set_setting_value("emoji", field, message.text)
    await state.clear()
    await message.answer(
        f"✅ Эмодзи «{EMOJI_LABELS.get(field, field)}» обновлён.",
        reply_markup=admin_main_kb(),
    )
