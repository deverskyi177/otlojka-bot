"""
Sialens — Telegram-бот модерации, игр и аналитики для групп.
aiogram 3.x, SQLite, Telegram Stars (XTR) для подписки.
"""

import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatPermissions, PreCheckoutQuery, LabeledPrice
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# ────────────────────────────────────────────────────────────────────────────
# КОНФИГУРАЦИЯ
# ────────────────────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения (.env)")

os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("sialens")

DB_PATH = os.path.join("data", "sialens.db")

# ────────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ
# ────────────────────────────────────────────────────────────────────────────


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            first_name TEXT,
            messages INTEGER DEFAULT 0,
            warns INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            premium_until INTEGER DEFAULT 0,
            trial_used INTEGER DEFAULT 0,
            joined_at INTEGER,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS activity (
            user_id INTEGER,
            chat_id INTEGER,
            hour INTEGER,
            day TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id, hour, day)
        );

        CREATE TABLE IF NOT EXISTS bad_words (
            chat_id INTEGER,
            word TEXT,
            PRIMARY KEY (chat_id, word)
        );

        CREATE TABLE IF NOT EXISTS triggers (
            chat_id INTEGER,
            key TEXT,
            response TEXT,
            PRIMARY KEY (chat_id, key)
        );

        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            block_stickers INTEGER DEFAULT 0,
            price_stars INTEGER DEFAULT 50,
            trial_days INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS global_admins (
            user_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS business_connections (
            business_connection_id TEXT PRIMARY KEY,
            owner_user_id INTEGER,
            is_enabled INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS personal_contacts (
            business_connection_id TEXT,
            chat_id INTEGER,
            status TEXT DEFAULT 'none',
            warns INTEGER DEFAULT 0,
            PRIMARY KEY (business_connection_id, chat_id)
        );
        """
    )
    conn.commit()
    conn.close()
    logger.info("База данных готова: %s", DB_PATH)


def get_settings(chat_id: int) -> sqlite3.Row:
    conn = db()
    row = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO settings (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row


def ensure_user(user_id: int, chat_id: int, username: str = "", first_name: str = ""):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (user_id, chat_id, username, first_name, joined_at) VALUES (?,?,?,?,?)",
            (user_id, chat_id, username, first_name, int(time.time())),
        )
        conn.commit()
    else:
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=? AND chat_id=?",
            (username, first_name, user_id, chat_id),
        )
        conn.commit()
    conn.close()


def bump_message(user_id: int, chat_id: int):
    conn = db()
    conn.execute(
        "UPDATE users SET messages = messages + 1 WHERE user_id=? AND chat_id=?",
        (user_id, chat_id),
    )
    hour = datetime.now().hour
    day = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT INTO activity (user_id, chat_id, hour, day, count) VALUES (?,?,?,?,1)
           ON CONFLICT(user_id, chat_id, hour, day) DO UPDATE SET count = count + 1""",
        (user_id, chat_id, hour, day),
    )
    conn.commit()
    conn.close()


def get_user(user_id: int, chat_id: int):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()
    conn.close()
    return row


def add_warn(user_id: int, chat_id: int) -> int:
    conn = db()
    conn.execute(
        "UPDATE users SET warns = warns + 1 WHERE user_id=? AND chat_id=?",
        (user_id, chat_id),
    )
    conn.commit()
    warns = conn.execute(
        "SELECT warns FROM users WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()["warns"]
    conn.close()
    return warns


def reset_warns(user_id: int, chat_id: int):
    conn = db()
    conn.execute(
        "UPDATE users SET warns = 0 WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    )
    conn.commit()
    conn.close()


def reset_stats(user_id: int, chat_id: int):
    conn = db()
    conn.execute(
        "UPDATE users SET messages = 0 WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    )
    conn.execute("DELETE FROM activity WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    conn.commit()
    conn.close()


def is_global_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    conn = db()
    row = conn.execute("SELECT 1 FROM global_admins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def add_global_admin(user_id: int):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO global_admins (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def remove_global_admin(user_id: int):
    conn = db()
    conn.execute("DELETE FROM global_admins WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def is_premium(user_id: int, chat_id: int) -> bool:
    row = get_user(user_id, chat_id)
    if not row:
        return False
    return row["premium_until"] > int(time.time())


def grant_premium(user_id: int, chat_id: int, days: int):
    ensure_user(user_id, chat_id)
    conn = db()
    row = conn.execute(
        "SELECT premium_until FROM users WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()
    base = max(row["premium_until"], int(time.time())) if row else int(time.time())
    new_until = base + days * 86400
    conn.execute(
        "UPDATE users SET premium_until=? WHERE user_id=? AND chat_id=?",
        (new_until, user_id, chat_id),
    )
    conn.commit()
    conn.close()


def remove_premium(user_id: int, chat_id: int):
    conn = db()
    conn.execute(
        "UPDATE users SET premium_until=0 WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    )
    conn.commit()
    conn.close()


def get_role(user_id: int, chat_id: int) -> str:
    if is_global_admin(user_id):
        return "admin"
    row = get_user(user_id, chat_id)
    if not row:
        return "новичок"
    if row["premium_until"] > int(time.time()):
        return "VIP"
    if row["messages"] >= 50:
        return "проверенный"
    return "новичок"


# ── Business Automation (личные чаты владельца аккаунта) ──


def save_business_connection(business_connection_id: str, owner_user_id: int, is_enabled: bool):
    conn = db()
    conn.execute(
        """INSERT INTO business_connections (business_connection_id, owner_user_id, is_enabled)
           VALUES (?,?,?)
           ON CONFLICT(business_connection_id)
           DO UPDATE SET owner_user_id=excluded.owner_user_id, is_enabled=excluded.is_enabled""",
        (business_connection_id, owner_user_id, int(is_enabled)),
    )
    conn.commit()
    conn.close()


def get_connection_owner(business_connection_id: str):
    conn = db()
    row = conn.execute(
        "SELECT owner_user_id FROM business_connections WHERE business_connection_id=? AND is_enabled=1",
        (business_connection_id,),
    ).fetchone()
    conn.close()
    return row["owner_user_id"] if row else None


def get_personal_status(business_connection_id: str, chat_id: int):
    conn = db()
    row = conn.execute(
        "SELECT status, warns FROM personal_contacts WHERE business_connection_id=? AND chat_id=?",
        (business_connection_id, chat_id),
    ).fetchone()
    conn.close()
    if not row:
        return "none", 0
    return row["status"], row["warns"]


def set_personal_status(business_connection_id: str, chat_id: int, status: str):
    conn = db()
    conn.execute(
        """INSERT INTO personal_contacts (business_connection_id, chat_id, status) VALUES (?,?,?)
           ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET status=excluded.status""",
        (business_connection_id, chat_id, status),
    )
    conn.commit()
    conn.close()


def add_personal_warn(business_connection_id: str, chat_id: int) -> int:
    conn = db()
    conn.execute(
        """INSERT INTO personal_contacts (business_connection_id, chat_id, warns) VALUES (?,?,1)
           ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET warns = warns + 1""",
        (business_connection_id, chat_id),
    )
    conn.commit()
    warns = conn.execute(
        "SELECT warns FROM personal_contacts WHERE business_connection_id=? AND chat_id=?",
        (business_connection_id, chat_id),
    ).fetchone()["warns"]
    conn.close()
    return warns


def reset_personal_warns(business_connection_id: str, chat_id: int):
    conn = db()
    conn.execute(
        "UPDATE personal_contacts SET warns=0 WHERE business_connection_id=? AND chat_id=?",
        (business_connection_id, chat_id),
    )
    conn.commit()
    conn.close()


PERSONAL_CMD_RE = re.compile(r"^\.(mute|unmute|ban|unban|warn|clean)(?:\s+(.*))?$", re.IGNORECASE)


# ────────────────────────────────────────────────────────────────────────────
# ИНИЦИАЛИЗАЦИЯ БОТА
# ────────────────────────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# в памяти: анти-флуд и повторы
last_messages: dict[tuple[int, int], list[float]] = {}
last_text: dict[tuple[int, int], str] = {}
repeat_count: dict[tuple[int, int], int] = {}
guess_numbers: dict[int, int] = {}

RU = "йцукенгшщзхъфывапролджэячсмитьбюЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ"
EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>"
RU2EN = str.maketrans(RU, EN)
EN2RU = str.maketrans(EN, RU)

LINK_RE = re.compile(r"(https?://|www\.|t\.me/|@[a-zA-Z0-9_]{4,})", re.IGNORECASE)


def parse_duration(text: str):
    """Возвращает (timedelta или None-навсегда, ok:bool)."""
    text = text.strip().lower()
    if text in ("навсегда", "forever", "перм", "permanent"):
        return None, True
    m = re.match(r"^(\d+)\s*(м|min|мин|ч|h|час|д|d|день|дн)$", text)
    if not m:
        return None, False
    value, unit = int(m.group(1)), m.group(2)
    if unit in ("м", "min", "мин"):
        return timedelta(minutes=value), True
    if unit in ("ч", "h", "час"):
        return timedelta(hours=value), True
    if unit in ("д", "d", "день", "дн"):
        return timedelta(days=value), True
    return None, False


def is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def get_target(message: Message):
    """Определяет пользователя-цель команды: по ответу на сообщение."""
    if message.reply_to_message:
        return message.reply_to_message.from_user
    return None


async def require_group_admin(message: Message) -> bool:
    """Проверяет, что вызвавший команду — админ чата или глобальный админ бота."""
    if is_global_admin(message.from_user.id):
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False


# ────────────────────────────────────────────────────────────────────────────
# СТАРТ / ПОМОЩЬ
# ────────────────────────────────────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "👁 <b>Sialens</b>\n"
        "Глаза всё видят. Бот готов следить за сообщениями и наводить порядок в чате.\n\n"
        "Напиши <code>.help</code> чтобы увидеть список команд, либо /admin для панели администратора."
    )
    await message.answer(text)


@router.message(Command("help"))
@router.message(F.text == ".help")
async def cmd_help(message: Message):
    text = (
        "<b>Модерация:</b> .mute .warn .ban .unban .kick .clean .wbl .wsag\n"
        "<b>Игры:</b> .games .rps .anim .revo .guess\n"
        "<b>Аналитика (VIP):</b> .stats .top .nightowl .peak\n"
        "<b>Триггеры (VIP):</b> .addtrigger .removetrigger .trigger\n"
        "<b>Прочее:</b> .poll .remind .sw\n"
        "<b>Подписка:</b> /buy\n"
    )
    await message.answer(text)


# ────────────────────────────────────────────────────────────────────────────
# СЧЁТЧИК СООБЩЕНИЙ + ФИЛЬТРЫ (запускается на каждое текстовое сообщение в группе)
# ────────────────────────────────────────────────────────────────────────────


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text | F.caption)
async def on_any_group_message(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    if message.text and message.text.startswith("."):
        return  # команды обрабатываются отдельными хендлерами

    ensure_user(
        message.from_user.id, message.chat.id,
        message.from_user.username or "", message.from_user.first_name or "",
    )
    bump_message(message.from_user.id, message.chat.id)

    text = message.text or message.caption or ""
    key = (message.chat.id, message.from_user.id)
    admin_here = False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        admin_here = member.status in ("administrator", "creator")
    except TelegramBadRequest:
        pass

    if admin_here or is_global_admin(message.from_user.id):
        await check_triggers(message, text)
        return

    # ── антифлуд ──
    now = time.time()
    stamps = last_messages.setdefault(key, [])
    stamps.append(now)
    stamps[:] = [t for t in stamps if now - t < 5]
    if len(stamps) >= 4:
        await auto_mute(message.chat.id, message.from_user.id, timedelta(minutes=5))
        await message.answer(
            f"🔇 {message.from_user.first_name} замучен на 5 мин за флуд."
        )
        stamps.clear()
        return

    # ── повторы ──
    if text and last_text.get(key) == text:
        repeat_count[key] = repeat_count.get(key, 0) + 1
        if repeat_count[key] >= 3:
            await try_delete(message)
            await warn_and_maybe_ban(message.chat.id, message.from_user, "повтор сообщений")
            repeat_count[key] = 0
            return
    else:
        repeat_count[key] = 0
    last_text[key] = text

    # ── ссылки ──
    if text and LINK_RE.search(text):
        await try_delete(message)
        await warn_and_maybe_ban(message.chat.id, message.from_user, "ссылки запрещены")
        return

    # ── капс ──
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 10:
        upper = sum(1 for c in letters if c.isupper())
        if upper / len(letters) > 0.7:
            await try_delete(message)
            await warn_and_maybe_ban(message.chat.id, message.from_user, "слишком много CAPS")
            return

    # ── маты (wbl) ──
    conn = db()
    words = [r["word"] for r in conn.execute(
        "SELECT word FROM bad_words WHERE chat_id=?", (message.chat.id,)
    ).fetchall()]
    conn.close()
    lowered = text.lower()
    if any(w in lowered for w in words):
        await try_delete(message)
        await warn_and_maybe_ban(message.chat.id, message.from_user, "запрещённое слово")
        return

    # ── стикеры/гифки ──
    settings = get_settings(message.chat.id)
    if settings["block_stickers"] and (message.sticker or message.animation):
        await try_delete(message)
        return

    await check_triggers(message, text)


async def try_delete(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def auto_mute(chat_id: int, user_id: int, duration: timedelta | None):
    until = int(time.time() + duration.total_seconds()) if duration else 0
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
    except TelegramBadRequest as e:
        logger.warning("Не удалось замутить %s в %s: %s", user_id, chat_id, e)


async def warn_and_maybe_ban(chat_id: int, user, reason: str):
    ensure_user(user.id, chat_id, user.username or "", user.first_name or "")
    warns = add_warn(user.id, chat_id)
    if warns >= 3:
        try:
            await bot.ban_chat_member(chat_id, user.id, until_date=int(time.time() + 86400))
            reset_warns(user.id, chat_id)
            await bot.send_message(
                chat_id, f"🚫 {user.first_name} забанен на 24ч (3 предупреждения)."
            )
        except TelegramBadRequest:
            pass
    else:
        await bot.send_message(
            chat_id, f"⚠️ {user.first_name} получил предупреждение ({warns}/3): {reason}"
        )


async def check_triggers(message: Message, text: str):
    if not text:
        return
    conn = db()
    rows = conn.execute(
        "SELECT key, response FROM triggers WHERE chat_id=?", (message.chat.id,)
    ).fetchall()
    conn.close()
    lowered = text.lower()
    for r in rows:
        if r["key"].lower() in lowered:
            await message.answer(r["response"])
            break


# ────────────────────────────────────────────────────────────────────────────
# ЛИЧНЫЕ ЧАТЫ ЧЕРЕЗ «АВТОМАТИЗАЦИЮ ЧАТОВ» (TELEGRAM BUSINESS)
# ────────────────────────────────────────────────────────────────────────────
#
# Когда владелец подключает бота через Настройки → Telegram Business →
# Автоматизация чатов, Telegram присылает апдейты business_connection и
# business_message. Внутри них message.chat — это переписка владельца с
# конкретным собеседником, а message.business_connection_id — идентификатор
# подключения, через который бот может писать/удалять сообщения ОТ ИМЕНИ
# владельца в этой переписке.
#
# ВАЖНО: у Bot API нет метода «заблокировать контакт» — это действие доступно
# только в самом приложении Telegram и владельцу аккаунта. Поэтому .ban здесь
# реализован как имитация блокировки: бот тихо и без ответа удаляет все
# дальнейшие сообщения от этого собеседника. Настоящую блокировку (чтобы
# человек не мог даже написать) всё равно нужно делать вручную в Telegram.


@router.business_connection()
async def on_business_connection(connection):
    save_business_connection(connection.id, connection.user.id, connection.is_enabled)
    logger.info(
        "Business-подключение %s: владелец=%s, включено=%s",
        connection.id, connection.user.id, connection.is_enabled,
    )


@router.business_message()
async def on_business_message(message: Message):
    bc_id = message.business_connection_id
    if not bc_id:
        return
    owner_id = get_connection_owner(bc_id)
    if owner_id is None:
        return

    contact_chat_id = message.chat.id

    # ── Сообщение отправлено самим владельцем аккаунта ──
    if message.from_user and message.from_user.id == owner_id:
        text = (message.text or "").strip()
        m = PERSONAL_CMD_RE.match(text)
        if not m:
            return  # обычное сообщение владельца, не команда — ничего не делаем
        cmd, arg = m.group(1).lower(), (m.group(2) or "").strip()

        if cmd == "mute":
            duration_text = arg or "навсегда"
            duration, ok = parse_duration(duration_text)
            set_personal_status(bc_id, contact_chat_id, "muted")
            try:
                await bot.send_message(contact_chat_id, "Замолчи.", business_connection_id=bc_id)
            except Exception as e:
                logger.warning("Не удалось отправить сообщение в личном чате: %s", e)

        elif cmd == "unmute":
            set_personal_status(bc_id, contact_chat_id, "none")

        elif cmd == "ban":
            set_personal_status(bc_id, contact_chat_id, "banned")

        elif cmd == "unban":
            set_personal_status(bc_id, contact_chat_id, "none")

        elif cmd == "warn":
            warns = add_personal_warn(bc_id, contact_chat_id)
            if warns >= 3:
                set_personal_status(bc_id, contact_chat_id, "banned")

        elif cmd == "clean":
            count = int(arg) if arg.isdigit() else 10
            for msg_id in range(message.message_id - 1, message.message_id - count - 1, -1):
                try:
                    await bot.delete_message(contact_chat_id, msg_id, business_connection_id=bc_id)
                except Exception:
                    continue

        # удаляем саму команду, чтобы собеседник не видел «.mute 1ч» в переписке
        try:
            await bot.delete_message(contact_chat_id, message.message_id, business_connection_id=bc_id)
        except Exception:
            pass
        return

    # ── Сообщение пришло от собеседника ──
    status, _ = get_personal_status(bc_id, contact_chat_id)
    if status in ("muted", "banned"):
        try:
            await bot.delete_message(contact_chat_id, message.message_id, business_connection_id=bc_id)
        except Exception as e:
            logger.warning("Не удалось удалить сообщение собеседника: %s", e)


# ────────────────────────────────────────────────────────────────────────────
# ПРИВЕТСТВИЕ / ПРОЩАНИЕ
# ────────────────────────────────────────────────────────────────────────────


@router.message(F.new_chat_members)
async def on_join(message: Message):
    for u in message.new_chat_members:
        ensure_user(u.id, message.chat.id, u.username or "", u.first_name or "")
        await message.answer(f"👋 Добро пожаловать, {u.first_name}! Хорошего пребывания в чате.")


@router.message(F.left_chat_member)
async def on_leave(message: Message):
    u = message.left_chat_member
    await message.answer(f"👋 {u.first_name} покинул(а) чат.")


# ────────────────────────────────────────────────────────────────────────────
# МОДЕРАЦИЯ
# ────────────────────────────────────────────────────────────────────────────


@router.message(F.text.regexp(r"^\.mute(\s+.*)?$"))
async def cmd_mute(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение пользователя: <code>.mute 1ч</code>")
    args = message.text.split(maxsplit=1)
    dur_text = args[1] if len(args) > 1 else "навсегда"
    duration, ok = parse_duration(dur_text)
    if not ok:
        return await message.reply("Формат: .mute 1ч / 6ч / 12ч / 24ч / навсегда")
    await auto_mute(message.chat.id, target.id, duration)
    label = "навсегда" if duration is None else dur_text
    await message.reply(f"🔇 {target.first_name} замучен ({label}).")


@router.message(F.text == ".nomute")
async def cmd_nomute_help(message: Message):
    await message.reply("Используйте: ответьте на сообщение и напишите .unmute")


@router.message(F.text.regexp(r"^\.unmute(\s+.*)?$"))
async def cmd_unmute(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение пользователя.")
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_photos=True,
                can_send_videos=True, can_send_other_messages=True,
            ),
        )
        await message.reply(f"🔊 {target.first_name} размучен.")
    except TelegramBadRequest as e:
        await message.reply(f"Ошибка: {e}")


@router.message(F.text.regexp(r"^\.warn(\s+.*)?$"))
async def cmd_warn(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение пользователя.")
    await warn_and_maybe_ban(message.chat.id, target, "выдано вручную")


@router.message(F.text.regexp(r"^\.ban(\s+.*)?$"))
async def cmd_ban(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение пользователя.")
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.reply(f"🚫 {target.first_name} забанен.")
    except TelegramBadRequest as e:
        await message.reply(f"Ошибка: {e}")


@router.message(F.text.regexp(r"^\.unban(\s+\d+)$"))
async def cmd_unban(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    user_id = int(message.text.split()[1])
    try:
        await bot.unban_chat_member(message.chat.id, user_id)
        await message.reply("✅ Пользователь разбанен.")
    except TelegramBadRequest as e:
        await message.reply(f"Ошибка: {e}")


@router.message(F.text == ".kick")
async def cmd_kick(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение пользователя.")
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {target.first_name} кикнут.")
    except TelegramBadRequest as e:
        await message.reply(f"Ошибка: {e}")


@router.message(F.text.regexp(r"^\.clean(\s+\d+)?$"))
async def cmd_clean(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    parts = message.text.split()
    count = int(parts[1]) if len(parts) > 1 else 10
    deleted = 0
    for msg_id in range(message.message_id - 1, message.message_id - count - 1, -1):
        try:
            await bot.delete_message(message.chat.id, msg_id)
            deleted += 1
        except TelegramBadRequest:
            continue
    await try_delete(message)
    note = await bot.send_message(message.chat.id, f"🧹 Удалено сообщений: {deleted}")
    await asyncio.sleep(3)
    await try_delete(note)


@router.message(F.text.regexp(r"^\.wbl(\s+.*)?$"))
async def cmd_wbl(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    parts = message.text.split(maxsplit=2)
    if len(parts) == 1:
        conn = db()
        words = [r["word"] for r in conn.execute(
            "SELECT word FROM bad_words WHERE chat_id=?", (message.chat.id,)
        ).fetchall()]
        conn.close()
        return await message.reply(
            "Список запрещённых слов:\n" + (", ".join(words) if words else "пусто")
            + "\n\nДобавить: .wbl add слово\nУдалить: .wbl del слово"
        )
    action = parts[1].lower()
    word = parts[2].lower() if len(parts) > 2 else ""
    conn = db()
    if action == "add" and word:
        conn.execute(
            "INSERT OR IGNORE INTO bad_words (chat_id, word) VALUES (?,?)",
            (message.chat.id, word),
        )
        conn.commit()
        await message.reply(f"✅ Добавлено в чёрный список: {word}")
    elif action in ("del", "remove") and word:
        conn.execute(
            "DELETE FROM bad_words WHERE chat_id=? AND word=?", (message.chat.id, word)
        )
        conn.commit()
        await message.reply(f"✅ Удалено из чёрного списка: {word}")
    conn.close()


@router.message(F.text.regexp(r"^\.wsag(\s+(on|off))?$"))
async def cmd_wsag(message: Message):
    if not is_group(message):
        return
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    parts = message.text.split()
    settings = get_settings(message.chat.id)
    if len(parts) == 1:
        state = "включена" if settings["block_stickers"] else "выключена"
        return await message.reply(f"Блокировка стикеров/гифок сейчас {state}.\n.wsag on/off")
    value = 1 if parts[1].lower() == "on" else 0
    conn = db()
    conn.execute(
        "UPDATE settings SET block_stickers=? WHERE chat_id=?", (value, message.chat.id)
    )
    conn.commit()
    conn.close()
    await message.reply(f"✅ Блокировка стикеров/гифок: {'включена' if value else 'выключена'}")


# ────────────────────────────────────────────────────────────────────────────
# ИГРЫ
# ────────────────────────────────────────────────────────────────────────────


@router.message(F.text == ".games")
async def cmd_games(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Анимация (.anim)", callback_data="game_info_anim")
    kb.button(text="✊✋✌️ Камень-ножницы-бумага", callback_data="game_info_rps")
    kb.button(text="🔫 Рулетка (.revo)", callback_data="game_info_revo")
    kb.button(text="🔢 Угадай число (.guess)", callback_data="game_info_guess")
    kb.adjust(1)
    await message.answer("🎮 Выберите игру:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("game_info_"))
async def cb_game_info(call: CallbackQuery):
    info = {
        "game_info_anim": "Напишите: .anim 1 (число от 1 до 5) — бот пришлёт анимацию.",
        "game_info_rps": "Напишите: .rps камень / ножницы / бумага",
        "game_info_revo": "Напишите: .revo — 1 к 6 шанс получить шуточный мут на 1 мин.",
        "game_info_guess": "Напишите: .guess начать — бот загадает число 1-100, угадывайте в чате.",
    }
    await call.answer(info.get(call.data, ""), show_alert=True)


@router.message(F.text.regexp(r"^\.anim(\s+[1-5])?$"))
async def cmd_anim(message: Message):
    parts = message.text.split()
    n = int(parts[1]) if len(parts) > 1 else random.randint(1, 5)
    emojis = ["🎲", "🎯", "🏀", "⚽", "🎳"]
    await message.answer_dice(emoji=emojis[n - 1])


@router.message(F.text.regexp(r"^\.rps(\s+.*)?$"))
async def cmd_rps(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Напишите: .rps камень / ножницы / бумага")
    choices = {"камень": 0, "ножницы": 1, "бумага": 2}
    user_choice = parts[1].strip().lower()
    if user_choice not in choices:
        return await message.reply("Варианты: камень, ножницы, бумага")
    bot_choice = random.choice(list(choices.keys()))
    u, b = choices[user_choice], choices[bot_choice]
    if u == b:
        result = "🤝 Ничья!"
    elif (u - b) % 3 == 1:
        result = "🎉 Вы выиграли!"
    else:
        result = "🤖 Бот выиграл!"
    await message.reply(f"Вы: {user_choice}\nБот: {bot_choice}\n{result}")


@router.message(F.text == ".revo")
async def cmd_revo(message: Message):
    if not is_group(message):
        return await message.reply("Игра работает только в группах.")
    if random.randint(1, 6) == 1:
        try:
            await bot.restrict_chat_member(
                message.chat.id, message.from_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(time.time() + 60),
            )
            await message.reply("💥 Бах! Вам не повезло — мут на 1 минуту.")
        except TelegramBadRequest:
            await message.reply("💥 Бах! (не удалось замутить — нет прав)")
    else:
        await message.reply("🔫 Click. Повезло, попробуйте ещё раз.")


@router.message(F.text.regexp(r"^\.guess(\s+.*)?$"))
async def cmd_guess(message: Message):
    parts = message.text.split(maxsplit=1)
    chat_id = message.chat.id
    if len(parts) == 1 or parts[1].strip().lower() in ("начать", "start"):
        guess_numbers[chat_id] = random.randint(1, 100)
        return await message.reply("🔢 Я загадал число от 1 до 100. Пишите .guess <число>")
    if chat_id not in guess_numbers:
        return await message.reply("Сначала напишите .guess начать")
    try:
        n = int(parts[1].strip())
    except ValueError:
        return await message.reply("Нужно число.")
    target = guess_numbers[chat_id]
    if n == target:
        del guess_numbers[chat_id]
        await message.reply(f"🎉 Угадали! Это было {target}.")
    elif n < target:
        await message.reply("⬆️ Больше!")
    else:
        await message.reply("⬇️ Меньше!")


# ────────────────────────────────────────────────────────────────────────────
# АНАЛИТИКА (VIP)
# ────────────────────────────────────────────────────────────────────────────


def require_premium_reply():
    async def _check(message: Message) -> bool:
        if is_global_admin(message.from_user.id):
            return True
        if is_premium(message.from_user.id, message.chat.id):
            return True
        await message.reply(
            "🔒 Эта функция доступна по подписке VIP. Используйте /buy чтобы оформить."
        )
        return False
    return _check


@router.message(F.text == ".stats")
async def cmd_stats(message: Message):
    row = get_user(message.from_user.id, message.chat.id)
    if not row:
        return await message.reply("Нет данных о вас в этом чате.")
    role = get_role(message.from_user.id, message.chat.id)
    await message.reply(
        f"📊 <b>Ваша статистика</b>\n"
        f"Сообщений: {row['messages']}\n"
        f"Предупреждений: {row['warns']}\n"
        f"Роль: {role}"
    )


@router.message(F.text == ".top")
async def cmd_top(message: Message):
    if not await require_premium_reply()(message):
        return
    conn = db()
    rows = conn.execute(
        "SELECT username, first_name, messages FROM users WHERE chat_id=? ORDER BY messages DESC LIMIT 10",
        (message.chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        return await message.reply("Пока нет данных.")
    lines = [
        f"{i+1}. {r['first_name'] or r['username'] or '???'} — {r['messages']} сообщ."
        for i, r in enumerate(rows)
    ]
    await message.reply("🏆 <b>Топ активности:</b>\n" + "\n".join(lines))


@router.message(F.text == ".nightowl")
async def cmd_nightowl(message: Message):
    if not await require_premium_reply()(message):
        return
    conn = db()
    rows = conn.execute(
        """SELECT user_id, SUM(count) as c FROM activity
           WHERE chat_id=? AND hour BETWEEN 0 AND 5
           GROUP BY user_id ORDER BY c DESC LIMIT 1""",
        (message.chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        return await message.reply("Ночных сов не найдено 🦉")
    user = get_user(rows[0]["user_id"], message.chat.id)
    name = user["first_name"] if user else rows[0]["user_id"]
    await message.reply(f"🦉 Ночная сова чата: {name} ({rows[0]['c']} сообщ. ночью)")


@router.message(F.text == ".peak")
async def cmd_peak(message: Message):
    if not await require_premium_reply()(message):
        return
    conn = db()
    rows = conn.execute(
        """SELECT hour, SUM(count) as c FROM activity WHERE chat_id=?
           GROUP BY hour ORDER BY c DESC LIMIT 1""",
        (message.chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        return await message.reply("Недостаточно данных.")
    await message.reply(f"📈 Час пик активности чата: {rows[0]['hour']}:00 ({rows[0]['c']} сообщ.)")


# ────────────────────────────────────────────────────────────────────────────
# ТРИГГЕРЫ (VIP)
# ────────────────────────────────────────────────────────────────────────────


TRIGGER_RE = re.compile(r'^\.addtrigger\s+"([^"]+)"\s+"([^"]+)"$')


@router.message(F.text.regexp(r'^\.addtrigger\s+.*$'))
async def cmd_addtrigger(message: Message):
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    if not await require_premium_reply()(message):
        return
    m = TRIGGER_RE.match(message.text)
    if not m:
        return await message.reply('Формат: .addtrigger "ключ" "ответ"')
    key, response = m.group(1), m.group(2)
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO triggers (chat_id, key, response) VALUES (?,?,?)",
        (message.chat.id, key, response),
    )
    conn.commit()
    conn.close()
    await message.reply(f"✅ Триггер добавлен: «{key}»")


@router.message(F.text.regexp(r'^\.removetrigger\s+"?([^"]+)"?$'))
async def cmd_removetrigger(message: Message):
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    m = re.match(r'^\.removetrigger\s+"?([^"]+)"?$', message.text)
    key = m.group(1)
    conn = db()
    conn.execute("DELETE FROM triggers WHERE chat_id=? AND key=?", (message.chat.id, key))
    conn.commit()
    conn.close()
    await message.reply(f"✅ Триггер удалён: «{key}»")


@router.message(F.text == ".trigger")
async def cmd_trigger_list(message: Message):
    conn = db()
    rows = conn.execute(
        "SELECT key FROM triggers WHERE chat_id=?", (message.chat.id,)
    ).fetchall()
    conn.close()
    if not rows:
        return await message.reply("Триггеров пока нет.")
    await message.reply("Триггеры: " + ", ".join(f"«{r['key']}»" for r in rows))


# ────────────────────────────────────────────────────────────────────────────
# ОПРОСЫ И НАПОМИНАНИЯ
# ────────────────────────────────────────────────────────────────────────────


POLL_RE = re.compile(r'"([^"]+)"')


@router.message(F.text.regexp(r'^\.poll\s+.*$'))
async def cmd_poll(message: Message):
    parts = POLL_RE.findall(message.text)
    if len(parts) < 3:
        return await message.reply('Формат: .poll "вопрос" "вариант1" "вариант2" ...')
    question, options = parts[0], parts[1:10]
    await bot.send_poll(message.chat.id, question=question, options=options, is_anonymous=False)


@router.message(F.text.regexp(r'^\.remind\s+"([^"]+)"\s+(\d+)\s*(м|мин|ч|час|д|дн)$'))
async def cmd_remind(message: Message):
    m = re.match(r'^\.remind\s+"([^"]+)"\s+(\d+)\s*(м|мин|ч|час|д|дн)$', message.text)
    text, value, unit = m.group(1), int(m.group(2)), m.group(3)
    seconds = value * 60
    if unit in ("ч", "час"):
        seconds = value * 3600
    elif unit in ("д", "дн"):
        seconds = value * 86400
    await message.reply(f"⏰ Напомню через {value}{unit}: «{text}»")
    asyncio.create_task(
        send_reminder(message.chat.id, message.from_user.id, text, seconds)
    )


async def send_reminder(chat_id: int, user_id: int, text: str, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.send_message(chat_id, f"⏰ <a href='tg://user?id={user_id}'>Напоминание</a>: {text}")
    except TelegramBadRequest:
        pass


# ────────────────────────────────────────────────────────────────────────────
# РАСКЛАДКА
# ────────────────────────────────────────────────────────────────────────────


@router.message(F.text.regexp(r"^\.sw\s+.+$"))
async def cmd_switch_layout(message: Message):
    text = message.text.split(maxsplit=1)[1]
    ru_letters = sum(1 for c in text if c in RU)
    en_letters = sum(1 for c in text if c in EN)
    converted = text.translate(EN2RU) if en_letters >= ru_letters else text.translate(RU2EN)
    await message.reply(converted)


# ────────────────────────────────────────────────────────────────────────────
# ПОДПИСКА (Telegram Stars)
# ────────────────────────────────────────────────────────────────────────────


@router.message(Command("buy"))
async def cmd_buy(message: Message):
    settings = get_settings(message.chat.id if is_group(message) else message.from_user.id)
    ensure_user(message.from_user.id, message.chat.id)
    row = get_user(message.from_user.id, message.chat.id)
    if row and not row["trial_used"] and get_settings(message.chat.id)["trial_days"] > 0:
        days = get_settings(message.chat.id)["trial_days"]
        grant_premium(message.from_user.id, message.chat.id, days)
        conn = db()
        conn.execute(
            "UPDATE users SET trial_used=1 WHERE user_id=? AND chat_id=?",
            (message.from_user.id, message.chat.id),
        )
        conn.commit()
        conn.close()
        return await message.reply(
            f"🎁 Вам активирован бесплатный триал VIP на {days} дн.! "
            f"После окончания используйте /buy снова, чтобы оплатить подписку."
        )
    price = get_settings(message.chat.id)["price_stars"]
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Sialens VIP — 1 месяц",
        description="Доступ к аналитике, триггерам и другим VIP-функциям на 30 дней.",
        payload=f"vip_{message.from_user.id}_{message.chat.id}",
        currency="XTR",
        prices=[LabeledPrice(label="VIP подписка (30 дней)", amount=price)],
        provider_token="",  # для Stars не требуется
    )


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    m = re.match(r"^vip_(\d+)_(-?\d+)$", payload)
    if m:
        user_id, chat_id = int(m.group(1)), int(m.group(2))
        grant_premium(user_id, chat_id, 30)
        await message.answer("✅ Оплата получена! VIP активирован на 30 дней.")


# ────────────────────────────────────────────────────────────────────────────
# АДМИН-ПАНЕЛЬ
# ────────────────────────────────────────────────────────────────────────────


def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика чата", callback_data="adm_stats")
    kb.button(text="⚙️ Настройки", callback_data="adm_settings")
    kb.button(text="👑 Управление админами", callback_data="adm_admins")
    kb.button(text="💳 Подписки", callback_data="adm_subs")
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    await message.answer("👁 <b>Админ-панель Sialens</b>\nВыберите раздел:", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    conn = db()
    total_users = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM users").fetchone()["c"]
    total_msgs = conn.execute("SELECT SUM(messages) as c FROM users").fetchone()["c"] or 0
    conn.close()
    await call.message.edit_text(
        f"📊 Всего пользователей в базе: {total_users}\nВсего сообщений учтено: {total_msgs}",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "adm_settings")
async def cb_adm_settings(call: CallbackQuery):
    s = get_settings(call.message.chat.id)
    await call.message.edit_text(
        f"⚙️ Цена подписки: {s['price_stars']} ⭐\nТриал: {s['trial_days']} дн.\n\n"
        f"Изменить: /setprice <звёзды>, /settrial <дни>",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "adm_admins")
async def cb_adm_admins(call: CallbackQuery):
    conn = db()
    rows = conn.execute("SELECT user_id FROM global_admins").fetchall()
    conn.close()
    ids = ADMIN_IDS + [r["user_id"] for r in rows]
    await call.message.edit_text(
        "👑 Администраторы бота:\n" + "\n".join(str(i) for i in ids)
        + "\n\n/addadmin <id>, /removeadmin <id>",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "adm_subs")
async def cb_adm_subs(call: CallbackQuery):
    conn = db()
    rows = conn.execute(
        "SELECT COUNT(*) as c FROM users WHERE premium_until > ?", (int(time.time()),)
    ).fetchone()
    conn.close()
    await call.message.edit_text(
        f"💳 Активных VIP-подписок: {rows['c']}\n\n"
        f"/givepremium <id> <дни>, /removepremium <id>",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


# ────────────────────────────────────────────────────────────────────────────
# АДМИН-КОМАНДЫ (текстовые)
# ────────────────────────────────────────────────────────────────────────────


def admin_only(handler):
    async def wrapper(message: Message):
        if not is_global_admin(message.from_user.id):
            return await message.reply("⛔ Недостаточно прав.")
        return await handler(message)
    return wrapper


@router.message(Command("givepremium"))
async def cmd_givepremium(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 3:
        return await message.reply("Формат: /givepremium <user_id> <дни>")
    user_id, days = int(parts[1]), int(parts[2])
    grant_premium(user_id, message.chat.id, days)
    await message.reply(f"✅ Пользователю {user_id} выдан VIP на {days} дн.")


@router.message(Command("removepremium"))
async def cmd_removepremium(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Формат: /removepremium <user_id>")
    remove_premium(int(parts[1]), message.chat.id)
    await message.reply("✅ VIP снят.")


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Формат: /addadmin <user_id>")
    add_global_admin(int(parts[1]))
    await message.reply("✅ Администратор добавлен.")


@router.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Формат: /removeadmin <user_id>")
    remove_global_admin(int(parts[1]))
    await message.reply("✅ Администратор удалён.")


@router.message(Command("banuser"))
async def cmd_banuser_admin(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Формат: /banuser <user_id>")
    try:
        await bot.ban_chat_member(message.chat.id, int(parts[1]))
        await message.reply("✅ Забанен.")
    except TelegramBadRequest as e:
        await message.reply(f"Ошибка: {e}")


@router.message(Command("unbanuser"))
async def cmd_unbanuser_admin(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Формат: /unbanuser <user_id>")
    try:
        await bot.unban_chat_member(message.chat.id, int(parts[1]))
        await message.reply("✅ Разбанен.")
    except TelegramBadRequest as e:
        await message.reply(f"Ошибка: {e}")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("Формат: /setprice <звёзды>")
    get_settings(message.chat.id)
    conn = db()
    conn.execute(
        "UPDATE settings SET price_stars=? WHERE chat_id=?", (int(parts[1]), message.chat.id)
    )
    conn.commit()
    conn.close()
    await message.reply(f"✅ Цена подписки: {parts[1]} ⭐")


@router.message(Command("settrial"))
async def cmd_settrial(message: Message):
    if not is_global_admin(message.from_user.id):
        return await message.reply("⛔ Недостаточно прав.")
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("Формат: /settrial <дни>")
    get_settings(message.chat.id)
    conn = db()
    conn.execute(
        "UPDATE settings SET trial_days=? WHERE chat_id=?", (int(parts[1]), message.chat.id)
    )
    conn.commit()
    conn.close()
    await message.reply(f"✅ Триал: {parts[1]} дн.")


@router.message(Command("resetwarns"))
async def cmd_resetwarns(message: Message):
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение пользователя.")
    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Предупреждения {target.first_name} сброшены.")


@router.message(Command("resetstats"))
async def cmd_resetstats(message: Message):
    if not await require_group_admin(message):
        return await message.reply("Только для админов чата.")
    target = await get_target(message)
    if not target:
        return await message.reply("Ответьте на сообщение пользователя.")
    reset_stats(target.id, message.chat.id)
    await message.reply(f"✅ Статистика {target.first_name} сброшена.")


# ────────────────────────────────────────────────────────────────────────────
# ЗАПУСК
# ────────────────────────────────────────────────────────────────────────────


async def main():
    init_db()
    logger.info("Sialens запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
