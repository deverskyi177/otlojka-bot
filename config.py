import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ID владельца бота — только он видит /admin
# Узнать свой ID можно у @userinfobot в Telegram
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "bot.db")

FEEDBACK_USERNAME = os.getenv("FEEDBACK_USERNAME", "deverskyi")
