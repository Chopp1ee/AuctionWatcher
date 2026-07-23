# watcher.py — джоб пошуку нових лотів за підписками
"""
Раз на FEED_INTERVAL секунд:
  1. дочитуємо хвіст фіду Prozorro і оновлюємо локальний кеш процедур;
  2. кожну свіжу подію зіставляємо з активними підписками;
  3. надсилаємо картки лотів, дотримуючись денної квоти.

Дві події вважаються вартими сповіщення:
  • published — лот уперше з'явився у фіді й опублікований уже після створення підписки
    (умова з датою рятує від спаму при холодному старті кешу);
  • tendering — лот перейшов у «Прийняття заяв на участь», причому ми реально бачили
    попередній статус, а не вперше зустріли лот уже в цьому стані.
"""
import asyncio

from telegram import InlineKeyboardButton

from feed import cleanup_old_procedures, poll_feed
from logger import logger
from matcher import match
from notifier import send_lot
from subs import (
    get_active_subscriptions,
    mark_notified,
    quota_consume,
    quota_mark_warned,
    was_notified,
)

SEND_DELAY = 0.06          # пауза між повідомленнями, щоб не впертись у ліміт Telegram

EVENT_HEADERS = {
    "published": "🆕 Новий лот за фільтром «{name}»",
    "tendering": "📝 Відкрито подання заявок — фільтр «{name}»",
}


def classify(event):
    """Яку подію несе зміна процедури: 'published', 'tendering' або нічого"""
    proc = event["proc"]

    if event["is_new"]:
        return "published"

    prev_status = event.get("prev_status")
    if (proc.get("status") == "active_tendering"
            and prev_status
            and prev_status != "active_tendering"):
        return "tendering"

    return None


async def process_events(bot, events):
    """Розсилає сповіщення за списком подій фіду. Повертає кількість надісланого."""
    if not events:
        return 0

    subscriptions = get_active_subscriptions()
    if not subscriptions:
        return 0

    sent_total = 0

    for event in events:
        kind = classify(event)
        if not kind:
            continue

        proc = event["proc"]
        procedure_id = proc.get("procedure_id")
        if not procedure_id:
            continue

        for sub in subscriptions:
            # лоти, опубліковані до створення підписки, не турбують користувача:
            # для них є ретроспектива в момент налаштування фільтра
            if kind == "published":
                published = proc.get("date_published") or ""
                created = sub.get("created_at") or ""
                if created and published and published < created:
                    continue

            if not match(proc, sub):
                continue

            if was_notified(sub["id"], procedure_id, kind):
                continue

            state = quota_consume(sub["id"])

            if state == "over":
                continue

            if state == "limit_reached":
                await _warn_quota(bot, sub)
                continue

            header = EVENT_HEADERS[kind].format(name=sub.get("name") or "без назви")
            extra = [InlineKeyboardButton(
                "🔔 Стежити за лотом", callback_data=f"track_{proc.get('auction_id')}"
            )]

            ok = await send_lot(bot, sub["chat_id"], proc, header=header, extra_buttons=extra)
            if ok:
                mark_notified(sub["id"], procedure_id, kind)
                sent_total += 1
                await asyncio.sleep(SEND_DELAY)

    return sent_total


async def _warn_quota(bot, sub):
    """Одноразове попередження про вичерпану денну квоту"""
    from config import DAILY_NOTIFY_LIMIT

    try:
        await bot.send_message(
            chat_id=sub["chat_id"],
            text=(
                f"⚠️ Фільтр «{sub.get('name') or 'без назви'}» вичерпав денний ліміт "
                f"({DAILY_NOTIFY_LIMIT} лотів).\n\n"
                "Решту лотів за сьогодні я не надсилатиму. "
                "Схоже, критерії надто широкі — варто їх звузити."
            ),
        )
        quota_mark_warned(sub["id"])
    except Exception as e:
        logger.error(f"❌ Не вдалося попередити про ліміт {sub['chat_id']}: {e}")


async def watch_new_lots(context):
    """Джоб: опитати фід і розіслати збіги"""
    try:
        events = await poll_feed()
    except Exception as e:
        logger.error(f"❌ Помилка опитування фіду: {e}")
        return

    if not events:
        return

    logger.info(f"📥 З фіду отримано {len(events)} змін")

    try:
        sent = await process_events(context.bot, events)
        if sent:
            logger.info(f"📨 Надіслано {sent} сповіщень за підписками")
    except Exception as e:
        logger.error(f"❌ Помилка розсилки за підписками: {e}")


async def daily_cache_cleanup(context):
    """Джоб: чистка застарілого кешу процедур"""
    try:
        cleanup_old_procedures()
    except Exception as e:
        logger.error(f"❌ Помилка чистки кешу процедур: {e}")


async def bootstrap_job(context):
    """
    Джоб разового наповнення кешу історією.
    Виконується один раз після старту: далі всі дні позначені finished.
    """
    from config import RETRO_DAYS
    from feed import bootstrap_history

    try:
        await bootstrap_history(RETRO_DAYS)
    except Exception as e:
        logger.error(f"❌ Помилка наповнення історії: {e}")
