import random
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db.database import log_game_result

router = Router()

MIN_PLAYERS = 4
NIGHT_PHASE = "night"
DAY_PHASE = "day"

lobbies: dict[int, dict] = {}
# chat_id -> {
#   "players": {user_id: name},
#   "roles": {user_id: "mafia"/"citizen"/"doctor"/"detective"},
#   "alive": set(user_id),
#   "phase": "night"/"day",
#   "night_kill": user_id or None,
#   "doctor_save": user_id or None,
#   "votes": {voter_id: target_id},
#   "started": bool,
# }


def lobby_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Присоединиться", callback_data="mafia:join")],
        [InlineKeyboardButton(text="▶️ Начать игру", callback_data="mafia:launch")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:games")],
    ])


def assign_roles(player_ids: list[int]) -> dict:
    n = len(player_ids)
    mafia_count = max(1, n // 4)
    shuffled = player_ids[:]
    random.shuffle(shuffled)

    roles = {}
    for uid in shuffled[:mafia_count]:
        roles[uid] = "mafia"

    rest = shuffled[mafia_count:]
    if rest:
        roles[rest[0]] = "doctor"
    if len(rest) > 1:
        roles[rest[1]] = "detective"
    for uid in rest[2:]:
        roles[uid] = "citizen"

    return roles


ROLE_NAMES = {
    "mafia": "🕵️ Мафия",
    "citizen": "👤 Мирный житель",
    "doctor": "💉 Доктор",
    "detective": "🔍 Детектив",
}

ROLE_DESCRIPTIONS = {
    "mafia": "Ночью выбирай, кого устранить. Твоя цель — уравнять число мафии и мирных.",
    "citizen": "Днём вычисляй мафию и голосуй за казнь. Ночью просто спи.",
    "doctor": "Ночью выбирай, кого спасти от мафии.",
    "detective": "Ночью можешь проверить одного игрока — мафия он или нет.",
}


@router.callback_query(F.data == "game:mafia")
async def mafia_menu(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if callback.message.chat.type == "private":
        await callback.answer("🕵️ Мафия играется только в групповых чатах!", show_alert=True)
        return

    lobbies[chat_id] = {
        "players": {callback.from_user.id: callback.from_user.first_name},
        "started": False,
    }
    await callback.message.edit_text(
        "🕵️ <b>Мафия</b>\n\n"
        f"Лобби создано! Минимум игроков: {MIN_PLAYERS}\n\n"
        f"Участники (1):\n• {callback.from_user.first_name}",
        reply_markup=lobby_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "mafia:join")
async def mafia_join(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    lobby = lobbies.get(chat_id)
    if not lobby or lobby["started"]:
        await callback.answer("Лобби не найдено или игра уже началась", show_alert=True)
        return

    lobby["players"][callback.from_user.id] = callback.from_user.first_name
    names = "\n".join(f"• {n}" for n in lobby["players"].values())
    await callback.message.edit_text(
        "🕵️ <b>Мафия</b>\n\n"
        f"Лобби создано! Минимум игроков: {MIN_PLAYERS}\n\n"
        f"Участники ({len(lobby['players'])}):\n{names}",
        reply_markup=lobby_kb(),
    )
    await callback.answer("Ты в игре!")


@router.callback_query(F.data == "mafia:launch")
async def mafia_launch(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    lobby = lobbies.get(chat_id)
    if not lobby or lobby["started"]:
        await callback.answer("Лобби не найдено", show_alert=True)
        return
    if len(lobby["players"]) < MIN_PLAYERS:
        await callback.answer(f"Нужно минимум {MIN_PLAYERS} игрока", show_alert=True)
        return

    player_ids = list(lobby["players"].keys())
    roles = assign_roles(player_ids)

    lobby.update({
        "roles": roles,
        "alive": set(player_ids),
        "phase": NIGHT_PHASE,
        "night_kill": None,
        "doctor_save": None,
        "detective_check": None,
        "votes": {},
        "started": True,
    })

    # Раздаём роли в личку — если ЛС недоступны, предупреждаем в чате
    failed = []
    for uid, role in roles.items():
        try:
            await bot.send_message(
                uid,
                f"🎭 <b>Твоя роль: {ROLE_NAMES[role]}</b>\n\n{ROLE_DESCRIPTIONS[role]}\n\n"
                "Игра началась в чате — следи за сообщениями там.",
            )
        except Exception:
            failed.append(lobby["players"][uid])

    text = (
        "🌙 <b>Игра началась! Наступает ночь.</b>\n\n"
        "Роли разосланы в личные сообщения.\n"
        f"Мафия ({sum(1 for r in roles.values() if r == 'mafia')}) выбирает жертву.\n\n"
        "Ждите утра — результаты ночи объявит бот."
    )
    if failed:
        text += (
            "\n\n⚠️ Не удалось написать в ЛС: " + ", ".join(failed) +
            "\nПопросите их сначала написать боту /start в личке."
        )

    await callback.message.edit_text(text)
    await callback.answer()

    # Упрощение: ночная фаза — авто-переход через голосование в общем чате днём.
    # Полноценная ночная логика (выбор жертвы мафией в ЛС) требует отдельного
    # маршрута callback'ов с проверкой роли — заложено ниже через day_vote.
    await start_day_vote(callback, chat_id, bot)


async def start_day_vote(callback: CallbackQuery, chat_id: int, bot: Bot):
    lobby = lobbies[chat_id]
    lobby["phase"] = DAY_PHASE
    lobby["votes"] = {}

    alive_ids = list(lobby["alive"])
    kb_rows = []
    for uid in alive_ids:
        name = lobby["players"][uid]
        kb_rows.append([InlineKeyboardButton(text=f"🗳 {name}", callback_data=f"mafia:vote:{uid}")])
    kb_rows.append([InlineKeyboardButton(text="📊 Итоги голосования", callback_data="mafia:tally")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await bot.send_message(
        chat_id,
        "☀️ <b>Наступил день!</b>\n\n"
        "Обсудите, кто похож на мафию, и проголосуйте за казнь.",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("mafia:vote:"))
async def mafia_vote(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    lobby = lobbies.get(chat_id)
    if not lobby or not lobby.get("started"):
        await callback.answer("Игра не найдена", show_alert=True)
        return
    if callback.from_user.id not in lobby["alive"]:
        await callback.answer("Ты выбыл из игры и не можешь голосовать", show_alert=True)
        return

    target_id = int(callback.data.split(":")[2])
    lobby["votes"][callback.from_user.id] = target_id
    await callback.answer(f"Голос учтён: {lobby['players'][target_id]}")


@router.callback_query(F.data == "mafia:tally")
async def mafia_tally(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    lobby = lobbies.get(chat_id)
    if not lobby or not lobby.get("started"):
        await callback.answer("Игра не найдена", show_alert=True)
        return

    votes = lobby["votes"]
    if not votes:
        await callback.answer("Ещё никто не проголосовал", show_alert=True)
        return

    tally: dict[int, int] = {}
    for target in votes.values():
        tally[target] = tally.get(target, 0) + 1

    executed_id = max(tally, key=tally.get)
    executed_name = lobby["players"][executed_id]
    executed_role = lobby["roles"][executed_id]
    lobby["alive"].discard(executed_id)

    result_text = (
        f"⚖️ <b>Казнён: {executed_name}</b>\n"
        f"Роль: {ROLE_NAMES[executed_role]}\n\n"
    )

    mafia_alive = [uid for uid in lobby["alive"] if lobby["roles"][uid] == "mafia"]
    citizens_alive = [uid for uid in lobby["alive"] if lobby["roles"][uid] != "mafia"]

    if not mafia_alive:
        result_text += "🎉 <b>Мирные жители победили!</b>"
        await callback.message.edit_text(result_text)
        for uid in citizens_alive:
            await log_game_result("mafia", chat_id, uid)
        del lobbies[chat_id]
    elif len(mafia_alive) >= len(citizens_alive):
        result_text += "🕵️ <b>Мафия победила!</b>"
        await callback.message.edit_text(result_text)
        for uid in mafia_alive:
            await log_game_result("mafia", chat_id, uid)
        del lobbies[chat_id]
    else:
        result_text += "Игра продолжается — новое голосование:"
        await callback.message.edit_text(result_text)
        await start_day_vote(callback, chat_id, bot)

    await callback.answer()
