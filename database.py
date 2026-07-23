# database.py
import sqlite3
from api import get_auction_info, format_date

DB_NAME = "auctions.db"


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