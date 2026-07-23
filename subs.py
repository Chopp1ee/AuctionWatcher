# subs.py — підписки користувачів, квоти сповіщень, дедуплікація
import sqlite3
from datetime import datetime, timezone

from config import DAILY_NOTIFY_LIMIT, MAX_SUBS_PER_USER
from database import get_conn

# поля фільтра, які можна редагувати з майстра
FILTER_FIELDS = (
    "keywords", "exclude_words", "method_groups", "regions",
    "organizer", "cadastral", "price_min", "price_max", "area_min", "area_max",
)


def _rows(cursor):
    cursor.row_factory = sqlite3.Row
    return cursor


def create_subscription(chat_id, name, **fields):
    """Створює підписку. Повертає її id або None, якщо ліміт вичерпано."""
    if count_subscriptions(chat_id) >= MAX_SUBS_PER_USER:
        return None

    columns = ["chat_id", "name", "created_at"]
    values = [chat_id, name, datetime.now(timezone.utc).isoformat()]

    for key in FILTER_FIELDS:
        if key in fields:
            columns.append(key)
            values.append(fields[key])

    conn = get_conn()
    cur = conn.execute(
        f"INSERT INTO subscriptions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        values
    )
    sub_id = cur.lastrowid
    conn.commit()
    conn.close()
    return sub_id


def get_subscription(sub_id):
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_subscriptions(chat_id):
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_subscriptions(chat_id):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE chat_id = ?", (chat_id,)
    ).fetchone()[0]
    conn.close()
    return n


def get_active_subscriptions():
    """Усі увімкнені підписки — для матчингу нових лотів"""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM subscriptions WHERE enabled = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_subscription(sub_id, **fields):
    allowed = {k: v for k, v in fields.items() if k in FILTER_FIELDS or k in ("name", "enabled")}
    if not allowed:
        return

    conn = get_conn()
    conn.execute(
        f"UPDATE subscriptions SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?",
        list(allowed.values()) + [sub_id]
    )
    conn.commit()
    conn.close()


def delete_subscription(sub_id):
    conn = get_conn()
    conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    conn.execute("DELETE FROM sub_notifications WHERE sub_id = ?", (sub_id,))
    conn.execute("DELETE FROM notify_quota WHERE sub_id = ?", (sub_id,))
    conn.commit()
    conn.close()


def toggle_subscription(sub_id):
    """Вмикає/вимикає підписку. Повертає новий стан."""
    conn = get_conn()
    row = conn.execute("SELECT enabled FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    if row is None:
        conn.close()
        return None
    new_state = 0 if row[0] else 1
    conn.execute("UPDATE subscriptions SET enabled = ? WHERE id = ?", (new_state, sub_id))
    conn.commit()
    conn.close()
    return bool(new_state)


# ── дедуплікація сповіщень ───────────────────────────────────────────────

def was_notified(sub_id, procedure_id, event):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM sub_notifications WHERE sub_id = ? AND procedure_id = ? AND event = ?",
        (sub_id, procedure_id, event)
    ).fetchone()
    conn.close()
    return row is not None


def mark_notified(sub_id, procedure_id, event):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO sub_notifications (sub_id, procedure_id, event, notified_at) "
        "VALUES (?, ?, ?, ?)",
        (sub_id, procedure_id, event, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def seed_notified(sub_id, procedure_ids, event="published"):
    """
    Позначає лоти як уже показані — щоб ретроспектива не прилетіла ще раз
    звичайним сповіщенням.
    """
    if not procedure_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    conn.executemany(
        "INSERT OR IGNORE INTO sub_notifications (sub_id, procedure_id, event, notified_at) "
        "VALUES (?, ?, ?, ?)",
        [(sub_id, pid, event, now) for pid in procedure_ids]
    )
    conn.commit()
    conn.close()


# ── денна квота ──────────────────────────────────────────────────────────

def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def quota_state(sub_id):
    """(надіслано сьогодні, ліміт, чи попереджали)"""
    conn = get_conn()
    row = conn.execute(
        "SELECT sent, warned FROM notify_quota WHERE sub_id = ? AND day = ?",
        (sub_id, _today())
    ).fetchone()
    conn.close()
    sent, warned = (row[0], bool(row[1])) if row else (0, False)
    return sent, DAILY_NOTIFY_LIMIT, warned


def quota_consume(sub_id):
    """
    Займає одиницю квоти.
    Повертає 'ok' | 'limit_reached' (саме зараз вичерпали) | 'over' (вже вичерпано).
    """
    day = _today()
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO notify_quota (sub_id, day, sent, warned) VALUES (?, ?, 0, 0)",
        (sub_id, day)
    )
    row = conn.execute(
        "SELECT sent, warned FROM notify_quota WHERE sub_id = ? AND day = ?", (sub_id, day)
    ).fetchone()
    sent, warned = row[0], bool(row[1])

    if sent >= DAILY_NOTIFY_LIMIT:
        conn.close()
        return "over" if warned else "limit_reached"

    conn.execute(
        "UPDATE notify_quota SET sent = sent + 1 WHERE sub_id = ? AND day = ?", (sub_id, day)
    )
    conn.commit()
    conn.close()
    return "ok"


def quota_mark_warned(sub_id):
    conn = get_conn()
    conn.execute(
        "UPDATE notify_quota SET warned = 1 WHERE sub_id = ? AND day = ?", (sub_id, _today())
    )
    conn.commit()
    conn.close()


def quota_skipped_today(sub_id):
    """Скільки лотів не показали через ліміт (для тексту попередження)"""
    conn = get_conn()
    row = conn.execute(
        "SELECT sent FROM notify_quota WHERE sub_id = ? AND day = ?", (sub_id, _today())
    ).fetchone()
    conn.close()
    return max(0, (row[0] if row else 0) - DAILY_NOTIFY_LIMIT)
