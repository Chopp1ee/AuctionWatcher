# config.py
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "auctions.db")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Пошук нових лотів за підписками
FEED_INTERVAL = int(os.getenv("FEED_INTERVAL", 60))          # опитування фіду Prozorro, сек
MAX_SUBS_PER_USER = int(os.getenv("MAX_SUBS_PER_USER", 5))   # підписок на користувача
DAILY_NOTIFY_LIMIT = int(os.getenv("DAILY_NOTIFY_LIMIT", 50))  # лотів на підписку за добу
RETRO_DAYS = int(os.getenv("RETRO_DAYS", 7))                 # глибина ретроспективи

# Лічильник ставок (тільки для адмінів)
BID_WATCH_INTERVAL = int(os.getenv("BID_WATCH_INTERVAL", 30))  # опитування лотів під наглядом, сек
BID_SUMMARY_LEAD_MINUTES = int(os.getenv("BID_SUMMARY_LEAD_MINUTES", 60))  # за скільки хв до дедлайну слати підсумок

# Посилання
PROZORRO_LOT_URL = "https://prozorro.sale/auction/{auction_id}"
NRC_LOT_URL = "https://nrcukraine.com.ua/uk/{selling_method}/procedures/view?id={auction_id}"

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не знайдено в .env файлі!")