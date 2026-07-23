# api.py
import re
import requests
import aiohttp
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from logger import logger


# Словник для перекладу статусів
STATUS_TRANSLATIONS = {
    # Основні статуси
    "active": "🟢 Активний",
    "active_tendering": "📝 Подання заявок на участь",
    "active_rectification": "🟡 Виправлення умов",
    "active_auction": "🔨 Аукціон триває",
    "active_qualification": "📋 Кваліфікація учасників",
    "active_awarded": "🏆 Переможця визначено",
    "active_contract": "📄 Укладення договору",
    "complete": "✅ Завершено",
    "cancelled": "❌ Відмінено",
    "pending": "⏳ Очікує",
    
    # Додаткові статуси
    "draft": "📝 Чернетка",
    "invalid": "❌ Невалідний",
    "unsuccessful": "❌ Невдалий",
    "deleted": "🗑 Видалено",
    "archived": "📦 Архівовано",
    
    # Статуси для аукціонів
    "auction": "🔨 Аукціон",
    "announced": "📢 Оголошено",
    "evaluation": "📊 Оцінка пропозицій",
    "decision": "📋 Прийняття рішення",
    "appeal": "⚖️ Оскарження",
    "cancelled_auction": "❌ Аукціон відмінено",
    "contract_signing": "📄 Підписання договору",
    "contract_active": "✅ Договір активний",
    "contract_terminated": "⛔ Договір розірвано",
    "contract_completed": "✅ Договір виконано",
    
    # Статуси для оренди
    "lease_active": "🟢 Оренда активна",
    "lease_ended": "⏹ Оренда завершена",
    "lease_terminated": "⛔ Оренду розірвано",
    "lease_pending": "⏳ Очікує оренду",
}


def translate_status(status_code):
    """
    Перекладає код статусу в зрозумілий текст
    """
    if not status_code:
        return "❓ Невідомий статус"
    
    # Шукаємо точний збіг
    if status_code in STATUS_TRANSLATIONS:
        return STATUS_TRANSLATIONS[status_code]
    
    # Якщо точного збігу немає, пробуємо частковий збіг
    for key, value in STATUS_TRANSLATIONS.items():
        if key in status_code or status_code in key:
            return value
    
    # Якщо нічого не знайшли, повертаємо оригінал
    return f"ℹ️ {status_code}"


def format_date(date_string):
    """
    Форматує дату з ISO формату в читабельний вигляд
    Формат: 20-07-2026 / 14:15
    """
    try:
        # Видаляємо 'Z' якщо є
        date_string = date_string.replace('Z', '+00:00')
        dt = datetime.fromisoformat(date_string)
        
        # Конвертуємо в Київський час
        kyiv_time = dt.astimezone(ZoneInfo("Europe/Kyiv"))
        
        return kyiv_time.strftime("%d-%m-%Y / %H:%M")
    except Exception:
        return date_string


def get_auction_info(url):
    """Отримує інформацію про аукціон за URL"""
    response = requests.get(url, timeout=15)
    response.raise_for_status()

    data = response.json()

    return {
        "id": data.get("_id"),
        "auction_id": data.get("auctionId"),
        "date_modified": data.get("dateModified"),
        "status": data.get("status"),
        "title": data.get("title", {}).get("uk_UA", "")
    }


def extract_auction_id(text):
    """
    Повертає auctionId незалежно від того,
    що користувач надіслав.
    """

    # API-посилання
    if "procedure.prozorro.sale/api/procedures/" in text:
        return None

    # NRC
    match = re.search(r"id=([A-Z0-9\-]+)", text)
    if match:
        return match.group(1)

    # Prozorro.sale
    match = re.search(r"auction/([A-Z0-9\-]+)", text)
    if match:
        return match.group(1)

    # Просто ID
    match = re.search(r"[A-Z]{3}\d{3}-UA-\d{8}-\d{5}", text)
    if match:
        return match.group(0)

    return None


def auction_id_to_api_url(auction_id):
    """Конвертує ID аукціону в API URL"""
    url = f"https://procedure.prozorro.sale/api/search/byAuctionId/{auction_id}"

    response = requests.get(url, timeout=15)
    response.raise_for_status()

    data = response.json()

    procedure_id = data["_id"]

    return f"https://procedure.prozorro.sale/api/procedures/{procedure_id}"


if __name__ == "__main__":
    url = input("Посилання: ")

    info = get_auction_info(url)

    print(info)
    # api.py - ДОДАТИ В КІНЕЦЬ ФАЙЛУ (перед if __name__ == "__main__":)


# Кеш для асинхронних запитів
async_cache = {}
ASYNC_CACHE_TTL = 60  # Кеш на 60 секунд


async def get_auction_info_async(session, url):
    """Асинхронне отримання інформації про аукціон з кешем"""
    # Перевіряємо кеш
    if url in async_cache:
        cached_data, timestamp = async_cache[url]
        if (datetime.now() - timestamp).seconds < ASYNC_CACHE_TTL:
            return cached_data
    
    try:
        async with session.get(url, timeout=15) as response:
            data = await response.json()
            
            # Зберігаємо в кеш
            async_cache[url] = (data, datetime.now())
            
            return {
                "id": data.get("_id"),
                "auction_id": data.get("auctionId"),
                "date_modified": data.get("dateModified"),
                "status": data.get("status"),
                "title": data.get("title", {}).get("uk_UA", "")
            }
    except Exception as e:
        logger.error(f"❌ Помилка запиту до API: {e}")
        raise