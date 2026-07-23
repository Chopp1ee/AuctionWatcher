# bidtracker.py — оцінка кількості поданих заявок за змінами dateModified
"""
До розкриття майданчики не бачать цінових пропозицій: у процедурі в статусі
active_tendering поле bids приходить як null. Але кожна подача заявки зачіпає лот
і зсуває dateModified. Отже, рахуючи зміни dateModified у вікні подання заявок,
можна оцінити, скільки заявок уже подано.

Що робить модуль:
  • кожні BID_WATCH_INTERVAL секунд опитує «гарячі» лоти зі списків відстеження адмінів
    (16 КБ на лот — це дешевше за фід, тому можна часто);
  • зараховує зміну як ймовірну заявку лише в active_tendering ПІСЛЯ кінця
    періоду виправлень: доти умови лота ще правлять, і зміни означають що завгодно;
  • за годину до дедлайну надсилає підсумок;
  • коли лот переходить у стадію з розкритими ставками — звіряє оцінку з фактом
    і накопичує статистику точності методу.

Метод дає саме оцінку: одна заявка може дати кілька змін, а дві заявки в межах
одного інтервалу опитування зіллються в одну. Тому в інтерфейсі всюди «~».
"""
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp

from api import format_date
from config import ADMIN_IDS, BID_SUMMARY_LEAD_MINUTES
from database import get_conn
from feed import extract_fields, upsert_procedure
from logger import logger

API_PROCEDURE_URL = "https://procedure.prozorro.sale/api/procedures/{procedure_id}"
API_SEARCH_URL = "https://procedure.prozorro.sale/api/search/byAuctionId/{auction_id}"

# статуси, у яких лот вартий частого опитування
HOT_STATUSES = ("active_tendering", "active_rectification")

# статуси, у яких ставки вже розкриті
REVEALED_STATUSES = ("active_qualification", "active_awarded", "active_contract",
                     "complete", "unsuccessful", "pending_payment", "cancelled")


# ── які лоти пильнуємо ───────────────────────────────────────────────────

def get_watched_lots():
    """
    Лоти зі списків відстеження адмінів, які зараз варто опитувати часто.
    Лот, якого ще немає в кеші, теж беремо — після першого опитування
    він або залишиться в списку, або відпаде за статусом.
    """
    if not ADMIN_IDS:
        return []

    admins = ",".join("?" * len(ADMIN_IDS))
    statuses = ",".join("?" * len(HOT_STATUSES))
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT DISTINCT ua.auction_id, ua.url,
               p.procedure_id, p.status, p.tender_end, p.rectification_end, p.title
        FROM user_auctions ua
        LEFT JOIN procedures p ON p.auction_id = ua.auction_id
        WHERE ua.chat_id IN ({admins})
          AND (p.status IS NULL OR p.status IN ({statuses}))
    """, list(ADMIN_IDS) + list(HOT_STATUSES)).fetchall()
    conn.close()

    return [
        {
            "auction_id": r[0], "url": r[1], "procedure_id": r[2],
            "status": r[3], "tender_end": r[4], "rectification_end": r[5], "title": r[6],
        }
        for r in rows
    ]


def admins_watching(auction_id):
    """Адміни, у списку відстеження яких є цей лот"""
    if not ADMIN_IDS:
        return []
    placeholders = ",".join("?" * len(ADMIN_IDS))
    conn = get_conn()
    rows = conn.execute(
        f"SELECT DISTINCT chat_id FROM user_auctions "
        f"WHERE auction_id = ? AND chat_id IN ({placeholders})",
        [auction_id] + ADMIN_IDS
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ── облік подій ──────────────────────────────────────────────────────────

def _counts_as_bid(proc):
    """
    Чи зараховувати зміну як ймовірну заявку.
    Тільки active_tendering і тільки після завершення періоду виправлень.
    """
    if proc.get("status") != "active_tendering":
        return False

    rectification_end = proc.get("rectification_end")
    if not rectification_end:
        return True         # періоду виправлень немає — вважаємо вікно чистим

    try:
        end = datetime.fromisoformat(rectification_end.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True

    return datetime.now(timezone.utc) > end


def record_event(proc):
    """
    Фіксує зміну лота. Повертає (зараховано_як_заявку, поточна_оцінка)
    або (False, оцінка), якщо цю зміну вже бачили.
    """
    auction_id = proc.get("auction_id")
    date_modified = proc.get("date_modified")
    counted = _counts_as_bid(proc)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO bid_events "
        "(auction_id, procedure_id, date_modified, status, counted, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (auction_id, proc.get("procedure_id"), date_modified,
         proc.get("status"), 1 if counted else 0, now)
    )
    is_fresh = cur.rowcount > 0

    estimate = conn.execute(
        "SELECT COUNT(*) FROM bid_events WHERE auction_id = ? AND counted = 1",
        (auction_id,)
    ).fetchone()[0]

    conn.execute("""
        INSERT INTO bid_estimates
            (auction_id, procedure_id, title, estimate, tender_end, min_bids, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(auction_id) DO UPDATE SET
            estimate = excluded.estimate,
            title = COALESCE(excluded.title, title),
            tender_end = COALESCE(excluded.tender_end, tender_end),
            updated_at = excluded.updated_at
    """, (auction_id, proc.get("procedure_id"), proc.get("title"), estimate,
          proc.get("tender_end"), proc.get("min_bids"), now))

    conn.commit()
    conn.close()

    return (is_fresh and counted), estimate


def record_actual(auction_id, actual):
    """Записує фактичну кількість ставок після розкриття"""
    conn = get_conn()
    row = conn.execute(
        "SELECT estimate, actual FROM bid_estimates WHERE auction_id = ?", (auction_id,)
    ).fetchone()

    if row is None or row[1] is not None:
        conn.close()
        return None                      # не стежили або вже звірили

    conn.execute(
        "UPDATE bid_estimates SET actual = ?, revealed_at = ? WHERE auction_id = ?",
        (actual, datetime.now(timezone.utc).isoformat(), auction_id)
    )
    conn.commit()
    conn.close()
    return row[0]                        # наша оцінка


def import_history_events():
    """
    Разово переносить у лічильник уже накопичену історію змін (auction_history).

    Стара таблиця фіксує кожну зміну dateModified доданих лотів, тож для лотів,
    де подання заявок триває давно, оцінка не починається з нуля. Записи там
    дублюються по кожному користувачу — дублі відсікає UNIQUE(auction_id, date_modified).

    Зараховуємо за тим самим правилом, що й наживо: лише зміни в active_tendering
    після завершення періоду виправлень.
    """
    conn = get_conn()

    rows = conn.execute("""
        SELECT DISTINCT h.auction_id, h.new_date, h.new_status,
               p.procedure_id, p.rectification_end
        FROM auction_history h
        LEFT JOIN procedures p ON p.auction_id = h.auction_id
        WHERE h.new_date IS NOT NULL
        ORDER BY h.new_date
    """).fetchall()

    imported = 0
    for auction_id, new_date, new_status, procedure_id, rectification_end in rows:
        counted = new_status == "active_tendering"
        if counted and rectification_end:
            try:
                end = datetime.fromisoformat(rectification_end.replace("Z", "+00:00"))
                moment = datetime.fromisoformat(new_date.replace("Z", "+00:00"))
                counted = moment > end
            except (ValueError, AttributeError):
                pass

        cur = conn.execute(
            "INSERT OR IGNORE INTO bid_events "
            "(auction_id, procedure_id, date_modified, status, counted, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (auction_id, procedure_id, new_date, new_status,
             1 if counted else 0, new_date)
        )
        imported += cur.rowcount

    conn.commit()

    # перераховуємо оцінки з урахуванням імпортованого
    auctions = conn.execute(
        "SELECT DISTINCT auction_id FROM bid_events"
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    for (auction_id,) in auctions:
        estimate = conn.execute(
            "SELECT COUNT(*) FROM bid_events WHERE auction_id = ? AND counted = 1",
            (auction_id,)
        ).fetchone()[0]

        meta = conn.execute(
            "SELECT procedure_id, title, tender_end, min_bids FROM procedures "
            "WHERE auction_id = ?", (auction_id,)
        ).fetchone() or (None, None, None, None)

        conn.execute("""
            INSERT INTO bid_estimates
                (auction_id, procedure_id, title, estimate, tender_end, min_bids, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(auction_id) DO UPDATE SET
                estimate = excluded.estimate,
                title = COALESCE(bid_estimates.title, excluded.title),
                tender_end = COALESCE(bid_estimates.tender_end, excluded.tender_end),
                min_bids = COALESCE(bid_estimates.min_bids, excluded.min_bids),
                updated_at = excluded.updated_at
        """, (auction_id, meta[0], meta[1], estimate, meta[2], meta[3], now))

    conn.commit()
    conn.close()

    if imported:
        logger.info(f"📥 У лічильник заявок імпортовано {imported} змін з історії")
    return imported


def event_counts(auction_id):
    """(усього зафіксовано змін, зараховано як заявки, коли почали рахувати)"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*), SUM(counted), MIN(date_modified) FROM bid_events WHERE auction_id = ?",
        (auction_id,)
    ).fetchone()
    conn.close()
    return (row[0] or 0), (row[1] or 0), row[2]


def accuracy_stats():
    """Накопичена точність методу по звірених лотах"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT estimate, actual FROM bid_estimates WHERE actual IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        return None

    diffs = [abs(e - a) for e, a in rows]
    exact = sum(1 for e, a in rows if e == a)
    return {
        "checked": len(rows),
        "exact": exact,
        "avg_error": sum(diffs) / len(diffs),
    }


def get_estimates(only_active=True):
    """Таблиця оцінок для команди /bids"""
    conn = get_conn()
    query = """
        SELECT b.auction_id, b.title, b.estimate, b.actual, b.tender_end,
               p.status, p.min_bids, b.updated_at
        FROM bid_estimates b
        LEFT JOIN procedures p ON p.auction_id = b.auction_id
    """
    if only_active:
        query += " WHERE b.actual IS NULL"
    query += " ORDER BY COALESCE(b.tender_end, '9999') ASC"

    rows = conn.execute(query).fetchall()
    conn.close()

    return [
        {
            "auction_id": r[0], "title": r[1], "estimate": r[2], "actual": r[3],
            "tender_end": r[4], "status": r[5], "min_bids": r[6], "updated_at": r[7],
        }
        for r in rows
    ]


# ── опитування ───────────────────────────────────────────────────────────

async def _fetch_procedure(session, lot):
    """Тягне повний об'єкт лота: спершу за прямим URL, інакше через пошук за auctionId"""
    url = lot.get("url")
    if not url and lot.get("procedure_id"):
        url = API_PROCEDURE_URL.format(procedure_id=lot["procedure_id"])
    if not url:
        url = API_SEARCH_URL.format(auction_id=lot["auction_id"])

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if isinstance(data, list):
                data = data[0] if data else None
            return data
    except Exception as e:
        logger.error(f"❌ Помилка опитування лота {lot.get('auction_id')}: {e}")
        return None


async def import_bid_history_job(context):
    """Джоб: разовий перенос історії змін у лічильник заявок"""
    try:
        import_history_events()
    except Exception as e:
        logger.error(f"❌ Помилка імпорту історії в лічильник: {e}")


async def poll_bid_watch(context):
    """Джоб: часте опитування «гарячих» лотів адмінів"""
    lots = get_watched_lots()
    if not lots:
        return

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(_fetch_procedure(session, lot) for lot in lots),
            return_exceptions=True
        )

    for lot, raw in zip(lots, results):
        if not isinstance(raw, dict):
            continue

        fields = extract_fields(raw)
        if not fields.get("procedure_id"):
            continue

        fields["min_bids"] = raw.get("minNumberOfQualifiedBids")

        prev = upsert_procedure(fields)          # (is_new, prev_status, prev_modified)
        _, prev_status, prev_modified = prev

        # ставки розкрились — звіряємо оцінку з фактом
        if fields.get("bids_count") is not None and fields.get("status") in REVEALED_STATUSES:
            estimate = record_actual(fields["auction_id"], fields["bids_count"])
            if estimate is not None:
                await _notify_reveal(context.bot, fields, estimate)
            continue

        if prev_modified == fields.get("date_modified"):
            continue                              # нічого не змінилось

        counted, estimate = record_event(fields)
        if counted:
            await _notify_activity(context.bot, fields, estimate)


# ── сповіщення адмінам ───────────────────────────────────────────────────

async def _notify_activity(bot, proc, estimate):
    """Миттєве сповіщення про зафіксовану активність"""
    title = (proc.get("title") or "")[:80]
    text = (
        f"⚡ Активність на лоті\n\n"
        f"🆔 {proc.get('auction_id')}\n"
        f"📄 {title}\n"
        f"📊 Ймовірних заявок: ~{estimate}\n"
        f"🕒 {format_date(proc.get('date_modified'))}"
    )
    if proc.get("tender_end"):
        text += f"\n⏳ Заявки до: {format_date(proc['tender_end'])}"

    for chat_id in admins_watching(proc.get("auction_id")):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"❌ Помилка сповіщення про активність для {chat_id}: {e}")


async def _notify_reveal(bot, proc, estimate):
    """Звірка оцінки з фактом після розкриття ставок"""
    actual = proc.get("bids_count")
    diff = abs(estimate - actual)
    verdict = "точно" if diff == 0 else f"похибка ±{diff}"

    text = (
        f"🔓 Ставки розкрито\n\n"
        f"🆔 {proc.get('auction_id')}\n"
        f"📄 {(proc.get('title') or '')[:80]}\n\n"
        f"📊 Наша оцінка: ~{estimate}\n"
        f"✅ Фактично заявок: {actual}\n"
        f"🎯 Результат: {verdict}"
    )

    stats = accuracy_stats()
    if stats and stats["checked"] > 1:
        text += (
            f"\n\n📈 Точність методу: {stats['exact']}/{stats['checked']} влучань, "
            f"середня похибка ±{stats['avg_error']:.1f}"
        )

    for chat_id in admins_watching(proc.get("auction_id")):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"❌ Помилка сповіщення про розкриття для {chat_id}: {e}")


async def bid_deadline_summary(context):
    """Джоб: підсумок за годину до закриття подання заявок"""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=BID_SUMMARY_LEAD_MINUTES)

    conn = get_conn()
    rows = conn.execute("""
        SELECT b.auction_id, b.title, b.estimate, b.tender_end, p.min_bids, p.status
        FROM bid_estimates b
        LEFT JOIN procedures p ON p.auction_id = b.auction_id
        WHERE b.summary_sent = 0
          AND b.actual IS NULL
          AND b.tender_end IS NOT NULL
          AND b.tender_end <= ?
          AND b.tender_end > ?
    """, (deadline.isoformat(), now.isoformat())).fetchall()
    conn.close()

    for auction_id, title, estimate, tender_end, min_bids, status in rows:
        text = (
            f"⏰ Скоро закриття подання заявок\n\n"
            f"🆔 {auction_id}\n"
            f"📄 {(title or '')[:80]}\n"
            f"⏳ Дедлайн: {format_date(tender_end)}\n\n"
            f"📊 Оцінка поданих заявок: ~{estimate}"
        )
        if min_bids:
            text += f"\n🎯 Мінімум для торгів: {min_bids}"
            if estimate < min_bids:
                text += "\n\n⚠️ Заявок може не вистачити — аукціон ризикує не відбутися."

        for chat_id in admins_watching(auction_id):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"❌ Помилка підсумку для {chat_id}: {e}")

        conn = get_conn()
        conn.execute(
            "UPDATE bid_estimates SET summary_sent = 1 WHERE auction_id = ?", (auction_id,)
        )
        conn.commit()
        conn.close()
