# monitor.py
import asyncio
import aiohttp
from datetime import datetime
from api import format_date, translate_status
from database import get_all_users_for_monitor, update_user_auction
from logger import logger

# Кеш для результатів
cache = {}
CACHE_TTL = 60  # Кеш на 60 секунд


async def check_auction_async(session, chat_id, auction_id, url, old_date, old_status):
    """Асинхронна перевірка одного аукціону з кешем"""
    try:
        # Перевіряємо кеш
        cache_key = url
        if cache_key in cache:
            cached_data, timestamp = cache[cache_key]
            if (datetime.now() - timestamp).seconds < CACHE_TTL:
                data = cached_data
            else:
                del cache[cache_key]
                async with session.get(url, timeout=15) as response:
                    data = await response.json()
                    cache[cache_key] = (data, datetime.now())
        else:
            async with session.get(url, timeout=15) as response:
                data = await response.json()
                cache[cache_key] = (data, datetime.now())
        
        new_date = data.get("dateModified")
        new_status = data.get("status")
        
        if new_date != old_date or new_status != old_status:
            update_user_auction(chat_id, auction_id, new_date, new_status)
            
            old_status_text = translate_status(old_status)
            new_status_text = translate_status(new_status)
            
            return {
                "chat_id": chat_id,
                "auction_id": auction_id,
                "old_date": old_date,
                "new_date": new_date,
                "old_status": old_status_text,
                "new_status": new_status_text,
                "title": data.get("title", {}).get("uk_UA", "")
            }
    except Exception as e:
        logger.error(f"❌ Помилка перевірки {auction_id}: {e}")
    
    return None


async def check_auctions(context):
    """Перевіряє всі аукціони всіх користувачів (асинхронно з кешем)"""
    start_time = datetime.now()
    logger.info("🔄 Початок перевірки аукціонів...")
    
    try:
        user_auctions = get_all_users_for_monitor()
        logger.info(f"📊 Знайдено {len(user_auctions)} аукціонів для перевірки")
    except Exception as e:
        logger.error(f"❌ Помилка отримання списку аукціонів: {e}")
        return

    if not user_auctions:
        logger.info("📭 Немає аукціонів для перевірки")
        return

    # Використовуємо асинхронні запити
    async with aiohttp.ClientSession() as session:
        # Створюємо завдання для всіх аукціонів
        tasks = []
        for chat_id, auction_id, url, old_date, old_status in user_auctions:
            task = check_auction_async(session, chat_id, auction_id, url, old_date, old_status)
            tasks.append(task)
        
        # Виконуємо всі запити паралельно
        results = await asyncio.gather(*tasks)
    
    # Групуємо результати по користувачах
    user_updates = {}
    for result in results:
        if result:
            chat_id = result["chat_id"]
            if chat_id not in user_updates:
                user_updates[chat_id] = []
            user_updates[chat_id].append(result)

    # Надсилаємо сповіщення
    for chat_id, updates in user_updates.items():
        try:
            message = "🔔 **Виявлено зміни в аукціонах!**\n\n"

            for update in updates:
                old_date_f = format_date(update['old_date'])
                new_date_f = format_date(update['new_date'])
                
                message += (
                    f"📌 {update['auction_id']}\n"
                    f"📄 {update['title'][:60]}{'...' if len(update['title']) > 60 else ''}\n"
                    f"📊 {update['old_status']} ➜ {update['new_status']}\n"
                    f"🕒 {old_date_f} ➜ {new_date_f}\n\n"
                )

            await context.bot.send_message(
                chat_id=chat_id,
                text=message
            )
            logger.info(f"✅ Сповіщення надіслано користувачу {chat_id}")

        except Exception as e:
            logger.error(f"❌ Помилка відправки повідомлення для {chat_id}: {e}")
    
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"✅ Перевірку завершено за {elapsed:.2f} секунд")