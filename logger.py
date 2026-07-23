# logger.py
import logging
import sys
from datetime import datetime
import os

# Створюємо папку для логів якщо її немає
if not os.path.exists('logs'):
    os.makedirs('logs')

def setup_logger():
    """Налаштовує логування"""
    logger = logging.getLogger('AuctionBot')
    logger.setLevel(logging.INFO)
    
    # Формат логування
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Логування в файл
    log_filename = f'logs/bot_{datetime.now().strftime("%Y%m%d")}.log'
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    
    # Логування в консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# Створюємо глобальний логгер
logger = setup_logger()