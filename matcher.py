# matcher.py — зіставлення процедури з критеріями підписки
"""
Вісім критеріїв фільтра. Незаповнений критерій нічого не обмежує,
тож порожня підписка ловить усе підряд (і швидко впирається в денну квоту).

Ключові слова шукаються як підрядок, а не як ціле слово — це навмисно:
українська морфологія робить пошук за коренем («пасовищ») набагато
кориснішим за пошук за точною словоформою («пасовище»).
"""
from datetime import datetime, timedelta, timezone

from database import get_conn

# роздільник списків усередині полів підписки
SEP = ";"


def split_list(raw):
    """'земля; пасовище' -> ['земля', 'пасовище']"""
    if not raw:
        return []
    return [part.strip() for part in raw.split(SEP) if part.strip()]


def join_list(values):
    return SEP.join(str(v).strip() for v in values if str(v).strip())


def _haystack(proc):
    """Текст, по якому шукаємо ключові слова"""
    return " ".join(filter(None, [
        proc.get("title") or "",
        proc.get("description") or "",
        proc.get("locality") or "",
    ])).lower()


def match(proc, sub):
    """
    Чи підпадає процедура proc під підписку sub.
    Обидва аргументи — словники (рядки з БД, приведені до dict).
    """
    # 1. Стоп-слова: якщо хоч одне трапилось — лот не показуємо
    excludes = split_list(sub.get("exclude_words"))
    if excludes:
        text = _haystack(proc)
        if any(word.lower() in text for word in excludes):
            return False

    # 2. Ключові слова: достатньо одного збігу
    keywords = split_list(sub.get("keywords"))
    if keywords:
        text = _haystack(proc)
        if not any(word.lower() in text for word in keywords):
            return False

    # 3. Тип процедури (порівнюємо за групою: landSell, landRental, ...)
    groups = split_list(sub.get("method_groups"))
    if groups and (proc.get("method_group") or "") not in groups:
        return False

    # 4. Регіон — у процедури їх може бути кілька, достатньо одного збігу
    regions = split_list(sub.get("regions"))
    if regions:
        proc_regions = (proc.get("region") or "").lower()
        if not any(r.lower() in proc_regions for r in regions):
            return False

    # 5. Організатор — за назвою або за кодом ЄДРПОУ/ІПН
    organizer = (sub.get("organizer") or "").strip().lower()
    if organizer:
        name = (proc.get("organizer_name") or "").lower()
        code = (proc.get("organizer_id") or "").lower()
        if organizer not in name and organizer != code:
            return False

    # 6. Кадастровий номер — зазвичай вводять префікс ділянки
    cadastral = (sub.get("cadastral") or "").strip()
    if cadastral:
        if cadastral not in (proc.get("cadastral") or ""):
            return False

    # 7. Ціна
    amount = proc.get("amount")
    price_min, price_max = sub.get("price_min"), sub.get("price_max")
    if price_min is not None or price_max is not None:
        if amount is None:
            return False
        if price_min is not None and amount < price_min:
            return False
        if price_max is not None and amount > price_max:
            return False

    # 8. Площа
    area = proc.get("land_area")
    area_min, area_max = sub.get("area_min"), sub.get("area_max")
    if area_min is not None or area_max is not None:
        if area is None:
            return False
        if area_min is not None and area < area_min:
            return False
        if area_max is not None and area > area_max:
            return False

    return True


def is_empty(sub):
    """Чи підписка взагалі щось обмежує"""
    return not any([
        split_list(sub.get("keywords")),
        split_list(sub.get("exclude_words")),
        split_list(sub.get("method_groups")),
        split_list(sub.get("regions")),
        (sub.get("organizer") or "").strip(),
        (sub.get("cadastral") or "").strip(),
        sub.get("price_min"), sub.get("price_max"),
        sub.get("area_min"), sub.get("area_max"),
    ])


def describe(sub, short=False):
    """Людський опис критеріїв підписки"""
    from refdata import group_title, region_short

    parts = []

    keywords = split_list(sub.get("keywords"))
    if keywords:
        parts.append("🔤 Слова: " + ", ".join(keywords))

    excludes = split_list(sub.get("exclude_words"))
    if excludes:
        parts.append("🚫 Крім: " + ", ".join(excludes))

    groups = split_list(sub.get("method_groups"))
    if groups:
        titles = [group_title(g) for g in groups]
        if short and len(titles) > 2:
            parts.append(f"📂 Типи: {titles[0]} +{len(titles) - 1}")
        else:
            parts.append("📂 Типи: " + ", ".join(titles))

    regions = split_list(sub.get("regions"))
    if regions:
        names = [region_short(r) for r in regions]
        if short and len(names) > 3:
            parts.append(f"📍 Регіони: {', '.join(names[:3])} +{len(names) - 3}")
        else:
            parts.append("📍 Регіони: " + ", ".join(names))

    if (sub.get("organizer") or "").strip():
        parts.append("🏛 Організатор: " + sub["organizer"])

    if (sub.get("cadastral") or "").strip():
        parts.append("🗺 Кадастр: " + sub["cadastral"])

    pmin, pmax = sub.get("price_min"), sub.get("price_max")
    if pmin is not None or pmax is not None:
        if pmin is not None and pmax is not None:
            parts.append(f"💰 Ціна: {pmin:,.0f}–{pmax:,.0f} грн".replace(",", " "))
        elif pmin is not None:
            parts.append(f"💰 Ціна: від {pmin:,.0f} грн".replace(",", " "))
        else:
            parts.append(f"💰 Ціна: до {pmax:,.0f} грн".replace(",", " "))

    amin, amax = sub.get("area_min"), sub.get("area_max")
    if amin is not None or amax is not None:
        if amin is not None and amax is not None:
            parts.append(f"📐 Площа: {amin:g}–{amax:g} га")
        elif amin is not None:
            parts.append(f"📐 Площа: від {amin:g} га")
        else:
            parts.append(f"📐 Площа: до {amax:g} га")

    return parts or ["⚠️ Критерії не задані — підпадатимуть усі лоти"]


# ── пошук по кешу ────────────────────────────────────────────────────────

def _narrow_sql(sub):
    """
    Попереднє звуження засобами SQL — щоб не тягнути в Python увесь кеш.
    Точну перевірку все одно робить match().
    """
    where, params = [], []

    groups = split_list(sub.get("method_groups"))
    if groups:
        where.append("method_group IN (%s)" % ",".join("?" * len(groups)))
        params += groups

    if sub.get("price_min") is not None:
        where.append("(amount IS NOT NULL AND amount >= ?)")
        params.append(sub["price_min"])
    if sub.get("price_max") is not None:
        where.append("(amount IS NOT NULL AND amount <= ?)")
        params.append(sub["price_max"])

    if sub.get("area_min") is not None:
        where.append("(land_area IS NOT NULL AND land_area >= ?)")
        params.append(sub["area_min"])
    if sub.get("area_max") is not None:
        where.append("(land_area IS NOT NULL AND land_area <= ?)")
        params.append(sub["area_max"])

    return where, params


def find_matches(sub, days=7, limit=200):
    """
    Лоти з кешу за останні N днів, що підпадають під підписку.
    Використовується для ретроспективи одразу після створення фільтра.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    where, params = _narrow_sql(sub)
    where.append("date_published >= ?")
    params.append(since)

    conn = get_conn()
    conn.row_factory = __import__("sqlite3").Row
    rows = conn.execute(
        f"SELECT * FROM procedures WHERE {' AND '.join(where)} "
        f"ORDER BY date_published DESC LIMIT ?",
        params + [limit * 5]        # запас: частину відсіє точний match()
    ).fetchall()
    conn.close()

    result = []
    for row in rows:
        proc = dict(row)
        if match(proc, sub):
            result.append(proc)
            if len(result) >= limit:
                break
    return result


def count_matches(sub, days=7):
    """Скільки лотів підпало б під підписку за N днів"""
    return len(find_matches(sub, days=days, limit=10_000))
