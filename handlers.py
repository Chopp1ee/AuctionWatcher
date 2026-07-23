# handlers.py — обробники підписок, майстра фільтрів і лічильника ставок
import asyncio
import html
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from api import format_date, translate_status
from bidtracker import accuracy_stats, event_counts, get_estimates
from config import ADMIN_IDS, RETRO_DAYS
from database import get_conn
from feed import cache_stats
from logger import logger
from matcher import find_matches
from notifier import send_lot
from subs import get_subscription
from wizard import (
    STEPS,
    clear_step,
    clear_wizard,
    delete_confirm,
    get_state,
    render_step,
    save_subscription,
    send_retrospective,
    start_wizard,
    sub_card,
    subs_menu,
    apply_text,
    GROUP_CODES,
)
from refdata import REGIONS
import wizard as wz


# ── меню фільтрів ────────────────────────────────────────────────────────

async def filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /filters і кнопка «📡 Мої фільтри»"""
    clear_wizard(context)
    text, keyboard = subs_menu(update.effective_chat.id)
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def _show(query, text, keyboard):
    """Перемальовує повідомлення, мовчки ковтаючи «нічого не змінилось»"""
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.error(f"❌ Помилка перемальовування: {e}")


# ── callback: керування підписками ───────────────────────────────────────

async def handle_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    chat_id = update.effective_chat.id

    if data == "s_list":
        clear_wizard(context)
        text, keyboard = subs_menu(chat_id)
        await _show(query, text, keyboard)
        return

    if data == "s_new":
        state = start_wizard(context)
        text, keyboard = render_step(state)
        await _show(query, text, keyboard)
        return

    if data.startswith("s_view_"):
        sub_id = int(data.rsplit("_", 1)[1])
        text, keyboard = sub_card(sub_id)
        await _show(query, text, keyboard)
        return

    if data.startswith("s_toggle_"):
        sub_id = int(data.rsplit("_", 1)[1])
        from subs import toggle_subscription
        toggle_subscription(sub_id)
        text, keyboard = sub_card(sub_id)
        await _show(query, text, keyboard)
        return

    if data.startswith("s_edit_"):
        sub_id = int(data.rsplit("_", 1)[1])
        state = start_wizard(context, sub_id=sub_id)
        text, keyboard = render_step(state)
        await _show(query, text, keyboard)
        return

    if data.startswith("s_del_"):
        sub_id = int(data.rsplit("_", 1)[1])
        text, keyboard = delete_confirm(sub_id)
        await _show(query, text, keyboard)
        return

    if data.startswith("s_delok_"):
        sub_id = int(data.rsplit("_", 1)[1])
        from subs import delete_subscription
        delete_subscription(sub_id)
        text, keyboard = subs_menu(chat_id)
        await _show(query, "🗑 Фільтр видалено.\n\n" + text, keyboard)
        return

    if data.startswith("s_test_"):
        sub_id = int(data.rsplit("_", 1)[1])
        sub = get_subscription(sub_id)
        if not sub:
            return

        await query.answer("Шукаю…")
        matches = find_matches(sub, days=RETRO_DAYS, limit=5)
        total = len(find_matches(sub, days=RETRO_DAYS, limit=10_000))

        if not total:
            await query.message.reply_text(
                f"🔍 За останні {RETRO_DAYS} днів під фільтр «{sub['name']}» "
                "не підпало жодного лота."
            )
            return

        await query.message.reply_text(
            f"🔍 За останні {RETRO_DAYS} днів під фільтр «{sub['name']}» "
            f"підпало <b>{total}</b> лотів. Показую {len(matches)}:",
            parse_mode=ParseMode.HTML
        )
        for proc in matches:
            await send_lot(context.bot, chat_id, proc, compact=True)
        return


# ── callback: майстер ────────────────────────────────────────────────────

async def handle_wizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    state = get_state(context)

    if not state:
        text, keyboard = subs_menu(update.effective_chat.id)
        await _show(query, "⌛️ Майстер закрито.\n\n" + text, keyboard)
        return

    if data == "w_cancel":
        clear_wizard(context)
        text, keyboard = subs_menu(update.effective_chat.id)
        await _show(query, "❌ Створення фільтра скасовано.\n\n" + text, keyboard)
        return

    if data == "w_next":
        state["step"] = min(state["step"] + 1, len(STEPS) - 1)
        state["page"] = 0

    elif data == "w_back":
        state["step"] = max(state["step"] - 1, 0)
        state["page"] = 0

    elif data == "w_clear":
        clear_step(state)

    elif data.startswith("w_pg_"):
        state["page"] = int(data.rsplit("_", 1)[1])

    elif data.startswith("w_tg_"):
        idx = int(data.rsplit("_", 1)[1])
        code = GROUP_CODES[idx]
        chosen = state["data"]["groups"]
        chosen.remove(code) if code in chosen else chosen.append(code)

    elif data.startswith("w_tr_"):
        idx = int(data.rsplit("_", 1)[1])
        region = REGIONS[idx]
        chosen = state["data"]["regions"]
        chosen.remove(region) if region in chosen else chosen.append(region)

    elif data == "w_save":
        await _finish_wizard(update, context, state)
        return

    text, keyboard = render_step(state)
    await _show(query, text, keyboard)


async def _finish_wizard(update, context, state):
    query = update.callback_query
    chat_id = update.effective_chat.id

    if not state["data"]["name"]:
        state["data"]["name"] = "Фільтр"

    await query.answer("Зберігаю…")

    ok, result = await save_subscription(update, context, state)
    if not ok:
        await _show(query, result, InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ До списку", callback_data="s_list")]]
        ))
        return

    # прибираємо клавіатуру майстра, далі спілкуємось новими повідомленнями
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    subscriptions = wz.get_subscriptions(chat_id)
    sub = subscriptions[-1] if subscriptions else None
    if sub:
        await send_retrospective(context.bot, chat_id, sub, result)

    text, keyboard = subs_menu(chat_id)
    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def handle_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Текстове повідомлення під час роботи майстра"""
    state = get_state(context)
    if not state:
        return False

    ok, error = apply_text(state, text)
    if not ok:
        await update.message.reply_text(f"⚠️ {error}")
        return True

    # після вдалого вводу автоматично переходимо далі — так менше натискань
    if STEPS[state["step"]] not in ("regions",):
        state["step"] = min(state["step"] + 1, len(STEPS) - 1)
        state["page"] = 0

    body, keyboard = render_step(state)
    await update.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    return True


# ── кнопка «стежити за лотом» ────────────────────────────────────────────

async def handle_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Додає лот зі сповіщення до списку ручного відстеження"""
    query = update.callback_query
    chat_id = update.effective_chat.id
    auction_id = data.replace("track_", "", 1)

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM procedures WHERE auction_id = ?", (auction_id,)
    ).fetchone()
    conn.close()

    if row is None:
        await query.answer("Лот не знайдено в кеші", show_alert=True)
        return

    proc = dict(row)
    url = f"https://procedure.prozorro.sale/api/procedures/{proc['procedure_id']}"

    from database import add_user_auction, get_user_auctions

    for existing_id, *_ in get_user_auctions(chat_id):
        if existing_id == auction_id:
            await query.answer("Цей лот уже у вашому списку", show_alert=True)
            return

    add_user_auction(chat_id, {
        "auction_id": auction_id,
        "title": proc.get("title") or "",
        "status": proc.get("status"),
        "date_modified": proc.get("date_modified"),
    }, url)

    await query.answer("✅ Додано до відстеження")


# ── /bids: оцінка кількості заявок ───────────────────────────────────────

def build_bids_view(revealed=False):
    """Екран лічильника заявок: (текст, клавіатура)"""
    items = get_estimates(only_active=not revealed)
    if revealed:
        items = [i for i in items if i["actual"] is not None]

    title = "🔓 Звірені лоти" if revealed else "🎯 Заявки на лотах"
    lines = [f"<b>{title}</b>", ""]
    rows = []

    if not items:
        if revealed:
            lines.append(
                "Жоден лот ще не дійшов до розкриття ставок.\n\n"
                "Щойно лот перейде в стадію з відкритими пропозиціями, "
                "я порівняю оцінку з фактом."
            )
        else:
            lines.append(
                "Зараз немає лотів під наглядом.\n\n"
                "Додайте лот кнопкою «➕ Додати аукціон» — поки він у статусі "
                "«Прийняття заяв на участь», я рахуватиму ймовірні заявки "
                "за змінами поля dateModified."
            )
    else:
        for item in items[:10]:
            auction_id = item["auction_id"] or ""
            total, counted, since = event_counts(auction_id)

            lines.append(f"🆔 <code>{html.escape(auction_id)}</code>")
            if item["title"]:
                lines.append(f"📄 {html.escape(item['title'][:70])}")

            if revealed and item["actual"] is not None:
                diff = abs(item["estimate"] - item["actual"])
                verdict = "точно" if diff == 0 else f"±{diff}"
                lines.append(f"📊 Оцінка ~{item['estimate']} · факт {item['actual']} · {verdict}")
            else:
                estimate_line = f"📊 Ймовірних заявок: ~{item['estimate']}"
                if item["min_bids"]:
                    estimate_line += f" (мінімум {item['min_bids']})"
                lines.append(estimate_line)
                lines.append(f"🔄 Змін зафіксовано: {total}, зараховано {counted}")

            if item["status"]:
                lines.append(f"📌 {translate_status(item['status'])}")
            if item["tender_end"] and not revealed:
                lines.append(f"⏳ Заявки до: {format_date(item['tender_end'])}")
            lines.append("")

            rows.append([InlineKeyboardButton(
                f"📜 Деталі {auction_id[:22]}", callback_data=f"b_lot_{auction_id}"
            )])

        if len(items) > 10:
            lines.append(f"…та ще {len(items) - 10} лотів.")

    stats = accuracy_stats()
    if stats:
        lines.append(
            f"📈 Точність методу: {stats['exact']}/{stats['checked']} влучань, "
            f"середня похибка ±{stats['avg_error']:.1f}"
        )
    else:
        lines.append("📈 Точність поки не рахувалась — жоден лот ще не дійшов до розкриття.")

    lines.append("")
    lines.append(
        "<i>Оцінка спирається на зміни dateModified у період подання заявок. "
        "Це орієнтир: одна заявка може дати кілька змін, а дві близькі за часом — "
        "злитися в одну.</i>"
    )

    nav = [InlineKeyboardButton("🔄 Оновити",
                                callback_data="b_revealed" if revealed else "b_refresh")]
    if revealed:
        nav.append(InlineKeyboardButton("🎯 Активні", callback_data="b_refresh"))
    else:
        nav.append(InlineKeyboardButton("🔓 Звірені", callback_data="b_revealed"))
    rows.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_lot_timeline(auction_id):
    """Таймлайн зафіксованих змін конкретного лота"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT date_modified, status, counted FROM bid_events "
        "WHERE auction_id = ? ORDER BY date_modified DESC LIMIT 25",
        (auction_id,)
    ).fetchall()
    meta = conn.execute(
        "SELECT title, estimate, actual, tender_end, min_bids FROM bid_estimates "
        "WHERE auction_id = ?", (auction_id,)
    ).fetchone()
    conn.close()

    lines = [f"<b>📜 Зміни лота</b>", f"🆔 <code>{html.escape(auction_id)}</code>"]

    if meta:
        if meta[0]:
            lines.append(f"📄 {html.escape((meta[0] or '')[:70])}")
        lines.append("")
        if meta[2] is not None:
            lines.append(f"📊 Оцінка ~{meta[1]} · фактично {meta[2]}")
        else:
            line = f"📊 Ймовірних заявок: ~{meta[1]}"
            if meta[4]:
                line += f" (мінімум {meta[4]})"
            lines.append(line)
        if meta[3]:
            lines.append(f"⏳ Заявки до: {format_date(meta[3])}")

    lines.append("")
    if not rows:
        lines.append("Змін ще не зафіксовано.")
    else:
        total, counted, since = event_counts(auction_id)
        lines.append(f"Усього змін: {total}, зараховано як заявки: {counted}")
        if since:
            lines.append(f"Рахунок ведеться з {format_date(since)}")
        lines.append("")
        for date_modified, status, is_counted in rows:
            mark = "✅" if is_counted else "▫️"
            lines.append(f"{mark} {format_date(date_modified)} · {translate_status(status)}")
        if total > len(rows):
            lines.append(f"\n…показано {len(rows)} з {total}.")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ До списку", callback_data="b_refresh")]
    ])
    return "\n".join(lines), keyboard


async def bids_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оцінка поданих заявок по лотах під наглядом (тільки для адмінів)"""
    if update.effective_chat.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Команда доступна лише адміністраторам.")
        return

    text, keyboard = build_bids_view()
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        disable_web_page_preview=True
    )


async def handle_bids_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Кнопки лічильника заявок"""
    query = update.callback_query

    if update.effective_chat.id not in ADMIN_IDS:
        await query.answer("Доступно лише адміністраторам", show_alert=True)
        return

    if data == "b_refresh":
        text, keyboard = build_bids_view(revealed=False)
    elif data == "b_revealed":
        text, keyboard = build_bids_view(revealed=True)
    elif data.startswith("b_lot_"):
        text, keyboard = build_lot_timeline(data.replace("b_lot_", "", 1))
    else:
        return

    await _show(query, text, keyboard)


async def feedstat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Стан кешу процедур (тільки для адмінів)"""
    if update.effective_chat.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ Команда доступна лише адміністраторам.")
        return

    stats = cache_stats()
    conn = get_conn()
    subs_count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    active_subs = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE enabled = 1").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM bid_events").fetchone()[0]
    cursor_row = conn.execute(
        "SELECT feed_date, position, last_modified FROM feed_cursor "
        "ORDER BY feed_date DESC LIMIT 1"
    ).fetchone()
    conn.close()

    lines = [
        "<b>📡 Стан моніторингу</b>",
        "",
        f"🗂 Лотів у кеші: {stats['total']}",
        f"🆕 Опубліковано за добу: {stats['published_24h']}",
        f"🕒 Остання зміна у фіді: {format_date(stats['last_modified']) if stats['last_modified'] else '—'}",
        "",
        f"📡 Фільтрів: {subs_count} (увімкнено {active_subs})",
        f"⚡ Зафіксовано подій ставок: {events}",
    ]
    if cursor_row:
        lines.append(f"📍 Курсор фіду: {cursor_row[0]}, позиція {cursor_row[1]}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
