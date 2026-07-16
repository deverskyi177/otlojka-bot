# -*- coding: utf-8 -*-
"""
Sialens — Telegram-бот для модерации и управления чатом.
aiogram 3.x, SQLite (aiosqlite), Gemini API, Telegram Stars.
Один файл — весь функционал бота.
"""

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatPermissions, PreCheckoutQuery, LabeledPrice,
    ChatMemberUpdated, ContentType,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

try:
    import google.generativeai as genai
except ImportError:
    genai = None

# ============================================================
#  КОНФИГ
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}

DATA_DIR = "data"
LOG_DIR = "logs"
DB_PATH = f"{DATA_DIR}/sialens.db"
LOG_PATH = f"{LOG_DIR}/bot.log"

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sialens")

if GEMINI_API_KEY and genai:
    genai.configure(api_key=GEMINI_API_KEY)
    GEMINI_MODEL = genai.GenerativeModel("gemini-1.5-flash")
else:
    GEMINI_MODEL = None
    log.warning("GEMINI_API_KEY не задан — команда .ai будет недоступна")

# Пороги ролей по XP
XP_VERIFIED = 50
XP_PER_MESSAGE = 1

# Игровые/фильтрационные константы
CAPS_THRESHOLD = 0.70
FLOOD_MSG_COUNT = 3
FLOOD_WINDOW_SEC = 5
FLOOD_MUTE_MINUTES = 10
WARN_LIMIT = 3
WARN_BAN_HOURS = 24

DICE_EMOJI = {
    "1": "🎲",
    "2": "🎯",
    "3": "🏀",
    "4": "⚽",
    "5": "🎳",
}

MUTE_DURATIONS = {
    "1ч": timedelta(hours=1),
    "6ч": timedelta(hours=6),
    "12ч": timedelta(hours=12),
    "24ч": timedelta(hours=24),
    "навсегда": None,
}

# In-memory состояние (не критично для персистентности между рестартами)
flood_tracker: dict[tuple[int, int], list[float]] = {}
ai_context: dict[int, list[dict]] = {}          # chat_id -> [{"role":..,"content":..}]
guess_games: dict[int, int] = {}                # chat_id -> загаданное число
recent_messages: dict[int, list[int]] = {}      # chat_id -> [message_id,...] для .clean

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ============================================================
#  БАЗА ДАННЫХ
# ============================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS chat_users (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            xp INTEGER DEFAULT 0,
            messages_count INTEGER DEFAULT 0,
            warns INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            muted_until TEXT,
            premium_until TEXT,
            trial_used INTEGER DEFAULT 0,
            joined_at TEXT,
            last_active TEXT,
            night_msgs INTEGER DEFAULT 0,
            day_msgs INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS bot_admins (
            user_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            caps_filter INTEGER DEFAULT 1,
            link_filter INTEGER DEFAULT 1,
            spam_filter INTEGER DEFAULT 1,
            flood_filter INTEGER DEFAULT 1,
            profanity_filter INTEGER DEFAULT 0,
            sticker_gif_filter INTEGER DEFAULT 0,
            welcome_enabled INTEGER DEFAULT 1,
            price_stars INTEGER DEFAULT 50,
            trial_days INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            key TEXT,
            response TEXT
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            text TEXT,
            remind_at TEXT,
            done INTEGER DEFAULT 0
        );
        """)
        await db.commit()

    # добавляем ADMIN_IDS из .env в таблицу bot_admins
    async with aiosqlite.connect(DB_PATH) as db:
        for uid in ADMIN_IDS:
            await db.execute(
                "INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (uid,)
            )
        await db.commit()
    log.info("База данных инициализирована: %s", DB_PATH)


async def ensure_chat_settings(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.commit()


async def get_chat_settings(chat_id: int) -> dict:
    await ensure_chat_settings(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return dict(row)


async def set_chat_setting(chat_id: int, field: str, value):
    await ensure_chat_settings(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE chat_settings SET {field}=? WHERE chat_id=?", (value, chat_id))
        await db.commit()


async def get_or_create_user(chat_id: int, user_id: int, username: str = "", first_name: str = "") -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM chat_users WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        row = await cur.fetchone()
        if row:
            if username or first_name:
                await db.execute(
                    "UPDATE chat_users SET username=?, first_name=? WHERE chat_id=? AND user_id=?",
                    (username, first_name, chat_id, user_id),
                )
                await db.commit()
            return dict(row)
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO chat_users (chat_id, user_id, username, first_name, joined_at, last_active)
               VALUES (?,?,?,?,?,?)""",
            (chat_id, user_id, username, first_name, now, now),
        )
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM chat_users WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        return dict(await cur.fetchone())


async def update_user_field(chat_id: int, user_id: int, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [chat_id, user_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE chat_users SET {sets} WHERE chat_id=? AND user_id=?", values
        )
        await db.commit()


async def is_bot_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM bot_admins WHERE user_id=?", (user_id,))
        return (await cur.fetchone()) is not None


async def is_premium(chat_id: int, user_id: int) -> bool:
    user = await get_or_create_user(chat_id, user_id)
    if not user["premium_until"]:
        return False
    try:
        return datetime.fromisoformat(user["premium_until"]) > datetime.utcnow()
    except ValueError:
        return False


def calc_role(user: dict) -> str:
    if user.get("xp", 0) >= XP_VERIFIED:
        return "проверенный"
    return "новичок"


async def get_role_label(chat_id: int, user_id: int) -> str:
    if await is_bot_admin(user_id):
        return "админ"
    if await is_premium(chat_id, user_id):
        return "VIP"
    user = await get_or_create_user(chat_id, user_id)
    return calc_role(user)


# ============================================================
#  УТИЛИТЫ
# ============================================================

LAYOUT_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,./QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
LAYOUT_RU = "йцукенгшщзхъфывапролджэячсмитьбю.ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"


def switch_layout(text: str) -> str:
    table_en_ru = str.maketrans(LAYOUT_EN, LAYOUT_RU)
    table_ru_en = str.maketrans(LAYOUT_RU, LAYOUT_EN)
    ru_chars = sum(1 for c in text if c in LAYOUT_RU)
    en_chars = sum(1 for c in text if c in LAYOUT_EN)
    if ru_chars >= en_chars:
        return text.translate(table_ru_en)
    return text.translate(table_en_ru)


def parse_args(text: str) -> list[str]:
    """Разбивает строку на аргументы, поддерживая "кавычки" для фраз."""
    return re.findall(r'"([^"]+)"|(\S+)', text)


def parse_quoted(text: str) -> list[str]:
    return re.findall(r'"([^"]+)"', text)


async def get_target_user_id(message: Message) -> tuple[int | None, str | None]:
    """Определяет id пользователя-цели: по ответу на сообщение или по @username в тексте."""
    if message.reply_to_message:
        u = message.reply_to_message.from_user
        return u.id, (u.username or u.first_name)
    parts = message.text.split()
    for p in parts[1:]:
        if p.startswith("@"):
            return None, p  # username без id — резолвится через get_chat_member не всегда возможно
        if p.isdigit():
            return int(p), None
    return None, None


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")


async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    if await is_bot_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except TelegramBadRequest:
        return False


# ============================================================
#  КЛАВИАТУРЫ
# ============================================================

def kb_admin_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛡 Фильтры", callback_data="adm:filters")
    b.button(text="📊 Статистика чата", callback_data="adm:stats")
    b.button(text="💎 Подписка", callback_data="adm:premium")
    b.button(text="🧩 Триггеры", callback_data="adm:triggers")
    b.button(text="⚙️ Прочее", callback_data="adm:other")
    b.adjust(2, 2, 1)
    return b.as_markup()


def kb_filters(settings: dict) -> InlineKeyboardMarkup:
    def mark(v):
        return "✅" if v else "❌"
    b = InlineKeyboardBuilder()
    b.button(text=f"{mark(settings['caps_filter'])} Капс", callback_data="flt:caps_filter")
    b.button(text=f"{mark(settings['link_filter'])} Ссылки", callback_data="flt:link_filter")
    b.button(text=f"{mark(settings['spam_filter'])} Флуд/спам", callback_data="flt:spam_filter")
    b.button(text=f"{mark(settings['profanity_filter'])} Мат (.wbl)", callback_data="flt:profanity_filter")
    b.button(text=f"{mark(settings['sticker_gif_filter'])} Стикеры/гифки (.wsag)", callback_data="flt:sticker_gif_filter")
    b.button(text=f"{mark(settings['welcome_enabled'])} Приветствие", callback_data="flt:welcome_enabled")
    b.button(text="⬅️ Назад", callback_data="adm:main")
    b.adjust(1)
    return b.as_markup()


def kb_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="adm:main")
    return b.as_markup()


def kb_premium_buy(price: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"💫 Купить VIP за {price} ⭐", callback_data="buy:premium")
    return b.as_markup()


# ============================================================
#  MIDDLEWARE: активность, роли, баны, муты
# ============================================================

@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def track_activity(message: Message):
    """Обновление активности, проверка бана/мута — выполняется первым для каждого сообщения в группе."""
    if not message.from_user or message.from_user.is_bot:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    user = await get_or_create_user(
        chat_id, user_id, message.from_user.username or "", message.from_user.first_name or ""
    )

    # бан
    if user["is_banned"]:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # мут
    if user["muted_until"]:
        try:
            until = datetime.fromisoformat(user["muted_until"])
            if until > datetime.utcnow() or user["muted_until"] == "forever":
                try:
                    await message.delete()
                except TelegramBadRequest:
                    pass
                return
        except ValueError:
            if user["muted_until"] == "forever":
                try:
                    await message.delete()
                except TelegramBadRequest:
                    pass
                return

    # трекинг для .clean
    recent_messages.setdefault(chat_id, []).append(message.message_id)
    if len(recent_messages[chat_id]) > 300:
        recent_messages[chat_id] = recent_messages[chat_id][-300:]

    # активность/xp/день-ночь
    now = datetime.utcnow()
    hour = now.hour
    night = 1 if (hour >= 23 or hour < 6) else 0
    fields = {
        "xp": user["xp"] + XP_PER_MESSAGE,
        "messages_count": user["messages_count"] + 1,
        "last_active": now.isoformat(),
    }
    if night:
        fields["night_msgs"] = user["night_msgs"] + 1
    else:
        fields["day_msgs"] = user["day_msgs"] + 1
    await update_user_field(chat_id, user_id, **fields)

    # не фильтруем команды и админов
    text = message.text or message.caption or ""
    is_admin_user = await is_chat_admin(chat_id, user_id)

    if text.startswith("."):
        await handle_dot_command(message, text)
        return

    if not is_admin_user:
        settings = await get_chat_settings(chat_id)
        triggered = await apply_filters(message, text, settings, user)
        if triggered:
            return
        await check_triggers(message, text)

    # ИИ-контекст (последние 15 сообщений)
    ctx = ai_context.setdefault(chat_id, [])
    if text:
        ctx.append({"role": "user", "content": f"{message.from_user.first_name}: {text}"})
        ai_context[chat_id] = ctx[-15:]


# ============================================================
#  ФИЛЬТРЫ И АВТО-МОДЕРАЦИЯ
# ============================================================

LINK_RE = re.compile(r"(https?://|t\.me/|www\.)\S+", re.IGNORECASE)
PROFANITY_WORDS = ["бляд", "хуй", "пизд", "ебат", "ебал", "сука", "пидор", "мудак"]


async def apply_filters(message: Message, text: str, settings: dict, user: dict) -> bool:
    chat_id = message.chat.id
    user_id = message.from_user.id

    if settings["sticker_gif_filter"] and message.content_type in (
        ContentType.STICKER, ContentType.ANIMATION,
    ):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return True

    if not text:
        return False

    if settings["profanity_filter"]:
        low = text.lower()
        if any(w in low for w in PROFANITY_WORDS):
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
            await add_warn(chat_id, user_id, message, reason="нецензурная лексика")
            return True

    if settings["caps_filter"] and len(text) >= 10:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if caps_ratio > CAPS_THRESHOLD:
                try:
                    await message.delete()
                except TelegramBadRequest:
                    pass
                await message.answer(
                    f"⚠️ {message.from_user.first_name}, пожалуйста, не пишите капсом."
                )
                return True

    if settings["link_filter"] and LINK_RE.search(text):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await message.answer(
            f"⚠️ {message.from_user.first_name}, ссылки в этом чате запрещены."
        )
        return True

    if settings["spam_filter"]:
        key = (chat_id, user_id)
        now_ts = datetime.utcnow().timestamp()
        history = flood_tracker.setdefault(key, [])
        history.append(now_ts)
        flood_tracker[key] = [t for t in history if now_ts - t <= FLOOD_WINDOW_SEC]
        if len(flood_tracker[key]) >= FLOOD_MSG_COUNT:
            flood_tracker[key] = []
            until = datetime.utcnow() + timedelta(minutes=FLOOD_MUTE_MINUTES)
            await mute_user(chat_id, user_id, until)
            await message.answer(
                f"🔇 {message.from_user.first_name} отправлен в мут на {FLOOD_MUTE_MINUTES} мин. за флуд."
            )
            return True

    return False


async def add_warn(chat_id: int, user_id: int, message: Message, reason: str = ""):
    user = await get_or_create_user(chat_id, user_id)
    warns = user["warns"] + 1
    await update_user_field(chat_id, user_id, warns=warns)
    if warns >= WARN_LIMIT:
        until = datetime.utcnow() + timedelta(hours=WARN_BAN_HOURS)
        await mute_user(chat_id, user_id, until)
        await update_user_field(chat_id, user_id, warns=0)
        await message.answer(
            f"🚫 {user['first_name'] or user_id} получил(а) {WARN_LIMIT}-й варн и забанен(а) на {WARN_BAN_HOURS}ч."
        )
    else:
        await message.answer(
            f"⚠️ Предупреждение {warns}/{WARN_LIMIT} для {user['first_name'] or user_id}. Причина: {reason or 'нарушение правил'}."
        )


async def mute_user(chat_id: int, user_id: int, until: datetime | None):
    try:
        if until:
            await bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            await update_user_field(chat_id, user_id, muted_until=until.isoformat())
        else:
            await bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
            await update_user_field(chat_id, user_id, muted_until="forever")
    except TelegramBadRequest as e:
        log.warning("Не удалось замутить %s в %s: %s", user_id, chat_id, e)


async def unmute_user(chat_id: int, user_id: int):
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_photos=True, can_send_videos=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
    except TelegramBadRequest as e:
        log.warning("Не удалось размутить %s в %s: %s", user_id, chat_id, e)
    await update_user_field(chat_id, user_id, muted_until=None)


# ============================================================
#  ТРИГГЕРЫ
# ============================================================

async def check_triggers(message: Message, text: str):
    chat_id = message.chat.id
    low = text.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT key, response FROM triggers WHERE chat_id=?", (chat_id,))
        rows = await cur.fetchall()
    for row in rows:
        if row["key"].lower() in low:
            await message.answer(row["response"])
            break


# ============================================================
#  ТОЧКА КОМАНД ".xxx"
# ============================================================

async def handle_dot_command(message: Message, text: str):
    chat_id = message.chat.id
    user_id = message.from_user.id
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].lower()
    rest = parts[1] if len(parts) > 1 else ""

    is_admin_user = await is_chat_admin(chat_id, user_id)

    # ---------- МОДЕРАЦИЯ (только админы чата) ----------
    if cmd == "mute":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_mute(message, rest)
    if cmd == "warn":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_warn(message, rest)
    if cmd == "ban":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_ban(message, rest)
    if cmd == "unban":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_unban(message, rest)
    if cmd == "kick":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_kick(message)
    if cmd == "clean":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_clean(message, rest)
    if cmd == "wbl":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_toggle_filter(message, "profanity_filter", "Фильтр мата")
    if cmd == "wsag":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_toggle_filter(message, "sticker_gif_filter", "Фильтр стикеров/гифок")

    # ---------- ИИ (платно) ----------
    if cmd == "ai":
        return await cmd_ai(message, rest)

    # ---------- ИГРЫ ----------
    if cmd == "revo":
        return await cmd_revo(message)
    if cmd == "anim":
        return await cmd_anim(message, rest)
    if cmd == "guess":
        return await cmd_guess(message, rest)
    if cmd == "rps":
        return await cmd_rps(message, rest)
    if cmd == "games":
        return await cmd_games(message)

    # ---------- АНАЛИТИКА (платно) ----------
    if cmd == "stats":
        return await cmd_stats(message)
    if cmd == "top":
        return await cmd_top(message)
    if cmd == "nightowl":
        return await cmd_nightowl(message)
    if cmd == "peak":
        return await cmd_peak(message)

    # ---------- ТРИГГЕРЫ (платно, только админы) ----------
    if cmd == "addtrigger":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_addtrigger(message, rest)
    if cmd == "removetrigger":
        if not is_admin_user:
            return await message.reply("⛔ Только для админов.")
        return await cmd_removetrigger(message, rest)
    if cmd == "trigger":
        return await cmd_trigger_list(message, rest)

    # ---------- ОПРОСЫ ----------
    if cmd == "poll":
        return await cmd_poll(message, rest)

    # ---------- НАПОМИНАНИЯ ----------
    if cmd == "remind":
        return await cmd_remind(message, rest)

    # ---------- РАСКЛАДКА ----------
    if cmd == "sw":
        return await cmd_switch(message, rest)


async def require_premium(message: Message) -> bool:
    chat_id, user_id = message.chat.id, message.from_user.id
    if await is_bot_admin(user_id) or await is_premium(chat_id, user_id):
        return True
    settings = await get_chat_settings(chat_id)
    await message.reply(
        f"💎 Эта функция доступна по подписке VIP ({settings['price_stars']} ⭐/мес). "
        f"Открой /admin → Подписка, чтобы оформить.",
        reply_markup=kb_premium_buy(settings["price_stars"]),
    )
    return False


# ---------- модерация ----------

async def cmd_mute(message: Message, rest: str):
    target_id, target_ref = await get_target_user_id(message)
    if not target_id:
        return await message.reply("Ответьте на сообщение пользователя командой .mute <1ч|6ч|12ч|24ч|навсегда>")
    duration_key = None
    for key in MUTE_DURATIONS:
        if key in rest:
            duration_key = key
            break
    if not duration_key:
        return await message.reply("Укажите срок: .mute 1ч / 6ч / 12ч / 24ч / навсегда")
    delta = MUTE_DURATIONS[duration_key]
    until = datetime.utcnow() + delta if delta else None
    await mute_user(message.chat.id, target_id, until)
    label = "навсегда" if not until else f"до {fmt_dt(until)}"
    await message.reply(f"🔇 Пользователь замучен {label}.")


async def cmd_warn(message: Message, rest: str):
    target_id, _ = await get_target_user_id(message)
    if not target_id:
        return await message.reply("Ответьте на сообщение пользователя командой .warn [причина]")
    await add_warn(message.chat.id, target_id, message, reason=rest)


async def cmd_ban(message: Message, rest: str):
    target_id, _ = await get_target_user_id(message)
    if not target_id:
        return await message.reply("Ответьте на сообщение пользователя командой .ban")
    try:
        await bot.ban_chat_member(message.chat.id, target_id)
    except TelegramBadRequest as e:
        return await message.reply(f"Не удалось забанить: {e}")
    await update_user_field(message.chat.id, target_id, is_banned=1)
    await message.reply("🚫 Пользователь забанен.")


async def cmd_unban(message: Message, rest: str):
    target_id, _ = await get_target_user_id(message)
    if not target_id and rest.strip().isdigit():
        target_id = int(rest.strip())
    if not target_id:
        return await message.reply("Укажите id или ответьте на сообщение: .unban <user_id>")
    try:
        await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
    except TelegramBadRequest as e:
        return await message.reply(f"Не удалось разбанить: {e}")
    await update_user_field(message.chat.id, target_id, is_banned=0, muted_until=None)
    await message.reply("✅ Пользователь разбанен.")


async def cmd_kick(message: Message):
    target_id, _ = await get_target_user_id(message)
    if not target_id:
        return await message.reply("Ответьте на сообщение пользователя командой .kick")
    try:
        await bot.ban_chat_member(message.chat.id, target_id)
        await bot.unban_chat_member(message.chat.id, target_id)
    except TelegramBadRequest as e:
        return await message.reply(f"Не удалось кикнуть: {e}")
    await message.reply("👢 Пользователь удалён из чата.")


async def cmd_clean(message: Message, rest: str):
    n = int(rest.strip()) if rest.strip().isdigit() else 20
    ids = recent_messages.get(message.chat.id, [])[-n:]
    deleted = 0
    for mid in ids:
        try:
            await bot.delete_message(message.chat.id, mid)
            deleted += 1
        except TelegramBadRequest:
            continue
    await message.answer(f"🧹 Удалено сообщений: {deleted}")


async def cmd_toggle_filter(message: Message, field: str, label: str):
    settings = await get_chat_settings(message.chat.id)
    new_val = 0 if settings[field] else 1
    await set_chat_setting(message.chat.id, field, new_val)
    state = "включён ✅" if new_val else "выключен ❌"
    await message.reply(f"{label} теперь {state}.")


# ---------- ИИ ----------

async def cmd_ai(message: Message, rest: str):
    if not await require_premium(message):
        return
    if not GEMINI_MODEL:
        return await message.reply("ИИ временно недоступен: не настроен GEMINI_API_KEY.")
    if not rest.strip():
        return await message.reply("Использование: .ai <вопрос>")

    chat_id = message.chat.id
    context = ai_context.get(chat_id, [])
    context_text = "\n".join(c["content"] for c in context[-15:])
    prompt = (
        "Ты — дружелюбный ассистент Telegram-чата Sialens. Отвечай кратко и по делу на русском.\n\n"
        f"Контекст последних сообщений чата:\n{context_text}\n\n"
        f"Вопрос пользователя {message.from_user.first_name}: {rest}"
    )
    await bot.send_chat_action(chat_id, "typing")
    try:
        response = await asyncio.to_thread(GEMINI_MODEL.generate_content, prompt)
        answer = response.text.strip() if response and response.text else "Не удалось получить ответ."
    except Exception as e:
        log.exception("Ошибка Gemini")
        answer = f"Ошибка при обращении к ИИ: {e}"
    await message.reply(answer[:4000])


# ---------- игры ----------

async def cmd_revo(message: Message):
    """Русская рулетка-игра: 1/6 шанс "проигрыша" (короткий мут на 5 минут в шутку)."""
    chat_id, user_id = message.chat.id, message.from_user.id
    if random.randint(1, 6) == 1:
        until = datetime.utcnow() + timedelta(minutes=5)
        await mute_user(chat_id, user_id, until)
        await message.reply("💥 БАБАХ! Не повезло — мут на 5 минут 😅")
    else:
        await message.reply("🔫 Клик... пронесло! Можно попробовать ещё раз.")


async def cmd_anim(message: Message, rest: str):
    n = rest.strip()
    emoji = DICE_EMOJI.get(n)
    if not emoji:
        return await message.reply("Использование: .anim 1-5 (1-🎲 2-🎯 3-🏀 4-⚽ 5-🎳)")
    await bot.send_dice(message.chat.id, emoji=emoji)


async def cmd_guess(message: Message, rest: str):
    chat_id = message.chat.id
    rest = rest.strip()
    if not rest:
        guess_games[chat_id] = random.randint(1, 100)
        return await message.reply("🎯 Я загадал число от 1 до 100. Угадывай: .guess <число>")
    if chat_id not in guess_games:
        return await message.reply("Сначала запустите игру: .guess")
    if not rest.isdigit():
        return await message.reply("Введите число: .guess <число>")
    guess = int(rest)
    target = guess_games[chat_id]
    if guess == target:
        del guess_games[chat_id]
        await message.reply(f"🎉 Угадал(а)! Число было {target}.")
    elif guess < target:
        await message.reply("⬆️ Больше!")
    else:
        await message.reply("⬇️ Меньше!")


async def cmd_rps(message: Message, rest: str):
    choices = {"камень": "🪨", "ножницы": "✂️", "бумага": "📄"}
    user_choice = rest.strip().lower()
    if user_choice not in choices:
        return await message.reply("Использование: .rps камень / ножницы / бумага")
    bot_choice = random.choice(list(choices.keys()))
    if user_choice == bot_choice:
        result = "🤝 Ничья!"
    elif (
        (user_choice == "камень" and bot_choice == "ножницы")
        or (user_choice == "ножницы" and bot_choice == "бумага")
        or (user_choice == "бумага" and bot_choice == "камень")
    ):
        result = "🎉 Ты выиграл(а)!"
    else:
        result = "😅 Бот выиграл!"
    await message.reply(
        f"Ты: {choices[user_choice]}  vs  Бот: {choices[bot_choice]}\n{result}"
    )


async def cmd_games(message: Message):
    text = (
        "🎮 <b>Доступные игры</b>\n\n"
        "• .revo — русская рулетка (шанс мута на 5 мин)\n"
        "• .anim 1-5 — анимированный бросок (кости/дартс/баскетбол/футбол/боулинг)\n"
        "• .guess — угадай число от 1 до 100\n"
        "• .rps камень|ножницы|бумага — камень-ножницы-бумага"
    )
    await message.answer(text)


# ---------- аналитика ----------

async def cmd_stats(message: Message):
    if not await require_premium(message):
        return
    chat_id, user_id = message.chat.id, message.from_user.id
    user = await get_or_create_user(chat_id, user_id)
    role = await get_role_label(chat_id, user_id)
    text = (
        f"📊 <b>Статистика {message.from_user.first_name}</b>\n\n"
        f"Роль: {role}\n"
        f"Сообщений: {user['messages_count']}\n"
        f"XP: {user['xp']}\n"
        f"Предупреждений: {user['warns']}\n"
        f"Дневных сообщений: {user['day_msgs']}\n"
        f"Ночных сообщений: {user['night_msgs']}\n"
        f"В чате с: {user['joined_at'][:10] if user['joined_at'] else '—'}"
    )
    await message.answer(text)


async def cmd_top(message: Message):
    if not await require_premium(message):
        return
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT first_name, username, xp FROM chat_users WHERE chat_id=? ORDER BY xp DESC LIMIT 10",
            (chat_id,),
        )
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("Пока нет данных.")
    lines = ["🏆 <b>Топ участников</b>\n"]
    for i, r in enumerate(rows, 1):
        name = r["first_name"] or r["username"] or "Без имени"
        lines.append(f"{i}. {name} — {r['xp']} XP")
    await message.answer("\n".join(lines))


async def cmd_nightowl(message: Message):
    if not await require_premium(message):
        return
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT first_name, username, night_msgs FROM chat_users WHERE chat_id=? ORDER BY night_msgs DESC LIMIT 10",
            (chat_id,),
        )
        rows = await cur.fetchall()
    if not rows or rows[0]["night_msgs"] == 0:
        return await message.answer("Пока нет ночной активности.")
    lines = ["🦉 <b>Топ 'сов' (23:00–06:00)</b>\n"]
    for i, r in enumerate(rows, 1):
        name = r["first_name"] or r["username"] or "Без имени"
        lines.append(f"{i}. {name} — {r['night_msgs']} сообщ.")
    await message.answer("\n".join(lines))


async def cmd_peak(message: Message):
    if not await require_premium(message):
        return
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT day_msgs, night_msgs FROM chat_users WHERE chat_id=?", (chat_id,)
        )
        rows = await cur.fetchall()
    day_total = sum(r["day_msgs"] for r in rows)
    night_total = sum(r["night_msgs"] for r in rows)
    peak = "день (06:00–23:00)" if day_total >= night_total else "ночь (23:00–06:00)"
    await message.answer(
        f"📈 <b>Пиковая активность чата</b>\n\n"
        f"Дневные сообщения: {day_total}\n"
        f"Ночные сообщения: {night_total}\n"
        f"Пик активности: {peak}"
    )


# ---------- триггеры ----------

async def cmd_addtrigger(message: Message, rest: str):
    if not await require_premium(message):
        return
    parts = parse_quoted(rest)
    if len(parts) < 2:
        return await message.reply('Использование: .addtrigger "ключ" "ответ"')
    key, response = parts[0], parts[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO triggers (chat_id, key, response) VALUES (?,?,?)",
            (message.chat.id, key, response),
        )
        await db.commit()
    await message.reply(f"✅ Триггер «{key}» добавлен.")


async def cmd_removetrigger(message: Message, rest: str):
    if not await require_premium(message):
        return
    parts = parse_quoted(rest)
    key = parts[0] if parts else rest.strip()
    if not key:
        return await message.reply('Использование: .removetrigger "ключ"')
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM triggers WHERE chat_id=? AND key=?", (message.chat.id, key)
        )
        await db.commit()
    await message.reply(f"🗑 Триггер «{key}» удалён (если существовал).")


async def cmd_trigger_list(message: Message, rest: str):
    if rest.strip().lower() != "list":
        return await message.reply("Использование: .trigger list")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT key FROM triggers WHERE chat_id=?", (message.chat.id,)
        )
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("Триггеров пока нет.")
    keys = ", ".join(f"«{r['key']}»" for r in rows)
    await message.answer(f"🧩 <b>Триггеры чата:</b>\n{keys}")


# ---------- опросы ----------

async def cmd_poll(message: Message, rest: str):
    parts = parse_quoted(rest)
    if len(parts) < 3:
        return await message.reply('Использование: .poll "вопрос" "вар1" "вар2" ...')
    question, options = parts[0], parts[1:10]
    await bot.send_poll(message.chat.id, question=question, options=options, is_anonymous=False)


# ---------- напоминания ----------

DURATION_RE = re.compile(r"(\d+)\s*([смчд])", re.IGNORECASE)


def parse_remind_duration(text: str) -> timedelta | None:
    m = DURATION_RE.search(text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return {
        "с": timedelta(seconds=n),
        "м": timedelta(minutes=n),
        "ч": timedelta(hours=n),
        "д": timedelta(days=n),
    }.get(unit)


async def cmd_remind(message: Message, rest: str):
    quoted = parse_quoted(rest)
    text_part = quoted[0] if quoted else rest
    delta = parse_remind_duration(rest)
    if not delta:
        return await message.reply('Использование: .remind "текст" 10м  (с/м/ч/д)')
    remind_at = datetime.utcnow() + delta
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reminders (chat_id, user_id, text, remind_at) VALUES (?,?,?,?)",
            (message.chat.id, message.from_user.id, text_part, remind_at.isoformat()),
        )
        await db.commit()
    await message.reply(f"⏰ Напомню через {rest.split()[-1]}: «{text_part}»")


async def reminders_loop():
    while True:
        try:
            now = datetime.utcnow().isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT * FROM reminders WHERE done=0 AND remind_at<=?", (now,)
                )
                rows = await cur.fetchall()
                for r in rows:
                    try:
                        await bot.send_message(
                            r["chat_id"],
                            f"⏰ <a href='tg://user?id={r['user_id']}'>Напоминание</a>: {r['text']}",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                    await db.execute("UPDATE reminders SET done=1 WHERE id=?", (r["id"],))
                await db.commit()
        except Exception:
            log.exception("Ошибка в reminders_loop")
        await asyncio.sleep(30)


# ---------- раскладка ----------

async def cmd_switch(message: Message, rest: str):
    source = rest.strip()
    if not source and message.reply_to_message:
        source = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not source:
        return await message.reply("Использование: .sw <текст> или ответом на сообщение")
    await message.reply(switch_layout(source))


# ============================================================
#  ПРИВЕТСТВИЕ / ПРОЩАНИЕ
# ============================================================

@router.message(F.new_chat_members)
async def on_new_member(message: Message):
    settings = await get_chat_settings(message.chat.id)
    if not settings["welcome_enabled"]:
        return
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        await get_or_create_user(message.chat.id, user.id, user.username or "", user.first_name or "")
        await message.answer(
            f"👋 Добро пожаловать, {user.first_name}! Ознакомьтесь с правилами чата. "
            f"Приятного общения в Sialens 🌌"
        )


@router.message(F.left_chat_member)
async def on_left_member(message: Message):
    settings = await get_chat_settings(message.chat.id)
    if not settings["welcome_enabled"]:
        return
    user = message.left_chat_member
    if user and not user.is_bot:
        await message.answer(f"👋 {user.first_name} покинул(а) чат.")


# ============================================================
#  ПОДПИСКА / TELEGRAM STARS
# ============================================================

async def start_trial_if_available(chat_id: int, user_id: int) -> bool:
    user = await get_or_create_user(chat_id, user_id)
    if user["trial_used"]:
        return False
    settings = await get_chat_settings(chat_id)
    until = datetime.utcnow() + timedelta(days=settings["trial_days"])
    await update_user_field(chat_id, user_id, premium_until=until.isoformat(), trial_used=1)
    return True


@router.callback_query(F.data == "buy:premium")
async def cb_buy_premium(callback: CallbackQuery):
    settings = await get_chat_settings(callback.message.chat.id)
    started_trial = await start_trial_if_available(callback.message.chat.id, callback.from_user.id)
    if started_trial:
        await callback.answer("Активирован бесплатный триал!", show_alert=True)
        return await callback.message.answer(
            f"🎁 Вам активирован пробный VIP на {settings['trial_days']} дн. Наслаждайтесь!"
        )
    prices = [LabeledPrice(label="VIP подписка (30 дней)", amount=settings["price_stars"])]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Sialens VIP",
        description="Доступ к ИИ, аналитике и триггерам на 30 дней.",
        payload=f"premium:{callback.message.chat.id}:{callback.from_user.id}",
        currency="XTR",
        prices=prices,
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    try:
        _, chat_id_s, user_id_s = payload.split(":")
        chat_id, user_id = int(chat_id_s), int(user_id_s)
    except ValueError:
        chat_id, user_id = message.chat.id, message.from_user.id
    user = await get_or_create_user(chat_id, user_id)
    base = datetime.utcnow()
    if user["premium_until"]:
        try:
            existing = datetime.fromisoformat(user["premium_until"])
            if existing > base:
                base = existing
        except ValueError:
            pass
    until = base + timedelta(days=30)
    await update_user_field(chat_id, user_id, premium_until=until.isoformat())
    await message.answer(f"✅ Оплата получена! VIP активен до {fmt_dt(until)}.")


# ============================================================
#  АДМИН-ПАНЕЛЬ /admin
# ============================================================

@router.message(Command("admin"))
async def cmd_admin_panel(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        return await message.answer("Команда /admin доступна только в группе.")
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        return await message.reply("⛔ Только для админов чата.")
    await message.answer("⚙️ <b>Панель управления Sialens</b>", reply_markup=kb_admin_main())


@router.callback_query(F.data == "adm:main")
async def cb_admin_main(callback: CallbackQuery):
    await callback.message.edit_text("⚙️ <b>Панель управления Sialens</b>", reply_markup=kb_admin_main())
    await callback.answer()


@router.callback_query(F.data == "adm:filters")
async def cb_admin_filters(callback: CallbackQuery):
    settings = await get_chat_settings(callback.message.chat.id)
    await callback.message.edit_text("🛡 <b>Фильтры чата</b>", reply_markup=kb_filters(settings))
    await callback.answer()


@router.callback_query(F.data.startswith("flt:"))
async def cb_toggle_filter(callback: CallbackQuery):
    if not await is_chat_admin(callback.message.chat.id, callback.from_user.id):
        return await callback.answer("Только для админов.", show_alert=True)
    field = callback.data.split(":")[1]
    settings = await get_chat_settings(callback.message.chat.id)
    new_val = 0 if settings[field] else 1
    await set_chat_setting(callback.message.chat.id, field, new_val)
    settings[field] = new_val
    await callback.message.edit_text("🛡 <b>Фильтры чата</b>", reply_markup=kb_filters(settings))
    await callback.answer("Изменено")


@router.callback_query(F.data == "adm:stats")
async def cb_admin_stats(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) c, SUM(messages_count) m FROM chat_users WHERE chat_id=?", (chat_id,)
        )
        row = await cur.fetchone()
    text = (
        f"📊 <b>Статистика чата</b>\n\n"
        f"Пользователей в базе: {row['c'] or 0}\n"
        f"Всего сообщений: {row['m'] or 0}"
    )
    await callback.message.edit_text(text, reply_markup=kb_back())
    await callback.answer()


@router.callback_query(F.data == "adm:premium")
async def cb_admin_premium(callback: CallbackQuery):
    settings = await get_chat_settings(callback.message.chat.id)
    text = (
        f"💎 <b>Подписка</b>\n\n"
        f"Цена: {settings['price_stars']} ⭐/мес\n"
        f"Пробный период: {settings['trial_days']} дн.\n\n"
        f"Изменить: /setprice и /settrial (только супер-админы бота)"
    )
    await callback.message.edit_text(text, reply_markup=kb_back())
    await callback.answer()


@router.callback_query(F.data == "adm:triggers")
async def cb_admin_triggers(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT key FROM triggers WHERE chat_id=?", (chat_id,))
        rows = await cur.fetchall()
    keys = ", ".join(f"«{r['key']}»" for r in rows) or "нет триггеров"
    text = (
        f"🧩 <b>Триггеры</b>\n\n{keys}\n\n"
        f'Добавить: .addtrigger "ключ" "ответ"\n'
        f'Удалить: .removetrigger "ключ"'
    )
    await callback.message.edit_text(text, reply_markup=kb_back())
    await callback.answer()


@router.callback_query(F.data == "adm:other")
async def cb_admin_other(callback: CallbackQuery):
    text = (
        "⚙️ <b>Прочие команды</b>\n\n"
        ".clean, .sw, .remind, .poll, игры — .games\n"
        "Команды супер-админа бота: /givepremium /removepremium /addadmin "
        "/removeadmin /banuser /unbanuser /setprice /settrial /resetwarns /resetstats"
    )
    await callback.message.edit_text(text, reply_markup=kb_back())
    await callback.answer()


# ============================================================
#  КОМАНДЫ СУПЕР-АДМИНА БОТА
# ============================================================

def parse_target_and_days(args: str) -> tuple[int | None, int]:
    tokens = args.split()
    user_id = int(tokens[0]) if tokens and tokens[0].isdigit() else None
    days = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else 30
    return user_id, days


@router.message(Command("givepremium"))
async def cmd_givepremium(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    user_id, days = parse_target_and_days(message.text.partition(" ")[2])
    if not user_id:
        return await message.reply("Использование: /givepremium <user_id> [days]")
    until = datetime.utcnow() + timedelta(days=days)
    await update_user_field(message.chat.id, user_id, premium_until=until.isoformat())
    await message.reply(f"✅ VIP выдан пользователю {user_id} до {fmt_dt(until)}.")


@router.message(Command("removepremium"))
async def cmd_removepremium(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /removepremium <user_id>")
    await update_user_field(message.chat.id, int(args), premium_until=None)
    await message.reply(f"✅ VIP снят с пользователя {args}.")


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /addadmin <user_id>")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (int(args),))
        await db.commit()
    await message.reply(f"✅ {args} назначен админом бота.")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /removeadmin <user_id>")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bot_admins WHERE user_id=?", (int(args),))
        await db.commit()
    await message.reply(f"✅ {args} больше не админ бота.")


@router.message(Command("banuser"))
async def cmd_banuser_admin(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /banuser <user_id>")
    await update_user_field(message.chat.id, int(args), is_banned=1)
    try:
        await bot.ban_chat_member(message.chat.id, int(args))
    except TelegramBadRequest:
        pass
    await message.reply(f"🚫 {args} забанен.")


@router.message(Command("unbanuser"))
async def cmd_unbanuser_admin(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /unbanuser <user_id>")
    await update_user_field(message.chat.id, int(args), is_banned=0)
    try:
        await bot.unban_chat_member(message.chat.id, int(args), only_if_banned=True)
    except TelegramBadRequest:
        pass
    await message.reply(f"✅ {args} разбанен.")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /setprice <звёзды>")
    await set_chat_setting(message.chat.id, "price_stars", int(args))
    await message.reply(f"✅ Цена подписки: {args} ⭐/мес.")


@router.message(Command("settrial"))
async def cmd_settrial(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /settrial <дни>")
    await set_chat_setting(message.chat.id, "trial_days", int(args))
    await message.reply(f"✅ Пробный период: {args} дн.")


@router.message(Command("resetwarns"))
async def cmd_resetwarns(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /resetwarns <user_id>")
    await update_user_field(message.chat.id, int(args), warns=0)
    await message.reply(f"✅ Варны {args} обнулены.")


@router.message(Command("resetstats"))
async def cmd_resetstats(message: Message):
    if not await is_bot_admin(message.from_user.id):
        return
    args = message.text.partition(" ")[2].strip()
    if not args.isdigit():
        return await message.reply("Использование: /resetstats <user_id>")
    await update_user_field(
        message.chat.id, int(args), xp=0, messages_count=0, night_msgs=0, day_msgs=0
    )
    await message.reply(f"✅ Статистика {args} сброшена.")


# ============================================================
#  СТАРТ
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я <b>Sialens</b> — бот модерации, аналитики и развлечений для твоего чата.\n\n"
        "Добавь меня в группу и выдай права администратора, чтобы начать. "
        "Команда /admin откроет панель управления, .games покажет список игр."
    )


# ============================================================
#  ЗАПУСК
# ============================================================

async def main():
    await init_db()
    asyncio.create_task(reminders_loop())
    log.info("Sialens запущен, начинаю polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
