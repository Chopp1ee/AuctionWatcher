# database.py
import sqlite3
from api import get_auction_info, format_date
from config import DATABASE_PATH

DB_NAME = DATABASE_PATH


def get_conn():
    """Підключення з увімкненим WAL — бот пише з кількох джобів одночасно"""
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Таблиця для користувачів
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TEXT
        )
    """)

    # Таблиця для аукціонів користувачів
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_auctions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            auction_id TEXT,
            url TEXT,
            title TEXT,
            status TEXT,
            date_modified TEXT,
            added_at TEXT,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id),
            UNIQUE(chat_id, auction_id)
        )
    """)

    # Таблиця для історії змін
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auction_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            auction_id TEXT,
            old_status TEXT,
            new_status TEXT,
            old_date TEXT,
            new_date TEXT,
            changed_at TEXT,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id)
        )
    """)

    # ── Пошук нових лотів за підписками ──────────────────────────────────

    # Локальний кеш процедур із фіду Prozorro.
    # Тягнути фід на кожен запит неможливо (3.6 МБ на 100 записів), тому
    # один фоновий джоб наповнює кеш, а матчинг і ретроспектива працюють по ньому.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS procedures (
            procedure_id      TEXT PRIMARY KEY,
            auction_id        TEXT,
            selling_method    TEXT,
            method_group      TEXT,
            status            TEXT,
            title             TEXT,
            description       TEXT,
            region            TEXT,
            locality          TEXT,
            cadastral         TEXT,
            land_area         REAL,
            amount            REAL,
            currency          TEXT,
            organizer_name    TEXT,
            organizer_id      TEXT,
            date_published    TEXT,
            date_modified     TEXT,
            tender_start      TEXT,
            tender_end        TEXT,
            rectification_end TEXT,
            auction_start     TEXT,
            bids_count        INTEGER,
            min_bids          INTEGER,
            first_seen        TEXT,
            last_seen         TEXT
        )
    """)
    # для баз, створених до появи колонки
    if "min_bids" not in {r[1] for r in cursor.execute("PRAGMA table_info(procedures)")}:
        cursor.execute("ALTER TABLE procedures ADD COLUMN min_bids INTEGER")
    for idx, col in [
        ("idx_proc_published", "date_published"),
        ("idx_proc_modified", "date_modified"),
        ("idx_proc_group", "method_group"),
        ("idx_proc_region", "region"),
        ("idx_proc_auction", "auction_id"),
        ("idx_proc_status", "status"),
    ]:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON procedures({col})")

    # Курсор обходу фіду: на чому зупинилися по кожній добі
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feed_cursor (
            feed_date     TEXT PRIMARY KEY,
            position      INTEGER DEFAULT 0,
            last_modified TEXT,
            finished      INTEGER DEFAULT 0,
            updated_at    TEXT
        )
    """)

    # Підписки користувачів (до 5 на людину)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER,
            name          TEXT,
            enabled       INTEGER DEFAULT 1,
            keywords      TEXT,
            exclude_words TEXT,
            method_groups TEXT,
            regions       TEXT,
            organizer     TEXT,
            cadastral     TEXT,
            price_min     REAL,
            price_max     REAL,
            area_min      REAL,
            area_max      REAL,
            created_at    TEXT,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subs_chat ON subscriptions(chat_id)")

    # Що вже надсилали — щоб один лот не прийшов двічі
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sub_notifications (
            sub_id       INTEGER,
            procedure_id TEXT,
            event        TEXT,
            notified_at  TEXT,
            PRIMARY KEY (sub_id, procedure_id, event)
        )
    """)

    # Денна квота сповіщень на підписку
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notify_quota (
            sub_id INTEGER,
            day    TEXT,
            sent   INTEGER DEFAULT 0,
            warned INTEGER DEFAULT 0,
            PRIMARY KEY (sub_id, day)
        )
    """)

    # ── Лічильник ставок (адмінська фіча) ────────────────────────────────

    # Кожна зафіксована зміна dateModified лота під наглядом
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bid_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id    TEXT,
            procedure_id  TEXT,
            date_modified TEXT,
            status        TEXT,
            counted       INTEGER DEFAULT 0,
            detected_at   TEXT,
            UNIQUE(auction_id, date_modified)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bidev_auction ON bid_events(auction_id)")

    # Підсумок по лоту: наша оцінка проти факту після розкриття
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bid_estimates (
            auction_id   TEXT PRIMARY KEY,
            procedure_id TEXT,
            title        TEXT,
            estimate     INTEGER DEFAULT 0,
            actual       INTEGER,
            tender_end   TEXT,
            min_bids     INTEGER,
            summary_sent INTEGER DEFAULT 0,
            revealed_at  TEXT,
            updated_at   TEXT
        )
    """)

    conn.commit()
    conn.close()


def add_user(chat_id, username=None, first_name=None, last_name=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    from datetime import datetime
    now = datetime.now().isoformat()

    cursor.execute("""
        INSERT OR REPLACE INTO users
        (chat_id, username, first_name, last_name, registered_at)
        VALUES (?, ?, ?, ?, ?)
    """, (chat_id, username, first_name, last_name, now))

    conn.commit()
    conn.close()


def add_user_auction(chat_id, info, url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    from datetime import datetime
    now = datetime.now().isoformat()

    cursor.execute("""
        INSERT OR REPLACE INTO user_auctions
        (chat_id, auction_id, url, title, status, date_modified, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        chat_id,
        info["auction_id"],
        url,
        info["title"],
        info["status"],
        info["date_modified"],
        now
    ))

    conn.commit()
    conn.close()


def get_user_auctions(chat_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT auction_id, url, title, status, date_modified
        FROM user_auctions
        WHERE chat_id = ?
        ORDER BY added_at DESC
    """, (chat_id,))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_all_users_for_monitor():
    """Отримує всіх користувачів з їх аукціонами для моніторингу"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.chat_id, ua.auction_id, ua.url, ua.date_modified, ua.status
        FROM user_auctions ua
        JOIN users u ON u.chat_id = ua.chat_id
        ORDER BY u.chat_id
    """)

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_user_auction_by_id(chat_id, auction_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT url, date_modified, status
        FROM user_auctions
        WHERE chat_id = ? AND auction_id = ?
    """, (chat_id, auction_id))

    row = cursor.fetchone()
    conn.close()

    return row


def update_user_auction(chat_id, auction_id, new_date, new_status):
    """Оновлює дані аукціону та додає запис в історію"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Отримуємо старі дані
    cursor.execute("""
        SELECT date_modified, status
        FROM user_auctions
        WHERE chat_id = ? AND auction_id = ?
    """, (chat_id, auction_id))
    
    old_data = cursor.fetchone()
    
    if old_data:
        old_date, old_status = old_data
        
        # Оновлюємо аукціон
        cursor.execute("""
            UPDATE user_auctions
            SET date_modified = ?, status = ?
            WHERE chat_id = ? AND auction_id = ?
        """, (new_date, new_status, chat_id, auction_id))
        
        # Додаємо запис в історію
        from datetime import datetime
        now = datetime.now().isoformat()
        
        cursor.execute("""
            INSERT INTO auction_history
            (chat_id, auction_id, old_status, new_status, old_date, new_date, changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, auction_id, old_status, new_status, old_date, new_date, now))
    
    conn.commit()
    conn.close()


def get_auction_history(chat_id, auction_id, limit=10):
    """Отримує історію змін для конкретного аукціону (з лімітом)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT old_status, new_status, old_date, new_date, changed_at
        FROM auction_history
        WHERE chat_id = ? AND auction_id = ?
        ORDER BY changed_at DESC
        LIMIT ?
    """, (chat_id, auction_id, limit))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_full_auction_history(chat_id, auction_id):
    """Отримує повну історію змін для аукціону без обмежень (для експорту)"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT old_status, new_status, old_date, new_date, changed_at
        FROM auction_history
        WHERE chat_id = ? AND auction_id = ?
        ORDER BY changed_at ASC
    """, (chat_id, auction_id))

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_auction_details(chat_id, auction_id):
    """Отримує деталі аукціону"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT auction_id, title, status, date_modified, added_at
        FROM user_auctions
        WHERE chat_id = ? AND auction_id = ?
    """, (chat_id, auction_id))

    row = cursor.fetchone()
    conn.close()

    return row


def remove_user_auction(chat_id, auction_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM user_auctions
        WHERE chat_id = ? AND auction_id = ?
    """, (chat_id, auction_id))

    conn.commit()
    conn.close()


def get_statistics():
    """Отримує статистику"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Кількість користувачів
    cursor.execute("SELECT COUNT(*) FROM users")
    users_count = cursor.fetchone()[0]
    
    # Кількість аукціонів
    cursor.execute("SELECT COUNT(*) FROM user_auctions")
    auctions_count = cursor.fetchone()[0]
    
    # Кількість змін
    cursor.execute("SELECT COUNT(*) FROM auction_history")
    changes_count = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        "users": users_count,
        "auctions": auctions_count,
        "changes": changes_count
    }


if __name__ == "__main__":
    init_db()
    print("✅ Базу даних створено!")