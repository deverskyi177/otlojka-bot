import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from db.database import init_db
from utils.scheduler import setup_scheduler

from handlers import common, admin, reminders, my_chat_member
from games import (
    number_game,
    mafia_game,
    roulette_game,
    dice_game,
    rps_game,
    hangman_game,
    bulls_cows_game,
    reaction_game,
    tictactoe_game,
    wheel_game,
)

logging.basicConfig(level=logging.INFO)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Проверь .env файл.")

    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # ВАЖНО: admin-роутер регистрируется первым, чтобы /admin перехватывался
    # до общих хендлеров команд.
    dp.include_router(admin.router)
    dp.include_router(my_chat_member.router)
    dp.include_router(common.router)
    dp.include_router(reminders.router)

    # Игры
    dp.include_router(number_game.router)
    dp.include_router(mafia_game.router)
    dp.include_router(roulette_game.router)
    dp.include_router(dice_game.router)
    dp.include_router(rps_game.router)
    dp.include_router(hangman_game.router)
    dp.include_router(bulls_cows_game.router)
    dp.include_router(reaction_game.router)
    dp.include_router(tictactoe_game.router)
    dp.include_router(wheel_game.router)

    scheduler = setup_scheduler(bot)
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
