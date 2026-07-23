# feed.py — інкрементальний обхід фіду Prozorro.Sale і локальний кеш процедур
"""
Фід нових/змінених процедур:
    GET https://procedure.prozorro.sale/api/search/byDateModified/{YYYY-MM-DD}?limit=N&page=P

Особливості, з яких випливає вся логіка нижче (перевірено запитами):
  • записи відсортовані за dateModified ЗА ЗРОСТАННЯМ — найсвіжіші в кінці;
  • сортування не перемикається (descending/order/sort ігноруються);
  • пагінація лінійна: page × limit = позиція; offset/skip не працюють;
  • порожня доба або надто велика сторінка → 204, ще далі → 422;
  • за добу ~1750 процедур (≈18 сторінок по 100), об'єкти повні й важкі (~36 КБ).

Через вагу фіду ми не тягнемо його на кожен запит користувача: джоб інкрементально
дочитує лише хвіст і складає витягнуті поля в таблицю procedures (~1 КБ на лот).

Курсор: тримаємо позицію-підказку, але істина — last_modified. Якщо процедура
змінюється повторно, вона переїжджає в кінець фіду й решта зсувається назад,
тому позиція «пливе» — звіряємось за dateModified і локально коригуємо крок.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp

from database import get_conn
from logger import logger
from refdata import group_of

BASE_URL = "https://procedure.prozorro.sale/api/search/byDateModified"

PAGE_LIMIT = 10          # компроміс: сторінка важить ~280 КБ замість 3.6 МБ
MAX_PAGES_PER_RUN = 60   # запобіжник, щоб один запуск не читав фід вічно
MAX_BACK_STEPS = 5       # наскільки далеко відступаємо, шукаючи місце розриву
MAX_FEED_DEPTH = 3100    # стеля пагінації API: глибше за дату не пускає (422)
MAX_DATE_ROLLOVERS = 3   # скільки діб щонайбільше проходимо за один цикл
RETENTION_DAYS = 30      # скільки тримаємо кеш процедур
REQUEST_TIMEOUT = 30


# ── витягування полів ────────────────────────────────────────────────────

def _uk(value):
    """Дістає українську мову з {'uk_UA': ...} або повертає рядок як є"""
    if isinstance(value, dict):
        return value.get("uk_UA") or value.get("en_US") or ""
    return value or ""


def _dig(obj, *path):
    """Безпечний обхід вкладених словників"""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_fields(proc):
    """Перетворює повний об'єкт процедури на рядок для кешу"""
    items = proc.get("items") or []

    regions, localities, cadastrals = [], [], []
    land_area = 0.0
    for it in items:
        region = _uk(_dig(it, "address", "region"))
        if region and region not in regions:
            regions.append(region)

        locality = _uk(_dig(it, "address", "locality"))
        if locality and locality not in localities:
            localities.append(locality)

        cad = _dig(it, "itemProps", "cadastralNumber")
        if cad and cad not in cadastrals:
            cadastrals.append(cad)

        area = _dig(it, "itemProps", "landArea")
        if isinstance(area, (int, float)):
            land_area += area

    # опис буває і в процедури, і в предметах — збираємо все для пошуку по словах
    descriptions = [_uk(proc.get("description"))]
    for it in items:
        d = _uk(it.get("description"))
        if d and d not in descriptions:
            descriptions.append(d)

    bids = proc.get("bids")
    selling_method = proc.get("sellingMethod") or ""

    return {
        "procedure_id": proc.get("_id"),
        "auction_id": proc.get("auctionId"),
        "selling_method": selling_method,
        "method_group": group_of(selling_method),
        "status": proc.get("status"),
        "title": _uk(proc.get("title")),
        "description": " ".join(d for d in descriptions if d),
        "region": "; ".join(regions),
        "locality": "; ".join(localities),
        "cadastral": "; ".join(cadastrals),
        "land_area": round(land_area, 4) if land_area else None,
        "amount": _dig(proc, "value", "amount"),
        "currency": _dig(proc, "value", "currency"),
        "organizer_name": _uk(_dig(proc, "sellingEntity", "identifier", "legalName"))
                          or _uk(_dig(proc, "sellingEntity", "name")),
        "organizer_id": _dig(proc, "sellingEntity", "identifier", "id"),
        "date_published": proc.get("datePublished"),
        "date_modified": proc.get("dateModified"),
        "tender_start": _dig(proc, "tenderPeriod", "startDate"),
        "tender_end": _dig(proc, "tenderPeriod", "endDate"),
        "rectification_end": _dig(proc, "rectificationPeriod", "endDate"),
        "auction_start": _dig(proc, "auctionPeriod", "startDate"),
        "bids_count": len(bids) if isinstance(bids, list) else None,
        "min_bids": proc.get("minNumberOfQualifiedBids"),
    }


# ── мережа ───────────────────────────────────────────────────────────────

async def fetch_page(session, feed_date, page, limit=PAGE_LIMIT):
    """
    Одна сторінка фіду. Повертає (список_процедур, статус):
        'ok'      — сторінка прочитана;
        'empty'   — даних більше немає (204), доба вичерпана;
        'ceiling' — впертись у стелю пагінації (422): API не віддає записи
                    глибше ~MAX_FEED_DEPTH від початку дати, і меншим limit
                    це не обходиться — треба переходити на наступну дату;
        'error'   — мережева чи інша помилка, варто спробувати пізніше.
    """
    url = f"{BASE_URL}/{feed_date}?limit={limit}&page={page}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
            if resp.status == 204:
                return [], "empty"
            if resp.status == 422:
                return [], "ceiling"
            if resp.status != 200:
                logger.warning(f"⚠️ Фід {feed_date} сторінка {page}: HTTP {resp.status}")
                return [], "error"

            data = await resp.json(content_type=None)
            if not isinstance(data, list):
                logger.warning(f"⚠️ Фід {feed_date} сторінка {page}: несподіваний формат")
                return [], "error"
            return data, "ok"

    except asyncio.TimeoutError:
        logger.warning(f"⏱ Тайм-аут фіду {feed_date} сторінка {page}")
        return [], "error"
    except Exception as e:
        logger.error(f"❌ Помилка читання фіду {feed_date} сторінка {page}: {e}")
        return [], "error"


async def find_start_page(session, feed_date, target_modified, limit=PAGE_LIMIT):
    """
    Найменша сторінка, де вже є записи, новіші за target_modified.

    Потрібно при переході на наступну дату: фід віддає записи від початку доби,
    а нас цікавить лише хвіст після курсора. Бінарний пошук коштує ~8 запитів
    замість сотень сторінок послідовного гортання.
    """
    lo, hi = 1, MAX_FEED_DEPTH // limit

    while lo < hi:
        mid = (lo + hi) // 2
        batch, status = await fetch_page(session, feed_date, mid, limit=limit)

        if status == "error":
            return lo
        if status in ("empty", "ceiling") or not batch:
            hi = mid                      # за межами даних — шукаємо лівіше
        elif (batch[-1].get("dateModified") or "") <= target_modified:
            lo = mid + 1                  # уся сторінка вже прочитана — правіше
        else:
            hi = mid

    return lo


# ── курсор ───────────────────────────────────────────────────────────────

def get_cursor(feed_date):
    conn = get_conn()
    row = conn.execute(
        "SELECT position, last_modified, finished FROM feed_cursor WHERE feed_date = ?",
        (feed_date,)
    ).fetchone()
    conn.close()
    if row:
        return {"position": row[0] or 0, "last_modified": row[1], "finished": bool(row[2])}
    return {"position": 0, "last_modified": None, "finished": False}


def save_cursor(feed_date, position, last_modified, finished=False):
    conn = get_conn()
    conn.execute("""
        INSERT INTO feed_cursor (feed_date, position, last_modified, finished, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(feed_date) DO UPDATE SET
            position = excluded.position,
            last_modified = excluded.last_modified,
            finished = excluded.finished,
            updated_at = excluded.updated_at
    """, (feed_date, position, last_modified, 1 if finished else 0,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


# ── запис у кеш ──────────────────────────────────────────────────────────

def upsert_procedure(fields):
    """
    Кладе процедуру в кеш.
    Повертає (is_new, prev_status, prev_modified) — потрібно, щоб зрозуміти,
    чи це вперше побачений лот, чи зміна вже відомого.
    """
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()

    prev = conn.execute(
        "SELECT status, date_modified FROM procedures WHERE procedure_id = ?",
        (fields["procedure_id"],)
    ).fetchone()

    if prev is None:
        conn.execute("""
            INSERT INTO procedures (
                procedure_id, auction_id, selling_method, method_group, status,
                title, description, region, locality, cadastral, land_area,
                amount, currency, organizer_name, organizer_id,
                date_published, date_modified, tender_start, tender_end,
                rectification_end, auction_start, bids_count, min_bids,
                first_seen, last_seen
            ) VALUES (
                :procedure_id, :auction_id, :selling_method, :method_group, :status,
                :title, :description, :region, :locality, :cadastral, :land_area,
                :amount, :currency, :organizer_name, :organizer_id,
                :date_published, :date_modified, :tender_start, :tender_end,
                :rectification_end, :auction_start, :bids_count, :min_bids,
                :first_seen, :last_seen
            )
        """, {**fields, "first_seen": now, "last_seen": now})
        result = (True, None, None)
    else:
        conn.execute("""
            UPDATE procedures SET
                auction_id = :auction_id, selling_method = :selling_method,
                method_group = :method_group, status = :status, title = :title,
                description = :description, region = :region, locality = :locality,
                cadastral = :cadastral, land_area = :land_area, amount = :amount,
                currency = :currency, organizer_name = :organizer_name,
                organizer_id = :organizer_id, date_published = :date_published,
                date_modified = :date_modified, tender_start = :tender_start,
                tender_end = :tender_end, rectification_end = :rectification_end,
                auction_start = :auction_start,
                bids_count = COALESCE(:bids_count, bids_count),
                min_bids = COALESCE(:min_bids, min_bids),
                last_seen = :last_seen
            WHERE procedure_id = :procedure_id
        """, {**fields, "last_seen": now})
        result = (False, prev[0], prev[1])

    conn.commit()
    conn.close()
    return result


# ── головний прохід ──────────────────────────────────────────────────────

async def poll_feed_date(session, feed_date, max_pages=MAX_PAGES_PER_RUN):
    """
    Дочитує фід за одну добу з місця, де зупинилися.

    Повертає список подій:
        {"proc": <поля>, "is_new": bool, "prev_status": str|None, "prev_modified": str|None}
    """
    cursor = get_cursor(feed_date)
    position = cursor["position"]
    last_modified = cursor["last_modified"]   # межа «це вже читали», незмінна в межах проходу
    cursor_modified = last_modified           # те, що збережемо: просувається завжди

    # стартуємо на сторінку раніше за курсор: перекриття дешеве (вже прочитане
    # відсіється за dateModified), зате компенсує зсув пагінації
    page = max(1, position // PAGE_LIMIT)
    events = []
    pages_read = 0
    back_steps = 0
    continuity_ok = not last_modified     # без курсора перевіряти нічого
    seen_positions = position

    while pages_read < max_pages:
        batch, status = await fetch_page(session, feed_date, page)
        pages_read += 1

        if status == "error":
            break

        if status == "ceiling":
            # глибше за цю дату API не пускає — читання продовжиться з наступної доби
            save_cursor(feed_date, seen_positions, cursor_modified, finished=True)
            logger.info(f"📅 Фід {feed_date}: стеля пагінації, переходжу на наступну добу")
            return events

        if status == "empty" or not batch:
            # дійшли до кінця доби
            save_cursor(feed_date, seen_positions, cursor_modified, finished=False)
            break

        # Перевірка безперервності — лише на старті проходу. Якщо на стартовій
        # сторінці немає жодного вже прочитаного запису, між курсором і нею є розрив
        # (лот змінився повторно, переїхав у кінець і зсунув решту) — відступаємо.
        # Далі йдемо вперед без відступів: наступна сторінка й має бути вся новіша.
        if not continuity_ok:
            if (page > 1 and back_steps < MAX_BACK_STEPS
                    and all((p.get("dateModified") or "") > last_modified for p in batch)):
                page -= 1
                back_steps += 1
                continue
            continuity_ok = True

        for offset, proc in enumerate(batch):
            dm = proc.get("dateModified") or ""
            abs_pos = (page - 1) * PAGE_LIMIT + offset + 1
            seen_positions = max(seen_positions, abs_pos)

            # курсор просуваємо для кожного побаченого запису, а не лише для тих,
            # що стали подією — інакше він застигає і фід перечитується з початку
            if dm > (cursor_modified or ""):
                cursor_modified = dm

            if last_modified and dm <= last_modified:
                continue

            fields = extract_fields(proc)
            if not fields["procedure_id"]:
                continue

            is_new, prev_status, prev_modified = upsert_procedure(fields)

            # той самий dateModified ми вже опрацьовували — не дублюємо подію
            if prev_modified == fields["date_modified"]:
                continue

            events.append({
                "proc": fields,
                "is_new": is_new,
                "prev_status": prev_status,
                "prev_modified": prev_modified,
            })

        save_cursor(feed_date, seen_positions, cursor_modified, finished=False)

        if len(batch) < PAGE_LIMIT:
            break              # неповна сторінка = хвіст фіду
        page += 1

    if pages_read >= max_pages:
        logger.warning(f"⚠️ Фід {feed_date}: досягнуто ліміт {max_pages} сторінок за прохід")

    return events


def next_feed_date(today):
    """
    Дата, з якої зараз читаємо фід.

    Оскільки фід віддає записи «від указаної дати і далі», ми тримаємось однієї
    дати, доки вона не впреться в стелю пагінації. Тоді переходимо на наступну добу,
    успадковуючи курсор — так жодна зміна не губиться на межі.

    Повертає None, якщо читати нема звідки: поточну добу вичерпано стелею,
    а наступна ще не настала.
    """
    conn = get_conn()
    unfinished = conn.execute(
        "SELECT feed_date FROM feed_cursor WHERE finished = 0 ORDER BY feed_date DESC LIMIT 1"
    ).fetchone()
    last_done = conn.execute(
        "SELECT feed_date, last_modified FROM feed_cursor WHERE finished = 1 "
        "ORDER BY feed_date DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if unfinished:
        return unfinished[0]

    if not last_done:
        return today

    candidate = (datetime.strptime(last_done[0], "%Y-%m-%d")
                 + timedelta(days=1)).strftime("%Y-%m-%d")
    if candidate > today:
        return None

    # відкриваємо нову добу, успадкувавши межу «це вже читали»
    save_cursor(candidate, 0, last_done[1], finished=False)
    return candidate


async def poll_feed():
    """
    Один цикл опитування фіду. Тримається активної дати, а коли та впирається
    в стелю пагінації — переходить на наступну добу (не більше кількох за прохід).
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    events = []

    async with aiohttp.ClientSession() as session:
        for _ in range(MAX_DATE_ROLLOVERS):
            feed_date = next_feed_date(today)
            if feed_date is None:
                logger.warning(
                    "⏸ Добу вичерпано стелею пагінації Prozorro — "
                    "нові зміни дочитаю після настання наступної доби (UTC)"
                )
                break

            cursor = get_cursor(feed_date)

            # щойно відкрита доба: знаходимо, з якої сторінки продовжувати
            if cursor["position"] == 0 and cursor["last_modified"]:
                start_page = await find_start_page(session, feed_date, cursor["last_modified"])
                if start_page > 1:
                    save_cursor(feed_date, (start_page - 1) * PAGE_LIMIT,
                                cursor["last_modified"], finished=False)
                    logger.info(f"📅 Фід {feed_date}: продовжую зі сторінки {start_page}")

            events += await poll_feed_date(session, feed_date)

            if not get_cursor(feed_date)["finished"]:
                break        # дату не вичерпано — на сьогодні все прочитано

    return events


# ── разове наповнення історії ────────────────────────────────────────────

BOOTSTRAP_LIMIT = 100      # для історії вигідніші великі сторінки: менше запитів


async def bootstrap_date(session, feed_date):
    """
    Вичитує добу цілком і кладе в кеш, НЕ породжуючи подій.
    Потрібно, щоб ретроспектива «що знайшлося б за 7 днів» мала на чому працювати.
    """
    page = 1
    total = 0
    last_modified = None

    while page <= MAX_FEED_DEPTH // BOOTSTRAP_LIMIT:
        batch, status = await fetch_page(session, feed_date, page, limit=BOOTSTRAP_LIMIT)
        if status != "ok" or not batch:
            break

        for proc in batch:
            fields = extract_fields(proc)
            if not fields["procedure_id"]:
                continue
            upsert_procedure(fields)
            total += 1
            dm = fields.get("date_modified") or ""
            if dm > (last_modified or ""):
                last_modified = dm

        if len(batch) < BOOTSTRAP_LIMIT:
            break
        page += 1

    save_cursor(feed_date, total, last_modified, finished=True)
    return total


async def bootstrap_history(days):
    """
    Наповнює кеш за минулі дні — один раз на добу-кандидата.
    Дні, позначені finished, пропускаються, тому перезапуск бота нічого не переробляє.
    """
    now = datetime.now(timezone.utc)
    filled = 0

    async with aiohttp.ClientSession() as session:
        for offset in range(days, 0, -1):
            feed_date = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            if get_cursor(feed_date)["finished"]:
                continue

            logger.info(f"📚 Наповнюю кеш за {feed_date}…")
            count = await bootstrap_date(session, feed_date)
            filled += count
            logger.info(f"📚 {feed_date}: до кешу додано {count} процедур")

    if filled:
        logger.info(f"📚 Історію завантажено: {filled} процедур")
    return filled


# ── обслуговування ───────────────────────────────────────────────────────

def cleanup_old_procedures(days=RETENTION_DAYS):
    """Прибирає з кешу процедури, які давно не оновлювались"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_conn()
    cur = conn.execute("DELETE FROM procedures WHERE last_seen < ?", (cutoff,))
    removed = cur.rowcount
    conn.execute("DELETE FROM feed_cursor WHERE feed_date < ?", (cutoff[:10],))
    conn.commit()
    conn.close()
    if removed:
        logger.info(f"🧹 З кешу прибрано {removed} застарілих процедур")
    return removed


def cache_stats():
    """Коротка статистика кешу для адмінів"""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0]
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    fresh = conn.execute(
        "SELECT COUNT(*) FROM procedures WHERE date_published >= ?", (day_ago,)
    ).fetchone()[0]
    last = conn.execute("SELECT MAX(date_modified) FROM procedures").fetchone()[0]
    conn.close()
    return {"total": total, "published_24h": fresh, "last_modified": last}
