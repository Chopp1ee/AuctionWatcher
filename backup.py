# backup.py
import shutil
from datetime import datetime
import os

def backup_database():
    """Створює бек-ап бази даних"""
    db_path = "auctions.db"
    backup_dir = "backups"
    
    # Перевіряємо чи існує база
    if not os.path.exists(db_path):
        print("❌ База даних не знайдена")
        return None
    
    # Створюємо папку для бек-апів
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
    
    # Створюємо бек-ап
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{backup_dir}/auctions_{timestamp}.db"
    
    shutil.copy2(db_path, backup_path)
    print(f"✅ Бек-ап створено: {backup_path}")
    
    # Видаляємо старі бек-апи (залишаємо останні 7)
    backups = sorted([f for f in os.listdir(backup_dir) if f.startswith("auctions_")])
    if len(backups) > 7:
        for old_backup in backups[:-7]:
            os.remove(f"{backup_dir}/{old_backup}")
            print(f"🗑 Видалено старий бек-ап: {old_backup}")
    
    return backup_path

if __name__ == "__main__":
    # Якщо запустити цей файл окремо - створить бек-ап
    backup_database()