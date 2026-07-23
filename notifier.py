# notifier.py — картка лота і надсилання сповіщень за підписками
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from api import format_date, translate_status
from config import NRC_LOT_URL, PROZORRO_LOT_URL
from logger import logger
from refdata import selling_method_title


def _money(amount, currency="UAH"):
    if amount is None:
        return None
    unit = "грн" if (currency or "UAH") == "UAH" else currency
    return f"{amount:,.0f} {unit}".replace(",", " ")


def lot_links(proc):
    """Дві кнопки: першоджерело і сторінка на сайті НРЦ"""
    auction_id = proc.get("auction_id") or ""
    selling_method = proc.get("selling_method") or ""
    buttons = []

    if auction_id:
        buttons.append(InlineKeyboardButton(
            "🔗 Prozorro", url=PROZORRO_LOT_URL.format(auction_id=auction_id)
        ))
        if selling_method:
            buttons.append(InlineKeyboardButton(
                "🌐 НРЦ",
                url=NRC_LOT_URL.format(selling_method=selling_method, auction_id=auction_id)
            ))
    return buttons


def format_lot_card(proc, header=None, compact=False):
    """Текст картки лота в HTML"""
    e = html.escape
    lines = []

    if header:
        lines.append(f"<b>{e(header)}</b>")
        lines.append("")

    lines.append(selling_method_title(proc.get("selling_method")))

    title = (proc.get("title") or "").strip()
    if title:
        limit = 180 if compact else 400
        if len(title) > limit:
            title = title[:limit].rstrip() + "…"
        lines.append(f"📄 {e(title)}")

    place = ", ".join(filter(None, [
        (proc.get("region") or "").split(";")[0].strip(),
        (proc.get("locality") or "").split(";")[0].strip(),
    ]))
    if place:
        lines.append(f"📍 {e(place)}")

    if not compact and proc.get("cadastral"):
        cadastral = proc["cadastral"].split(";")[0].strip()
        lines.append(f"🗺 <code>{e(cadastral)}</code>")

    if proc.get("land_area"):
        lines.append(f"📐 {proc['land_area']:g} га")

    money = _money(proc.get("amount"), proc.get("currency"))
    if money:
        lines.append(f"💰 {e(money)}")

    if not compact and proc.get("organizer_name"):
        organizer = proc["organizer_name"]
        if len(organizer) > 70:
            organizer = organizer[:70].rstrip() + "…"
        lines.append(f"🏛 {e(organizer)}")

    if proc.get("status"):
        lines.append(f"📊 {e(translate_status(proc['status']))}")

    if proc.get("tender_end"):
        lines.append(f"⏳ Заявки до: {e(format_date(proc['tender_end']))}")

    if not compact and proc.get("auction_start"):
        lines.append(f"🔨 Аукціон: {e(format_date(proc['auction_start']))}")

    lines.append(f"🆔 <code>{e(proc.get('auction_id') or '')}</code>")

    return "\n".join(lines)


async def send_lot(bot, chat_id, proc, header=None, compact=False, extra_buttons=None):
    """Надсилає картку лота з кнопками-посиланнями"""
    text = format_lot_card(proc, header=header, compact=compact)

    rows = []
    links = lot_links(proc)
    if links:
        rows.append(links)
    if extra_buttons:
        rows.append(extra_buttons)

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.error(f"❌ Не вдалося надіслати лот {proc.get('auction_id')} до {chat_id}: {e}")
        return False
