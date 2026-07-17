import aiosqlite
import json
import time
from config import DB_PATH

# ---------------------------------------------------------------------------
# Тексты и кнопки по умолчанию — всё это редактируется потом через /admin
# ---------------------------------------------------------------------------
DEFAULT_TEXTS = {
    "welcome": (
        "👋 <b>Привет! Я — игровой бот</b>\n\n"
        "Добавь меня в любой чат (с правами админа), и твои друзья "
        "смогут играть прямо там: Мафия, Число, Рулетка и ещё 7 игр.\n\n"
        "Выбирай игру снизу 👇"
    ),
    "farewell": "👋 До встречи! Возвращайся, когда захочется поиграть.",
    "help": (
        "ℹ️ <b>Как играть</b>\n\n"
        "1. Добавь бота в чат и выдай права администратора\n"
        "2. Напиши /start в чате\n"
        "3. Выбери игру и следуй инструкциям на кнопках\n\n"
        "Есть идея новой игры или вопрос? Жми «Обратная связь» в меню."
    ),
    "btn_games": "🎮 Игры",
    "btn_help": "ℹ️ Помощь",
    "btn_feedback": "💬 Обратная связь",
    "btn_back": "⬅️ Назад",
    "btn_reminders": "⏰ Напоминания",
}

DEFAULT_EMOJI = {
    "number": "🔢",
    "mafia": "🕵️",
    "roulette": "🎡",
    "dice": "🎲",
    "rps": "✂️",
    "hangman": "📝",
    "bulls_cows": "🐮",
    "reaction": "⚡",
    "tictactoe": "❌",
    "wheel": "🎁",
    "back": "⬅️",
    "vs_bot": "🤖",
    "vs_player": "👤",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    is_premium INTEGER DEFAULT 0,
    joined_at INTEGER,
    games_played INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    chat_id INTEGER,
    text TEXT,
    remind_at INTEGER,
    is_done INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS game_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_name TEXT,
    chat_id INTEGER,
    winner_id INTEGER,
    played_at INTEGER
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

        # Инициализация текстов/кнопок/эмодзи, если их ещё нет
        cur = await db.execute("SELECT value FROM settings WHERE key = 'texts'")
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES ('texts', ?)",
                (json.dumps(DEFAULT_TEXTS, ensure_ascii=False),),
            )
        cur = await db.execute("SELECT value FROM settings WHERE key = 'emoji'")
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES ('emoji', ?)",
                (json.dumps(DEFAULT_EMOJI, ensure_ascii=False),),
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Пользователи / чаты
# ---------------------------------------------------------------------------
async def upsert_user(user_id: int, username: str, first_name: str, is_premium: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, username, first_name, is_premium, joined_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   first_name=excluded.first_name,
                   is_premium=excluded.is_premium""",
            (user_id, username, first_name, int(is_premium), int(time.time())),
        )
        await db.commit()


async def upsert_chat(chat_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO chats (chat_id, title, added_at) VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
            (chat_id, title, int(time.time())),
        )
        await db.commit()


async def get_stats_summary() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        users_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM chats")
        chats_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM game_stats")
        games_count = (await cur.fetchone())[0]
        return {"users": users_count, "chats": chats_count, "games": games_count}


async def log_game_result(game_name: str, chat_id: int, winner_id: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO game_stats (game_name, chat_id, winner_id, played_at) VALUES (?, ?, ?, ?)",
            (game_name, chat_id, winner_id, int(time.time())),
        )
        if winner_id:
            await db.execute(
                "UPDATE users SET wins = wins + 1, games_played = games_played + 1 WHERE user_id = ?",
                (winner_id,),
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Настройки (тексты / кнопки / эмодзи) — редактируются из /admin
# ---------------------------------------------------------------------------
async def get_setting(key: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row else {}


async def set_setting_value(key: str, field: str, value: str):
    data = await get_setting(key)
    data[field] = value
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, json.dumps(data, ensure_ascii=False)),
        )
        await db.commit()


async def get_text(field: str) -> str:
    texts = await get_setting("texts")
    return texts.get(field, DEFAULT_TEXTS.get(field, ""))


async def get_emoji(field: str) -> str:
    emoji = await get_setting("emoji")
    return emoji.get(field, DEFAULT_EMOJI.get(field, ""))


# ---------------------------------------------------------------------------
# Напоминания
# ---------------------------------------------------------------------------
async def add_reminder(user_id: int, chat_id: int, text: str, remind_at: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO reminders (user_id, chat_id, text, remind_at) VALUES (?, ?, ?, ?)",
            (user_id, chat_id, text, remind_at),
        )
        await db.commit()
        return cur.lastrowid


async def get_due_reminders(now_ts: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, chat_id, text FROM reminders WHERE is_done = 0 AND remind_at <= ?",
            (now_ts,),
        )
        return await cur.fetchall()


async def mark_reminder_done(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET is_done = 1 WHERE id = ?", (reminder_id,))
        await db.commit()
