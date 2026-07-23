# wizard.py — майстер створення фільтра і керування підписками
"""
Покроковий майстер на inline-кнопках. Стан живе в context.user_data["wizard"],
тому кожен користувач веде свій фільтр незалежно.

Кроки навмисно розташовані від найкорисніших до необов'язкових: тип процедури
й регіон звужують потік найсильніше, тому йдуть першими, а на кожному наступному
кроці доступна кнопка «Зберегти зараз» — щоб не змушувати проходити всі дев'ять.
"""
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from config import DAILY_NOTIFY_LIMIT, MAX_SUBS_PER_USER, RETRO_DAYS
from logger import logger
from matcher import describe, find_matches, join_list, split_list
from notifier import send_lot
from refdata import REGIONS, SELLING_METHOD_GROUPS, group_title, normalize_region, region_short
from subs import (
    count_subscriptions,
    create_subscription,
    delete_subscription,
    get_subscription,
    get_subscriptions,
    seed_notified,
    toggle_subscription,
    update_subscription,
)

GROUP_CODES = list(SELLING_METHOD_GROUPS.keys())

GROUPS_PER_PAGE = 8
REGIONS_PER_PAGE = 15

# порядок кроків майстра
STEPS = ("name", "groups", "regions", "keywords", "excludes", "price", "area",
         "organizer", "cadastral", "confirm")

STEP_TITLES = {
    "name": "Назва фільтра",
    "groups": "Типи процедур",
    "regions": "Регіони",
    "keywords": "Ключові слова",
    "excludes": "Слова-винятки",
    "price": "Ціна",
    "area": "Площа",
    "organizer": "Організатор",
    "cadastral": "Кадастровий номер",
    "confirm": "Підтвердження",
}


# ── стан майстра ─────────────────────────────────────────────────────────

def get_state(context):
    return context.user_data.get("wizard")


def start_wizard(context, sub_id=None):
    """Створює стан майстра — з нуля або з даних наявної підписки"""
    data = {
        "name": "", "groups": [], "regions": [], "keywords": [], "excludes": [],
        "price_min": None, "price_max": None, "area_min": None, "area_max": None,
        "organizer": "", "cadastral": "",
    }

    if sub_id:
        sub = get_subscription(sub_id)
        if sub:
            data.update({
                "name": sub.get("name") or "",
                "groups": split_list(sub.get("method_groups")),
                "regions": split_list(sub.get("regions")),
                "keywords": split_list(sub.get("keywords")),
                "excludes": split_list(sub.get("exclude_words")),
                "price_min": sub.get("price_min"), "price_max": sub.get("price_max"),
                "area_min": sub.get("area_min"), "area_max": sub.get("area_max"),
                "organizer": sub.get("organizer") or "",
                "cadastral": sub.get("cadastral") or "",
            })

    context.user_data["wizard"] = {"step": 0, "page": 0, "data": data, "sub_id": sub_id}
    return context.user_data["wizard"]


def clear_wizard(context):
    context.user_data.pop("wizard", None)


def state_to_sub(data):
    """Стан майстра -> поля підписки"""
    return {
        "keywords": join_list(data["keywords"]),
        "exclude_words": join_list(data["excludes"]),
        "method_groups": join_list(data["groups"]),
        "regions": join_list(data["regions"]),
        "organizer": data["organizer"] or "",
        "cadastral": data["cadastral"] or "",
        "price_min": data["price_min"], "price_max": data["price_max"],
        "area_min": data["area_min"], "area_max": data["area_max"],
    }


# ── малювання кроків ─────────────────────────────────────────────────────

def _nav_row(state, allow_skip=True):
    """Нижній ряд кнопок: назад / пропустити / зберегти / скасувати"""
    row = []
    if state["step"] > 0:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data="w_back"))
    if allow_skip:
        row.append(InlineKeyboardButton("Далі ➡️", callback_data="w_next"))
    return row


def _tail_rows(state):
    rows = []
    if state["step"] > 1:
        rows.append([InlineKeyboardButton("✅ Зберегти зараз", callback_data="w_save")])
    rows.append([InlineKeyboardButton("❌ Скасувати", callback_data="w_cancel")])
    return rows


def render_step(state):
    """Повертає (текст, клавіатура) для поточного кроку"""
    step = STEPS[state["step"]]
    data = state["data"]
    page = state.get("page", 0)

    number = state["step"] + 1
    head = f"<b>Крок {number}/{len(STEPS)} · {STEP_TITLES[step]}</b>\n\n"

    if step == "name":
        text = head + (
            "Як назвати цей фільтр?\n\n"
            "Наприклад: <i>Пасовища Волинь</i> або <i>Оренда до 100 тис</i>.\n\n"
            "Надішліть назву повідомленням."
        )
        if data["name"]:
            text += f"\n\nЗараз: <b>{html.escape(data['name'])}</b>"
        rows = [[InlineKeyboardButton("Далі ➡️", callback_data="w_next")]] + _tail_rows(state)
        return text, InlineKeyboardMarkup(rows)

    if step == "groups":
        chosen = data["groups"]
        text = head + (
            "Які типи процедур вас цікавлять?\n"
            "Натискайте, щоб позначити. Нічого не обрано — підійдуть усі типи."
        )
        if chosen:
            text += "\n\nОбрано: " + ", ".join(group_title(c) for c in chosen)

        start = page * GROUPS_PER_PAGE
        rows = []
        for idx in range(start, min(start + GROUPS_PER_PAGE, len(GROUP_CODES))):
            code = GROUP_CODES[idx]
            mark = "✅ " if code in chosen else ""
            rows.append([InlineKeyboardButton(
                f"{mark}{group_title(code)}", callback_data=f"w_tg_{idx}"
            )])

        rows.append(_pager(page, len(GROUP_CODES), GROUPS_PER_PAGE))
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup([r for r in rows if r])

    if step == "regions":
        chosen = data["regions"]
        text = head + (
            "Які регіони відстежувати?\n"
            "Нічого не обрано — вся Україна."
        )
        if chosen:
            text += "\n\nОбрано: " + ", ".join(region_short(r) for r in chosen)

        start = page * REGIONS_PER_PAGE
        rows, row = [], []
        for idx in range(start, min(start + REGIONS_PER_PAGE, len(REGIONS))):
            region = REGIONS[idx]
            mark = "✅" if region in chosen else ""
            row.append(InlineKeyboardButton(
                f"{mark}{region_short(region)}", callback_data=f"w_tr_{idx}"
            ))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        rows.append(_pager(page, len(REGIONS), REGIONS_PER_PAGE))
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup([r for r in rows if r])

    if step == "keywords":
        text = head + (
            "Ключові слова — шукатиму їх у назві та описі лота.\n\n"
            "Кілька слів надсилайте через кому: <i>пасовище, рілля, сінокіс</i>\n"
            "Шукаю за частиною слова, тож «пасовищ» знайде і «пасовище», і «пасовища».\n\n"
            "Достатньо збігу з будь-яким словом зі списку."
        )
        if data["keywords"]:
            text += "\n\nЗараз: <b>" + html.escape(", ".join(data["keywords"])) + "</b>"
        rows = [[InlineKeyboardButton("🗑 Очистити", callback_data="w_clear")]] if data["keywords"] else []
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup(rows)

    if step == "excludes":
        text = head + (
            "Слова-винятки — лоти з ними я не показуватиму.\n\n"
            "Наприклад, ви шукаєте оренду, але суборенда не потрібна: <i>суборенда</i>\n\n"
            "Кілька слів — через кому."
        )
        if data["excludes"]:
            text += "\n\nЗараз: <b>" + html.escape(", ".join(data["excludes"])) + "</b>"
        rows = [[InlineKeyboardButton("🗑 Очистити", callback_data="w_clear")]] if data["excludes"] else []
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup(rows)

    if step == "price":
        text = head + (
            "Стартова ціна лота, грн.\n\n"
            "Надішліть діапазон: <i>100000-500000</i>\n"
            "Або лише межу: <i>від 50000</i> · <i>до 1000000</i>"
        )
        if data["price_min"] is not None or data["price_max"] is not None:
            text += f"\n\nЗараз: {_range_text(data['price_min'], data['price_max'], 'грн')}"
        rows = [[InlineKeyboardButton("🗑 Очистити", callback_data="w_clear")]] \
            if (data["price_min"] is not None or data["price_max"] is not None) else []
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup(rows)

    if step == "area":
        text = head + (
            "Площа ділянки, гектари.\n\n"
            "Наприклад: <i>2-10</i> · <i>від 5</i> · <i>до 50</i>\n\n"
            "Стосується лотів із земельними ділянками."
        )
        if data["area_min"] is not None or data["area_max"] is not None:
            text += f"\n\nЗараз: {_range_text(data['area_min'], data['area_max'], 'га')}"
        rows = [[InlineKeyboardButton("🗑 Очистити", callback_data="w_clear")]] \
            if (data["area_min"] is not None or data["area_max"] is not None) else []
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup(rows)

    if step == "organizer":
        text = head + (
            "Організатор торгів.\n\n"
            "Надішліть частину назви або код ЄДРПОУ/ІПН.\n"
            "Наприклад: <i>Головне управління Держгеокадастру</i> або <i>37137609</i>"
        )
        if data["organizer"]:
            text += f"\n\nЗараз: <b>{html.escape(data['organizer'])}</b>"
        rows = [[InlineKeyboardButton("🗑 Очистити", callback_data="w_clear")]] if data["organizer"] else []
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup(rows)

    if step == "cadastral":
        text = head + (
            "Кадастровий номер.\n\n"
            "Можна вказати початок номера, щоб зловити цілий масив:\n"
            "<i>5324888200</i> — усі ділянки цієї сільради."
        )
        if data["cadastral"]:
            text += f"\n\nЗараз: <b>{html.escape(data['cadastral'])}</b>"
        rows = [[InlineKeyboardButton("🗑 Очистити", callback_data="w_clear")]] if data["cadastral"] else []
        rows.append(_nav_row(state))
        rows += _tail_rows(state)
        return text, InlineKeyboardMarkup(rows)

    # confirm
    sub_preview = state_to_sub(data)
    lines = [f"<b>Фільтр «{html.escape(data['name'] or 'без назви')}»</b>", ""]
    lines += [html.escape(part) for part in describe(sub_preview)]
    lines += ["", f"Ліміт: {DAILY_NOTIFY_LIMIT} лотів на добу."]

    rows = [
        [InlineKeyboardButton("💾 Зберегти фільтр", callback_data="w_save")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="w_back")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="w_cancel")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _pager(page, total, per_page):
    pages = (total + per_page - 1) // per_page
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"w_pg_{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"w_pg_{page + 1}"))
    return row


def _range_text(low, high, unit):
    if low is not None and high is not None:
        return f"{low:g}–{high:g} {unit}"
    if low is not None:
        return f"від {low:g} {unit}"
    return f"до {high:g} {unit}"


# ── розбір текстового вводу ──────────────────────────────────────────────

def parse_range(text):
    """
    '100000-500000' | 'від 50000' | 'до 1000000' | '5' -> (min, max)
    Повертає None, якщо розібрати не вдалося.
    """
    import re

    t = text.strip().lower().replace(",", ".").replace(" ", "")
    t = t.replace("грн", "").replace("га", "")

    def num(s):
        try:
            return float(s)
        except ValueError:
            return None

    if t.startswith("від"):
        value = num(t[3:])
        return (value, None) if value is not None else None
    if t.startswith("до"):
        value = num(t[2:])
        return (None, value) if value is not None else None

    match = re.match(r"^(\d+(?:\.\d+)?)[-–—](\d+(?:\.\d+)?)$", t)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        return (min(low, high), max(low, high))

    value = num(t)
    if value is not None:
        return (value, None)

    return None


def apply_text(state, text):
    """
    Застосовує текстове повідомлення до поточного кроку.
    Повертає (успіх, повідомлення про помилку).
    """
    step = STEPS[state["step"]]
    data = state["data"]
    text = text.strip()

    if step == "name":
        data["name"] = text[:60]
        return True, None

    if step == "keywords":
        data["keywords"] = [w.strip() for w in text.split(",") if w.strip()][:15]
        return True, None

    if step == "excludes":
        data["excludes"] = [w.strip() for w in text.split(",") if w.strip()][:15]
        return True, None

    if step == "price":
        parsed = parse_range(text)
        if not parsed:
            return False, "Не зрозумів діапазон. Приклади: 100000-500000, від 50000, до 1000000"
        data["price_min"], data["price_max"] = parsed
        return True, None

    if step == "area":
        parsed = parse_range(text)
        if not parsed:
            return False, "Не зрозумів площу. Приклади: 2-10, від 5, до 50"
        data["area_min"], data["area_max"] = parsed
        return True, None

    if step == "organizer":
        data["organizer"] = text[:120]
        return True, None

    if step == "cadastral":
        data["cadastral"] = text[:40]
        return True, None

    if step == "regions":
        # дозволяємо ввести регіон текстом — швидше, ніж шукати кнопку
        region = normalize_region(text)
        if not region:
            return False, "Не впізнав регіон. Оберіть кнопкою або напишіть, наприклад: Волинська"
        if region not in data["regions"]:
            data["regions"].append(region)
        return True, None

    return False, "На цьому кроці користуйтесь кнопками."


def clear_step(state):
    """Очищає значення поточного кроку"""
    step = STEPS[state["step"]]
    data = state["data"]

    if step == "keywords":
        data["keywords"] = []
    elif step == "excludes":
        data["excludes"] = []
    elif step == "price":
        data["price_min"] = data["price_max"] = None
    elif step == "area":
        data["area_min"] = data["area_max"] = None
    elif step == "organizer":
        data["organizer"] = ""
    elif step == "cadastral":
        data["cadastral"] = ""
    elif step == "groups":
        data["groups"] = []
    elif step == "regions":
        data["regions"] = []


# ── збереження та ретроспектива ──────────────────────────────────────────

async def save_subscription(update, context, state):
    """Зберігає підписку і показує, що вона зловила б за останні дні"""
    chat_id = update.effective_chat.id
    data = state["data"]
    fields = state_to_sub(data)
    name = data["name"] or "Фільтр"

    sub_id = state.get("sub_id")
    if sub_id:
        update_subscription(sub_id, name=name, **fields)
    else:
        sub_id = create_subscription(chat_id, name, **fields)
        if sub_id is None:
            clear_wizard(context)
            return False, (
                f"❌ Досягнуто ліміту: {MAX_SUBS_PER_USER} фільтрів на користувача.\n"
                "Видаліть непотрібний фільтр і спробуйте знову."
            )

    clear_wizard(context)

    sub = get_subscription(sub_id)
    matches = find_matches(sub, days=RETRO_DAYS, limit=200)

    # усе, що показали в ретроспективі, позначаємо як надіслане —
    # інакше ці ж лоти прилетять ще раз звичайним сповіщенням
    seed_notified(sub_id, [m["procedure_id"] for m in matches], event="published")

    return True, matches


async def send_retrospective(bot, chat_id, sub, matches):
    """Показує кілька збігів за останні дні одразу після створення фільтра"""
    name = sub.get("name") or "фільтр"

    if not matches:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Фільтр «{name}» збережено.\n\n"
                f"За останні {RETRO_DAYS} днів під нього не підпало жодного лота. "
                "Щойно з'явиться відповідний — одразу надішлю.\n\n"
                "Якщо очікували більше — критерії, схоже, надто вузькі."
            ),
        )
        return

    preview = matches[:5]
    tail = len(matches) - len(preview)

    text = (
        f"✅ Фільтр «{name}» збережено.\n\n"
        f"За останні {RETRO_DAYS} днів під нього підпало <b>{len(matches)}</b> лотів."
    )
    if tail > 0:
        text += f" Показую {len(preview)} найсвіжіших."
    if len(matches) >= DAILY_NOTIFY_LIMIT:
        text += (
            f"\n\n⚠️ Це багато: денний ліміт — {DAILY_NOTIFY_LIMIT} лотів. "
            "Варто звузити критерії."
        )
    text += "\n\nНадалі надсилатиму лише нові лоти."

    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    for proc in preview:
        await send_lot(bot, chat_id, proc, compact=True)


# ── меню підписок ────────────────────────────────────────────────────────

def subs_menu(chat_id):
    """Список фільтрів користувача"""
    subscriptions = get_subscriptions(chat_id)

    if not subscriptions:
        text = (
            "📡 <b>Мої фільтри</b>\n\n"
            "У вас поки немає жодного фільтра.\n\n"
            "Фільтр — це збережені критерії пошуку. Щойно на Prozorro.Продажі "
            "з'явиться лот, який їм відповідає, я одразу надішлю картку лота."
        )
        rows = [[InlineKeyboardButton("➕ Створити фільтр", callback_data="s_new")]]
        return text, InlineKeyboardMarkup(rows)

    text = f"📡 <b>Мої фільтри</b> ({len(subscriptions)}/{MAX_SUBS_PER_USER})\n"
    rows = []
    for sub in subscriptions:
        mark = "🟢" if sub["enabled"] else "⚪️"
        rows.append([InlineKeyboardButton(
            f"{mark} {sub['name'] or 'Без назви'}", callback_data=f"s_view_{sub['id']}"
        )])

    if len(subscriptions) < MAX_SUBS_PER_USER:
        rows.append([InlineKeyboardButton("➕ Створити фільтр", callback_data="s_new")])

    return text, InlineKeyboardMarkup(rows)


def sub_card(sub_id):
    """Картка одного фільтра"""
    sub = get_subscription(sub_id)
    if not sub:
        return "❌ Фільтр не знайдено.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ До списку", callback_data="s_list")]]
        )

    from subs import quota_state
    sent, limit, _ = quota_state(sub_id)

    state_text = "🟢 Увімкнено" if sub["enabled"] else "⚪️ Вимкнено"
    lines = [
        f"<b>{html.escape(sub['name'] or 'Без назви')}</b>",
        state_text,
        "",
    ]
    lines += [html.escape(part) for part in describe(sub)]
    lines += ["", f"📨 Сьогодні надіслано: {sent}/{limit}"]

    rows = [
        [InlineKeyboardButton(
            "⚪️ Вимкнути" if sub["enabled"] else "🟢 Увімкнути",
            callback_data=f"s_toggle_{sub_id}"
        )],
        [InlineKeyboardButton("✏️ Редагувати", callback_data=f"s_edit_{sub_id}")],
        [InlineKeyboardButton("🔍 Що знайшлося б за 7 днів", callback_data=f"s_test_{sub_id}")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"s_del_{sub_id}")],
        [InlineKeyboardButton("⬅️ До списку", callback_data="s_list")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def delete_confirm(sub_id):
    sub = get_subscription(sub_id)
    name = sub["name"] if sub else "фільтр"
    text = f"🗑 Видалити фільтр «{html.escape(name or 'Без назви')}»?\n\nЦю дію не відкотити."
    rows = [
        [InlineKeyboardButton("✅ Так, видалити", callback_data=f"s_delok_{sub_id}")],
        [InlineKeyboardButton("⬅️ Ні, назад", callback_data=f"s_view_{sub_id}")],
    ]
    return text, InlineKeyboardMarkup(rows)
