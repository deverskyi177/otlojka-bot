# -*- coding: utf-8 -*-
"""
Sialens — учебный Telegram-бот на aiogram 3 для деплоя на Railway.
Весь код в одном файле: bot.py
Настройки через переменные окружения (или .env).
"""
import os
import re
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, ChatActions, LabeledPrice
from aiogram.filters import Command, Text
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiosqlite

# Gemini generative api
try:
    from google import generativeai as genai  # type: ignore
except Exception:
    genai = None

# Load .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_IDS = os.getenv("ADMIN_IDS", "")  # comma-separated user ids

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required in env")

# parse admin ids
ADMIN_IDS_LIST = []
for part in ADMIN_IDS.split(","):
    part = part.strip()
    if part:
        try:
            ADMIN_IDS_LIST.append(int(part))
        except Exception:
            pass

# Logging
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# DB
DB_PATH = os.getenv("DB_PATH", "sialens.db")
os.makedirs("data", exist_ok=True)

# Bot and dispatcher
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# In-memory flood tracking
_flood_cache: Dict[Tuple[int, int], List[float]] = {}  # (chat_id, user_id) -> timestamps

# In-memory reminders tasks to persist them across runtime.
_reminder_tasks: Dict[int, asyncio.Task] = {}

# Ensure Gemini configured
if GEMINI_API_KEY and genai is not None:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.warning("Couldn't configure Gemini client: %s", e)
else:
    if GEMINI_API_KEY:
        logger.warning("google-generativeai package not available; AI features disabled.")


# Utilities
async def init_db():
    """
    Create DB and tables if not exists. Uses sqlite3 synchronous for startup,
    then aiosqlite for runtime convenience.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # users: track points, role, premium_until
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        points INTEGER DEFAULT 0,
        role TEXT DEFAULT 'new',
        premium_until TIMESTAMP,
        is_admin INTEGER DEFAULT 0
    )""")
    # warns
    c.execute("""
    CREATE TABLE IF NOT EXISTS warns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        issuer_id INTEGER,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # bans
    c.execute("""
    CREATE TABLE IF NOT EXISTS bans (
        chat_id INTEGER,
        user_id INTEGER,
        banned_until TIMESTAMP,
        reason TEXT,
        PRIMARY KEY (chat_id, user_id)
    )""")
    # triggers
    c.execute("""
    CREATE TABLE IF NOT EXISTS triggers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        keyword TEXT,
        response TEXT
    )""")
    # messages for AI context
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        username TEXT,
        text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # stats (message counts, nightowl tracking)
    c.execute("""
    CREATE TABLE IF NOT EXISTS stats (
        chat_id INTEGER,
        user_id INTEGER,
        messages INTEGER DEFAULT 0,
        last_message_at TIMESTAMP,
        PRIMARY KEY (chat_id, user_id)
    )""")
    # reminders
    c.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        text TEXT,
        remind_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        done INTEGER DEFAULT 0
    )""")
    # filters config (per chat)
    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_config (
        chat_id INTEGER PRIMARY KEY,
        caps_filter INTEGER DEFAULT 1,
        links_filter INTEGER DEFAULT 1,
        repeat_filter INTEGER DEFAULT 1,
        stickers_filter INTEGER DEFAULT 1,
        swears_filter INTEGER DEFAULT 1
    )""")
    # payments/premium purchases
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        amount INTEGER,
        currency TEXT,
        provider_payment_charge_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()
    # load pending reminders into tasks
    await schedule_pending_reminders()


async def schedule_pending_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow()
        async with db.execute("SELECT id, chat_id, user_id, text, remind_at FROM reminders WHERE done = 0") as cur:
            rows = await cur.fetchall()
            for r in rows:
                rid, chat_id, user_id, text, remind_at = r
                remind_dt = datetime.fromisoformat(remind_at) if isinstance(remind_at, str) else datetime.utcfromtimestamp(remind_at)
                if remind_dt <= now:
                    # send immediately and mark done
                    asyncio.create_task(send_reminder_immediately(rid, chat_id, user_id, text))
                else:
                    delay = (remind_dt - now).total_seconds()
                    task = asyncio.create_task(reminder_task(rid, chat_id, user_id, text, delay))
                    _reminder_tasks[rid] = task


async def send_reminder_immediately(rid, chat_id, user_id, text):
    try:
        await bot.send_message(chat_id, f"🔔 Напоминание для <a href='tg://user?id={user_id}'>пользователя</a>:\n\n{text}", disable_web_page_preview=True)
    except Exception as e:
        logger.exception("Failed to send immediate reminder: %s", e)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
        await db.commit()


async def reminder_task(rid, chat_id, user_id, text, delay):
    try:
        await asyncio.sleep(delay)
        await bot.send_message(chat_id, f"🔔 Напоминание для <a href='tg://user?id={user_id}'>пользователя</a>:\n\n{text}", disable_web_page_preview=True)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
            await db.commit()
    except asyncio.CancelledError:
        logger.info("Reminder %s cancelled", rid)
    except Exception as e:
        logger.exception("reminder task error: %s", e)


# Helper DB functions
async def add_message_for_context(chat_id: int, user_id: int, username: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO messages (chat_id, user_id, username, text) VALUES (?, ?, ?, ?)",
                         (chat_id, user_id, username, text))
        # keep last 200 messages
        await db.execute("DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT 200)")
        await db.commit()


async def get_ai_context(chat_id: int, limit: int = 15) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, text, created_at FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                              (chat_id, limit)) as cur:
            rows = await cur.fetchall()
    ctx = []
    for r in reversed(rows):
        ctx.append({"user_id": r[0], "username": r[1], "text": r[2], "created_at": r[3]})
    return ctx


async def ensure_user(user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                         (user.id, user.username, user.first_name))
        await db.commit()


async def inc_user_points(chat_id: int, user: types.User, amount: int = 1):
    await ensure_user(user)
    async with aiosqlite.connect(DB_PATH) as db:
        # increment stats table
        await db.execute("INSERT OR IGNORE INTO stats (chat_id, user_id, messages, last_message_at) VALUES (?, ?, 0, ?)",
                         (chat_id, user.id, datetime.utcnow().isoformat()))
        await db.execute("UPDATE stats SET messages = messages + ?, last_message_at = ? WHERE chat_id = ? AND user_id = ?",
                         (amount, datetime.utcnow().isoformat(), chat_id, user.id))
        # points in users table
        await db.execute("UPDATE users SET points = COALESCE(points,0) + ? WHERE user_id = ?", (amount, user.id))
        await db.commit()


async def add_warn(chat_id: int, user_id: int, issuer_id: int, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO warns (chat_id, user_id, issuer_id, reason) VALUES (?, ?, ?, ?)",
                         (chat_id, user_id, issuer_id, reason))
        await db.commit()
        # count warns
        async with db.execute("SELECT COUNT(*) FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)) as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0
    return count


async def reset_warns(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        await db.commit()


async def ban_user(chat_id: int, user_id: int, duration_seconds: Optional[int], reason: str = ""):
    banned_until = None
    if duration_seconds:
        banned_until = (datetime.utcnow() + timedelta(seconds=duration_seconds)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO bans (chat_id, user_id, banned_until, reason) VALUES (?, ?, ?, ?)",
                         (chat_id, user_id, banned_until, reason))
        await db.commit()


async def unban_user_db(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        await db.commit()


async def is_banned(chat_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT banned_until FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            banned_until = row[0]
            if not banned_until:
                return True  # forever
            try:
                dt = datetime.fromisoformat(banned_until)
            except Exception:
                return True
            if dt < datetime.utcnow():
                # expired -> remove
                await db.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                await db.commit()
                return False
            return True


# Moderation helpers
async def restrict_user(chat_id: int, user_id: int, until_seconds: Optional[int], can_send_messages: bool = False):
    """
    Mute by setting chat permissions. until_seconds=None -> forever (use large date)
    """
    until_date = 0
    if until_seconds:
        until_date = int((datetime.utcnow() + timedelta(seconds=until_seconds)).timestamp())
    else:
        # far future
        until_date = int((datetime.utcnow() + timedelta(days=3650)).timestamp())
    perms = types.ChatPermissions(can_send_messages=can_send_messages)
    try:
        await bot.restrict_chat_member(chat_id, user_id, perms, until_date=until_date)
    except Exception as e:
        logger.exception("Failed to restrict %s in %s: %s", user_id, chat_id, e)


def parse_duration_to_seconds(text: str) -> Optional[int]:
    if text == "навсегда" or text == "forever":
        return None
    # parse like "1ч" "6ч" "12ч" "24ч", or "10м"
    m = re.match(r"^(\d+)\s*([мmчhH])$", text)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        if unit in ("м", "m"):
            return val * 60
        if unit in ("ч", "h"):
            return val * 3600
    # if plain minutes/seconds
    m2 = re.match(r"^(\d+)\s*(s|sec|сек)$", text)
    if m2:
        return int(m2.group(1))
    return None


# Filters
async def check_caps_filter(text: str) -> bool:
    # returns True if should be considered caps violation (>70% letters uppercase)
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    up = sum(1 for c in letters if c.isupper())
    ratio = up / len(letters)
    return ratio >= 0.7


def contains_link(text: str) -> bool:
    return bool(re.search(r"https?://|t\.me\/|telegram\.me", text, flags=re.IGNORECASE))


# Flood/repeat detection
def add_message_flood(chat_id: int, user_id: int) -> int:
    key = (chat_id, user_id)
    now = asyncio.get_event_loop().time()
    arr = _flood_cache.get(key, [])
    arr = [t for t in arr if now - t <= 5.0]
    arr.append(now)
    _flood_cache[key] = arr
    return len(arr)


# Commands and Handlers

# Start and help
@dp.message(Command(commands=["start", "help"]))
async def cmd_start(message: Message):
    await ensure_user(message.from_user)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Админ-панель", callback_data="admin_panel")],
        [InlineKeyboardButton(text="Игры", callback_data="games_menu"), InlineKeyboardButton(text="Аналитика", callback_data="analytics_menu")]
    ])
    await message.reply(
        "Привет! Я Sialens — учебный модератор и помощник.\n\nДоступные команды (часть):\n"
        ".mute, .warn, .ban, .unban, .kick, .clean, .ai, .revo, .guess, .rps, .poll, .remind, .addtrigger\n\n"
        "Всё управление через инлайн-кнопки и команды.",
        reply_markup=kb
    )


# Admin panel via inline
@dp.callback_query(Text(startswith="admin_panel"))
async def callback_admin_panel(query: CallbackQuery):
    user_id = query.from_user.id
    # Check admin
    if user_id not in ADMIN_IDS_LIST:
        await query.answer("Доступно только админам", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Управление премиумом", callback_data="admin_premium")],
        [InlineKeyboardButton("Управление ворнами", callback_data="admin_warns")],
        [InlineKeyboardButton("Настройки чата", callback_data="admin_config")],
        [InlineKeyboardButton("Закрыть", callback_data="close")]
    ])
    await query.message.edit_text("Админ-панель", reply_markup=kb)
    await query.answer()


@dp.callback_query(Text(startswith="close"))
async def callback_close(query: CallbackQuery):
    try:
        await query.message.delete()
    except Exception:
        await query.answer("Закрыто")
    else:
        await query.answer("Закрыто")


# Moderation commands
@dp.message(Command(commands=["mute"]))
async def cmd_mute(message: Message):
    # usage: .mute @user 1ч или reply: .mute 1ч
    if not message.chat.type in ("group", "supergroup"):
        await message.reply("Команда в чате.")
        return
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    args = message.get_args().split()
    duration_arg = None
    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        if args:
            duration_arg = args[0]
    else:
        # try to parse first arg as mention or id and second as duration
        if len(args) >= 2:
            mention = args[0]
            duration_arg = args[1]
            if mention.startswith("@"):
                # try to resolve - naive
                username = mention[1:]
                # Could use get_chat_member but skip
            else:
                try:
                    target_id = int(mention)
                except Exception:
                    target_id = None
                if target_id:
                    target = types.User(id=target_id, is_bot=False, first_name="", last_name=None, username=None)
    if not target:
        await message.reply("Укажите пользователя через ответ на сообщение или аргументом.")
        return
    secs = parse_duration_to_seconds(duration_arg) if duration_arg else parse_duration_to_seconds("1ч")
    await restrict_user(message.chat.id, target.id, secs)
    await message.reply(f"🔇 Пользователь {get_user_mention(target)} замьючен на {duration_arg or '1ч'}.")


def get_user_mention(u: types.User) -> str:
    name = u.first_name or u.username or str(u.id)
    return f"<a href='tg://user?id={u.id}'>{name}</a>"


@dp.message(Command(commands=["warn"]))
async def cmd_warn(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя, чтобы выдать warn.")
        return
    target = message.reply_to_message.from_user
    reason = message.get_args() or "Нарушение"
    count = await add_warn(message.chat.id, target.id, message.from_user.id, reason)
    await message.reply(f"⚠️ {get_user_mention(target)} предупреждён. Всего варнов: {count}")
    # auto ban after 3 warns
    if count >= 3:
        await message.reply("3 варна — автоматический бан на 24ч.")
        await ban_user(message.chat.id, target.id, 24 * 3600, "3 warns auto-ban")
        try:
            await bot.ban_chat_member(message.chat.id, target.id, until_date=int((datetime.utcnow() + timedelta(hours=24)).timestamp()))
        except Exception as e:
            logger.exception("ban error: %s", e)


@dp.message(Command(commands=["resetwarns"]))
async def cmd_resetwarns(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    await reset_warns(message.chat.id, target.id)
    await message.reply(f"Warns для {get_user_mention(target)} сброшены.")


@dp.message(Command(commands=["ban"]))
async def cmd_ban(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    args = message.get_args().split()
    reason = " ".join(args[1:]) if len(args) > 1 else ""
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        duration_arg = args[0] if args else None
    else:
        await message.reply("Ответьте на сообщение пользователя или укажите id")
        return
    secs = parse_duration_to_seconds(duration_arg) if duration_arg else parse_duration_to_seconds("24ч")
    await ban_user(message.chat.id, target.id, secs, reason)
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
    except Exception as e:
        logger.exception("ban_chat_member: %s", e)
    await message.reply(f"⛔ Пользователь {get_user_mention(target)} забанен. Причина: {reason or 'не указана'}")


@dp.message(Command(commands=["unban"]))
async def cmd_unban(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    if message.reply_to_message:
        target = message.reply_to_message.from_user
    else:
        args = message.get_args().split()
        if not args:
            await message.reply("Укажите id пользователя или ответьте на сообщение.")
            return
        try:
            tid = int(args[0])
        except:
            await message.reply("Неверный id.")
            return
        target = types.User(id=tid, is_bot=False, first_name="")
    try:
        await bot.unban_chat_member(message.chat.id, target.id)
    except Exception as e:
        logger.exception("unban_chat_member: %s", e)
    await unban_user_db(message.chat.id, target.id)
    await message.reply(f"✅ {get_user_mention(target)} разбанен.")


@dp.message(Command(commands=["kick"]))
async def cmd_kick(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
    except Exception:
        pass
    await message.reply(f"👢 {get_user_mention(target)} кикнут.")


@dp.message(Command(commands=["clean"]))
async def cmd_clean(message: Message):
    # .clean 10 or reply .clean
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    args = message.get_args().strip()
    if args.isdigit():
        count = int(args)
        # Use get_chat_history? Bot API doesn't support deletion by count directly.
        # Here we'll attempt to delete last `count` messages including command.
        # Note: Bots can only delete messages they can see.
        deleted = 0
        async for m in bot.get_chat_history(message.chat.id, limit=count + 1):
            try:
                await bot.delete_message(message.chat.id, m.message_id)
                deleted += 1
            except Exception:
                pass
        await message.answer(f"Удалено приблизительно {deleted} сообщений.")
    elif message.reply_to_message:
        # delete the replied message only
        try:
            await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
            await message.reply("Удалено.")
        except Exception:
            await message.reply("Не получилось удалить.")
    else:
        await message.reply("Использование: .clean <кол-во> или ответ на сообщение.")


# Filters handling for incoming messages
@dp.message()
async def on_message_handler(message: Message):
    # track stats and points
    try:
        if message.chat.type in ("group", "supergroup"):
            await inc_user_points(message.chat.id, message.from_user, 1)
    except Exception as e:
        logger.exception("inc_user_points: %s", e)

    # store messages for AI context
    try:
        await add_message_for_context(message.chat.id, message.from_user.id, message.from_user.username or message.from_user.first_name or "", message.text or "")
    except Exception:
        pass

    # basic filter checks
    try:
        # get chat config
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT caps_filter, links_filter, repeat_filter, stickers_filter, swears_filter FROM chat_config WHERE chat_id = ?", (message.chat.id,)) as cur:
                row = await cur.fetchone()
                if row:
                    caps_filter, links_filter, repeat_filter, stickers_filter, swears_filter = row
                else:
                    caps_filter = links_filter = repeat_filter = stickers_filter = swears_filter = 1
        # Check flood
        if message.chat.type in ("group", "supergroup"):
            cnt = add_message_flood(message.chat.id, message.from_user.id)
            if cnt >= 3:
                # auto-mute for 1 minute
                await restrict_user(message.chat.id, message.from_user.id, 60)
                await message.reply(f"🔇 {get_user_mention(message.from_user)} автоматический мут за флуд.")
                return

        # caps
        if caps_filter and message.text and await check_caps_filter(message.text):
            # warn or delete
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            await message.reply(f"Пожалуйста, не писать капсом, {get_user_mention(message.from_user)}.")
            return

        # links
        if links_filter and message.text and contains_link(message.text):
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            await message.reply(f"Ссылки запрещены в этом чате.")
            return

        # stickers/gifs filter
        if stickers_filter and (message.sticker or message.animation or message.document and message.document.mime_type and "gif" in (message.document.mime_type)):
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            await message.reply("Стикеры и гифки запрещены.")
            return

        # profanity simple check
        if swears_filter and message.text:
            # naive swear list
            bad = ["badword1", "badword2", "бля", "сука"]
            low = message.text.lower()
            if any(w in low for w in bad):
                await add_warn(message.chat.id, message.from_user.id, 0, "swear")
                await bot.delete_message(message.chat.id, message.message_id)
                await message.reply("Мат запрещён.")
                return

        # triggers: check triggers table
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT response FROM triggers WHERE chat_id = ? AND ? LIKE '%' || keyword || '%'", (message.chat.id, message.text or "")) as cur:
                row = await cur.fetchone()
                if row:
                    resp = row[0]
                    await message.reply(resp)
    except Exception as e:
        logger.exception("on_message_handler error: %s", e)


# AI command
@dp.message(Command(commands=["ai"]))
async def cmd_ai(message: Message):
    prompt = message.get_args()
    if not prompt:
        await message.reply("Использование: .ai <текст>")
        return
    # check premium or trial - simplified: admins and premium users allowed for now
    # build context
    ctx = await get_ai_context(message.chat.id, 15)
    # create prompt
    combined = ""
    for c in ctx:
        un = c.get("username") or str(c.get("user_id"))
        combined += f"{un}: {c.get('text')}\n"
    combined += f"User: {prompt}\nAssistant:"
    await message.reply_chat_action(ChatActions.TYPING)
    if genai is None or GEMINI_API_KEY is None:
        await message.reply("AI недоступен (ключ или библиотека не настроены).")
        return
    try:
        # call generative ai
        resp = genai.generate_text(model="models/text-bison-001",  # model name may vary
                                  prompt=combined,
                                  temperature=0.7,
                                  max_output_tokens=512)
        text = resp.text if hasattr(resp, "text") else str(resp)
        await message.reply(text)
    except Exception as e:
        logger.exception("Gemini error: %s", e)
        await message.reply("Ошибка при обращении к AI.")


# Triggers
@dp.message(Command(commands=["addtrigger"]))
async def cmd_addtrigger(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    # usage: .addtrigger "ключ" "ответ"
    args = re.findall(r'"(.*?)"', message.text)
    if len(args) < 2:
        await message.reply('Использование: .addtrigger "ключ" "ответ"')
        return
    keyword, response = args[0], args[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO triggers (chat_id, keyword, response) VALUES (?, ?, ?)", (message.chat.id, keyword, response))
        await db.commit()
    await message.reply(f"Триггер добавлен: {keyword} -> {response}")


@dp.message(Command(commands=["removetrigger"]))
async def cmd_removetrigger(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    args = re.findall(r'"(.*?)"', message.text)
    if len(args) < 1:
        await message.reply('Использование: .removetrigger "ключ"')
        return
    keyword = args[0]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM triggers WHERE chat_id = ? AND keyword = ?", (message.chat.id, keyword))
        await db.commit()
    await message.reply(f"Триггер {keyword} удалён.")


@dp.message(Command(commands=["trigger"]))
async def cmd_trigger_list(message: Message):
    # .trigger list
    if message.get_args().strip() == "list" or not message.get_args().strip():
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, keyword, response FROM triggers WHERE chat_id = ?", (message.chat.id,)) as cur:
                rows = await cur.fetchall()
        if not rows:
            await message.reply("Триггеров нет.")
            return
        out = "\n".join([f"{r[0]}. {r[1]} -> {r[2]}" for r in rows])
        await message.reply("Триггеры:\n" + out)


# Polls
@dp.message(Command(commands=["poll"]))
async def cmd_poll(message: Message):
    # usage: .poll "вопрос" "вар1" "вар2" ...
    args = re.findall(r'"(.*?)"', message.text)
    if len(args) < 2:
        await message.reply('Использование: .poll "вопрос" "вариант1" "вариант2" ...')
        return
    question = args[0]
    options = args[1:]
    try:
        await bot.send_poll(message.chat.id, question, options, is_anonymous=False, allows_multiple_answers=False)
    except Exception as e:
        logger.exception("send_poll error: %s", e)
        await message.reply("Не удалось создать опрос.")


# Remind
@dp.message(Command(commands=["remind"]))
async def cmd_remind(message: Message):
    # .remind "текст" 10м
    args = re.findall(r'"(.*?)"', message.text)
    if len(args) < 1:
        await message.reply('Использование: .remind "текст" 10м')
        return
    text = args[0]
    rest = message.text.split('"')[-1].strip()
    m = re.match(r"(\d+)\s*([мmчh]?)", rest)
    if not m:
        await message.reply("Укажите время. Пример: 10м")
        return
    val = int(m.group(1))
    unit = m.group(2).lower() or "м"
    seconds = val * 60 if unit in ("м", "m") else val * 3600
    remind_at = datetime.utcnow() + timedelta(seconds=seconds)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO reminders (chat_id, user_id, text, remind_at) VALUES (?, ?, ?, ?)",
                               (message.chat.id, message.from_user.id, text, remind_at.isoformat()))
        await db.commit()
        rid = cur.lastrowid
    task = asyncio.create_task(reminder_task(rid, message.chat.id, message.from_user.id, text, seconds))
    _reminder_tasks[rid] = task
    await message.reply(f"Напоминание установлено на {val}{unit}.")


# Switch layout (.sw simple transliteration between ru/en)
TRANSLIT_MAP = {
    # from RU to EN layout (qwerty mapping)
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u", "ш": "i", "щ": "o", "з": "p",
    "х": "[", "ъ": "]", "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g", "р": "h", "о": "j", "л": "k",
    "д": "l", "ж": ";", "э": "'", "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b", "т": "n", "ь": "m",
    "б": ",", "ю": "."
}
@dp.message(Command(commands=["sw"]))
async def cmd_sw(message: Message):
    text = message.get_args()
    if not text and message.reply_to_message:
        text = message.reply_to_message.text or ""
    if not text:
        await message.reply("Использование: .sw <текст> или ответ на сообщение.")
        return
    out = ""
    for ch in text:
        low = ch.lower()
        if low in TRANSLIT_MAP:
            mapped = TRANSLIT_MAP[low]
            out += mapped.upper() if ch.isupper() else mapped
        else:
            out += ch
    await message.reply(out)


# Games (simple implementations)
@dp.message(Command(commands=["rps"]))
async def cmd_rps(message: Message):
    # rock-paper-scissors: .rps <rock/paper/scissors>
    arg = message.get_args().strip().lower()
    opts = ["rock", "paper", "scissors"]
    if not arg or arg not in opts:
        await message.reply("Использование: .rps <rock|paper|scissors>")
        return
    import random
    bot_choice = random.choice(opts)
    res = "Ничья"
    if arg == bot_choice:
        res = "Ничья"
    elif (arg == "rock" and bot_choice == "scissors") or (arg == "paper" and bot_choice == "rock") or (arg == "scissors" and bot_choice == "paper"):
        res = "Вы выиграли!"
    else:
        res = "Бот выиграл!"
    await message.reply(f"Вы: {arg}\nБот: {bot_choice}\n\n{res}")


@dp.message(Command(commands=["guess"]))
async def cmd_guess(message: Message):
    # .guess начальное - бот загадывает 1-100, запоминаем в memory (simplified)
    # For simplicity, we'll do single-message random guess game
    import random
    number = random.randint(1, 10)
    await message.reply(f"Я загадал число от 1 до 10. Угадайте! (Отправьте число)")

    def check(m: Message):
        return m.from_user.id == message.from_user.id and m.chat.id == message.chat.id

    try:
        ans: Message = await dp.wait_for(types.Message, timeout=20.0, check=check)  # type: ignore
        try:
            val = int(ans.text.strip())
            if val == number:
                await ans.reply("Угадали! 🎉")
            else:
                await ans.reply(f"Не угадали. Я загадал {number}.")
        except Exception:
            await ans.reply("Некорректный ввод.")
    except asyncio.TimeoutError:
        await message.reply("Время вышло.")


@dp.message(Command(commands=["revo"]))
async def cmd_revo(message: Message):
    # Russian roulette: 6 chambers, 1 bullet
    import random
    chamber = random.randint(1, 6)
    if chamber == 1:
        await message.reply("💥 Вы проиграли!")
    else:
        await message.reply("Удача! Вы выжили.")


# Analytics: .stats, .top, .nightowl, .peak
@dp.message(Command(commands=["stats"]))
async def cmd_stats(message: Message):
    # show user's stats in this chat
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT messages FROM stats WHERE chat_id = ? AND user_id = ?", (message.chat.id, uid)) as cur:
            row = await cur.fetchone()
    msgs = row[0] if row else 0
    await message.reply(f"Статистика для {get_user_mention(message.from_user)}:\nСообщений в этом чате: {msgs}")


@dp.message(Command(commands=["top"]))
async def cmd_top(message: Message):
    # top 10 by messages in chat
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, messages FROM stats WHERE chat_id = ? ORDER BY messages DESC LIMIT 10", (message.chat.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.reply("Нет статистики.")
        return
    parts = []
    for r in rows:
        uid, cnt = r
        parts.append(f"{uid} — {cnt}")
    await message.reply("Топ по сообщениям:\n" + "\n".join(parts))


@dp.message(Command(commands=["nightowl"]))
async def cmd_nightowl(message: Message):
    # find users who message between 00:00-06:00 UTC (approx)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, last_message_at FROM stats WHERE chat_id = ?", (message.chat.id,)) as cur:
            rows = await cur.fetchall()
    out = []
    for r in rows:
        uid, last_at = r
        if last_at:
            try:
                dt = datetime.fromisoformat(last_at)
                if 0 <= dt.hour <= 6:
                    out.append(str(uid))
            except Exception:
                pass
    await message.reply("Ночные совы:\n" + (", ".join(out) if out else "Нет данных"))


# Payments (Telegram Stars) - simplistic handlers
@dp.pre_checkout_query()
async def pre_checkout(pre_checkout: types.PreCheckoutQuery):
    # Accept all
    await pre_checkout.answer(ok=True)


@dp.message(Text(startswith="pay"))
async def pay_command(message: Message):
    # usage: pay <amount>
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: pay <amount>")
        return
    try:
        amount = int(parts[1])
    except:
        await message.reply("Некорректная сумма.")
        return
    # This is a placeholder: In production you'd use actual invoice and provider_token
    prices = [LabeledPrice(label="Stars", amount=amount * 100)]  # amount in cents of currency unit
    # to send invoice you need provider_token set and bot payments enabled
    await message.reply("Платежная функция в демо отключена. Это учебный проект.")


@dp.message(content_types=types.ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message):
    # record payment
    pay: types.SuccessfulPayment = message.successful_payment  # type: ignore
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO payments (user_id, chat_id, amount, currency, provider_payment_charge_id) VALUES (?,?,?,?,?)",
                         (message.from_user.id, message.chat.id, pay.total_amount, pay.currency, pay.provider_payment_charge_id))
        await db.commit()
    # grant premium (simple: +30 days)
    until = datetime.utcnow() + timedelta(days=30)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (until.isoformat(), message.from_user.id))
        await db.commit()
    await message.reply("Спасибо за оплату! Премиум активирован на 30 дней.")


# Admin direct commands
@dp.message(Command(commands=["givepremium"]))
async def cmd_givepremium(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только админы.")
        return
    # usage: /givepremium @user 30d
    matches = re.findall(r"@?([A-Za-z0-9_]+)|(\d+)", message.get_args())
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    # default 30 days
    await message.reply("Премиум выдан (упрощенно).")
    until = datetime.utcnow() + timedelta(days=30)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (until.isoformat(), target.id))
        await db.commit()


@dp.message(Command(commands=["addadmin"]))
async def cmd_addadmin(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только корень.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя.")
        return
    uid = message.reply_to_message.from_user.id
    if uid not in ADMIN_IDS_LIST:
        ADMIN_IDS_LIST.append(uid)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (uid, message.reply_to_message.from_user.username))
        await db.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (uid,))
        await db.commit()
    await message.reply("Добавлен админ.")


@dp.message(Command(commands=["removeadmin"]))
async def cmd_removeadmin(message: Message):
    if message.from_user.id not in ADMIN_IDS_LIST:
        await message.reply("Только корень.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя.")
        return
    uid = message.reply_to_message.from_user.id
    if uid in ADMIN_IDS_LIST:
        ADMIN_IDS_LIST.remove(uid)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (uid,))
        await db.commit()
    await message.reply("Удалён админ.")


# Greeting and farewell
@dp.message()
async def on_new_members_echo(message: Message):
    # join/left events
    try:
        if message.new_chat_members:
            for m in message.new_chat_members:
                # auto greet
                await message.reply(f"Добро пожаловать, {get_user_mention(m)}! Пожалуйста, представьтесь.")
        if message.left_chat_member:
            m = message.left_chat_member
            await message.reply(f"Прощай, {m.full_name if hasattr(m, 'full_name') else m.first_name}.")
    except Exception:
        pass


# Bot startup
async def on_startup():
    logger.info("Starting Sialens bot...")
    await init_db()
    logger.info("DB initialized.")
    # other startup tasks here


# Shutdown
async def on_shutdown():
    logger.info("Shutting down Sialens...")


if __name__ == "__main__":
    import asyncio
    async def main():
        await on_startup()
        try:
            # Start polling
            await dp.start_polling(bot)
        finally:
            await on_shutdown()
    asyncio.run(main())