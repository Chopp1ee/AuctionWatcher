# config.py
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "auctions.db")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не знайдено в .env файлі!")