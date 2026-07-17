from aiogram import Router, Bot
from aiogram.types import ChatMemberUpdated

from db.database import upsert_chat

router = Router()


@router.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated, bot: Bot):
    new_status = event.new_chat_member.status
    if new_status not in ("member", "administrator"):
        return

    chat = event.chat
    await upsert_chat(chat.id, chat.title or "")

    if new_status == "member":
        # Бота добавили, но не сделали админом — многие игры (особенно Мафия
        # с рассылкой в ЛС и модерацией) работают лучше с правами админа.
        try:
            await bot.send_message(
                chat.id,
                "👋 Спасибо, что добавили меня!\n\n"
                "Чтобы все игры работали без ограничений (закрепление сообщений, "
                "модерация), выдайте мне права администратора.\n\n"
                "Затем напишите /start, чтобы начать.",
            )
        except Exception:
            pass
