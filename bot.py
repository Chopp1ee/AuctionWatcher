# bot.py - ПОВНИЙ ФАЙЛ З ВСІМА ОНОВЛЕННЯМИ
import asyncio
import sqlite3
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import TOKEN, ADMIN_IDS, CHECK_INTERVAL
from api import (
    get_auction_info,
    extract_auction_id,
    auction_id_to_api_url,
    format_date,
    translate_status,
)
from database import (
    init_db, 
    add_user, 
    add_user_auction, 
    get_user_auctions,
    remove_user_auction,
    get_user_auction_by_id,
    get_auction_history,
    get_full_auction_history,
    get_auction_details,
    DB_NAME
)
from monitor import check_auctions
from logger import logger
from backup import backup_database
from exporter import export_history_to_excel, cleanup_old_exports
from config import BID_WATCH_INTERVAL, FEED_INTERVAL
from watcher import bootstrap_job, daily_cache_cleanup, watch_new_lots
from bidtracker import bid_deadline_summary, import_bid_history_job, poll_bid_watch
from handlers import (
    bids_command,
    feedstat_command,
    filters_command,
    handle_bids_callback,
    handle_sub_callback,
    handle_track_callback,
    handle_wizard_callback,
    handle_wizard_text,
)
from wizard import clear_wizard, get_state


# Створюємо базу даних при запуску
init_db()


# Головне меню (кнопки внизу екрану)
def get_main_menu(chat_id=None):
    """Адміни бачать додатковий ряд з лічильником заявок і станом моніторингу"""
    keyboard = [
        [
            KeyboardButton("📡 Мої фільтри"),
            KeyboardButton("📋 Мої аукціони")
        ],
        [
            KeyboardButton("➕ Додати аукціон"),
            KeyboardButton("📜 Історія змін")
        ],
        [
            KeyboardButton("⚡ Швидке видалення"),
            KeyboardButton("📊 Експорт в Excel")
        ],
        [
            KeyboardButton("📖 Допомога")
        ]
    ]

    if chat_id in ADMIN_IDS:
        keyboard.insert(0, [
            KeyboardButton("🎯 Заявки на лотах"),
            KeyboardButton("📈 Стан моніторингу")
        ])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# Клавіатура для вибору аукціону (inline кнопки)
def get_auction_keyboard(auctions, action):
    """
    Створює клавіатуру зі списком аукціонів
    action: 'history' або 'remove'
    """
    keyboard = []
    for auction_id, _, title, _, _ in auctions[:10]:
        button = InlineKeyboardButton(
            auction_id,
            callback_data=f"{action}_{auction_id}"
        )
        keyboard.append([button])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    
    return InlineKeyboardMarkup(keyboard)


async def send_auctions_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, edit: bool = False):
    """Надсилає сторінку з аукціонами (тільки перегляд)"""
    try:
        auctions = context.user_data.get('auctions_list', [])
        if not auctions:
            if edit and hasattr(update, 'callback_query'):
                await update.callback_query.edit_message_text("📭 Список аукціонів порожній.")
            else:
                await update.message.reply_text("📭 Список аукціонів порожній.")
            return
        
        ITEMS_PER_PAGE = 5
        total_pages = (len(auctions) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0
        
        start_idx = page * ITEMS_PER_PAGE
        end_idx = min(start_idx + ITEMS_PER_PAGE, len(auctions))
        
        message = f"📋 Ваші аукціони (сторінка {page + 1}/{total_pages}):\n\n"
        
        for idx, (auction_id, url, title, status, date_modified) in enumerate(auctions[start_idx:end_idx], start_idx + 1):
            status_text = translate_status(status)
            date_f = format_date(date_modified)
            short_title = title[:80] + '...' if len(title) > 80 else title
            
            message += (
                f"{idx}. {auction_id}\n"
                f"   📄 {short_title}\n"
                f"   📊 {status_text}\n"
                f"   🕒 {date_f}\n\n"
            )
        
        # Кнопки навігації
        keyboard = []
        nav_buttons = []
        
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list_page_{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"list_page_{page + 1}"))
        
        keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔄 Оновити", callback_data="refresh_list")])
        keyboard.append([InlineKeyboardButton("🔙 Головне меню", callback_data="back_to_main")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Якщо це редагування (з callback_query)
        if edit and hasattr(update, 'callback_query'):
            try:
                # Перевіряємо чи змінився текст перед редагуванням
                current_message = update.callback_query.message
                if current_message.text != message or current_message.reply_markup != reply_markup:
                    await update.callback_query.edit_message_text(
                        message,
                        reply_markup=reply_markup
                    )
                else:
                    # Якщо текст не змінився, просто відповідаємо без редагування
                    await update.callback_query.answer("Список актуальний")
            except Exception as e:
                # Якщо помилка "Message is not modified", ігноруємо
                if "Message is not modified" in str(e):
                    await update.callback_query.answer("Список актуальний")
                else:
                    raise e
        else:
            await update.message.reply_text(
                message,
                reply_markup=reply_markup
            )
            
    except Exception as e:
        logger.error(f"❌ Помилка в send_auctions_page: {e}")
        if edit and hasattr(update, 'callback_query'):
            try:
                await update.callback_query.edit_message_text("❌ Помилка при відображенні списку.")
            except:
                pass
        else:
            await update.message.reply_text("❌ Помилка при відображенні списку.")

async def send_remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    """Надсилає сторінку з аукціонами для швидкого видалення"""
    try:
        auctions = context.user_data.get('remove_list', [])
        if not auctions:
            await update.message.reply_text("📭 Список аукціонів порожній.")
            return
        
        ITEMS_PER_PAGE = 5
        total_pages = (len(auctions) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0
        
        start_idx = page * ITEMS_PER_PAGE
        end_idx = min(start_idx + ITEMS_PER_PAGE, len(auctions))
        
        message = f"🗑 Швидке видалення (сторінка {page + 1}/{total_pages}):\n\n"
        message += "Натисніть на аукціон щоб видалити:\n\n"
        
        keyboard = []
        
        for idx, (auction_id, url, title, status, _) in enumerate(auctions[start_idx:end_idx], start_idx + 1):
            status_text = translate_status(status)
            short_title = title[:50] + '...' if len(title) > 50 else title
            
            message += f"{idx}. {auction_id}\n   📄 {short_title}\n   📊 {status_text}\n\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"❌ Видалити {auction_id[:20]}...", 
                    callback_data=f"remove_quick_{auction_id}"
                )
            ])
        
        # Кнопки навігації
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"remove_page_{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"remove_page_{page + 1}"))
        
        keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔄 Оновити", callback_data="refresh_remove")])
        keyboard.append([InlineKeyboardButton("🔙 Головне меню", callback_data="back_to_main")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                message,
                reply_markup=reply_markup
            )
            
    except Exception as e:
        logger.error(f"❌ Помилка в send_remove_page: {e}")
        await update.message.reply_text("❌ Помилка при відображенні.")


async def send_history_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    """Надсилає сторінку з аукціонами для історії"""
    try:
        auctions = context.user_data.get('history_list', [])
        if not auctions:
            await update.message.reply_text("📭 Список аукціонів порожній.")
            return
        
        ITEMS_PER_PAGE = 5
        total_pages = (len(auctions) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0
        
        start_idx = page * ITEMS_PER_PAGE
        end_idx = min(start_idx + ITEMS_PER_PAGE, len(auctions))
        
        message = f"📜 Виберіть аукціон для перегляду історії (сторінка {page + 1}/{total_pages}):\n\n"
        
        keyboard = []
        
        for idx, (auction_id, _, title, status, _) in enumerate(auctions[start_idx:end_idx], start_idx + 1):
            status_text = translate_status(status)
            short_title = title[:50] + '...' if len(title) > 50 else title
            
            message += f"{idx}. `{auction_id}`\n   📄 {short_title}\n   📊 {status_text}\n\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"📜 {auction_id[:20]}...", 
                    callback_data=f"history_show_{auction_id}"
                )
            ])
        
        # Кнопки навігації
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"history_page_{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"history_page_{page + 1}"))
        
        keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔙 Головне меню", callback_data="back_to_main")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            
    except Exception as e:
        logger.error(f"❌ Помилка в send_history_page: {e}")
        await update.message.reply_text("❌ Помилка при відображенні списку.")


async def send_export_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    """Надсилає сторінку з аукціонами для експорту"""
    try:
        auctions = context.user_data.get('export_list', [])
        if not auctions:
            await update.message.reply_text("📭 Список аукціонів для експорту порожній.")
            return
        
        ITEMS_PER_PAGE = 5
        total_pages = (len(auctions) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        
        if page >= total_pages:
            page = total_pages - 1
        if page < 0:
            page = 0
        
        start_idx = page * ITEMS_PER_PAGE
        end_idx = min(start_idx + ITEMS_PER_PAGE, len(auctions))
        
        chat_id = update.effective_chat.id
        
        message = f"📊 Експорт в Excel (сторінка {page + 1}/{total_pages}):\n\n"
        message += "Аукціони з історією змін:\n\n"
        
        keyboard = []
        
        for idx, (auction_id, _, title, status, _) in enumerate(auctions[start_idx:end_idx], start_idx + 1):
            history = get_auction_history(chat_id, auction_id, limit=100)
            changes_count = len(history)
            
            status_text = translate_status(status)
            short_title = title[:40] + '...' if len(title) > 40 else title
            
            message += (
                f"{idx}. `{auction_id}`\n"
                f"   📄 {short_title}\n"
                f"   📊 {status_text}\n"
                f"   🔄 Змін: {changes_count}\n\n"
            )
            
            keyboard.append([
                InlineKeyboardButton(
                    f"📊 {auction_id[:20]}... ({changes_count} змін)", 
                    callback_data=f"export_do_{auction_id}"
                )
            ])
        
        # Кнопки навігації
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"export_page_{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"export_page_{page + 1}"))
        
        keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔙 Головне меню", callback_data="back_to_main")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            
    except Exception as e:
        logger.error(f"❌ Помилка в send_export_page: {e}")
        await update.message.reply_text("❌ Помилка при відображенні списку.")


async def safe_send_message(update, text, reply_markup=None):
    """Безпечна відправка повідомлення з обробкою помилок"""
    try:
        if reply_markup:
            await update.message.reply_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text)
        return True
    except Exception as e:
        print(f"❌ Помилка відправки: {e}")
        try:
            await update.message.reply_text("❌ Виникла помилка при відправці повідомлення.")
        except:
            pass
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    try:
        chat_id = update.effective_chat.id
        
        user = update.effective_user
        add_user(
            chat_id, 
            username=user.username, 
            first_name=user.first_name, 
            last_name=user.last_name
        )
        
        await update.message.reply_text(
            "👋 Привіт!\n\n"
            "Я бот для моніторингу аукціонів Prozorro.Продажі. Умію дві речі:\n\n"
            "📡 <b>Шукати нові лоти за вашими критеріями.</b>\n"
            "Налаштуйте фільтр — регіон, тип торгів, ключові слова, ціна, площа — "
            "і я надсилатиму кожен новий лот, щойно він з'явиться.\n\n"
            "📋 <b>Стежити за конкретними лотами.</b>\n"
            "Додайте лот — сповіщу про зміну статусу чи умов.\n\n"
            "Почніть з кнопки «📡 Мої фільтри».",
            parse_mode="HTML",
            reply_markup=get_main_menu(update.effective_chat.id)
        )
        
    except Exception as e:
        print(f"❌ Помилка в start: {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    from config import DAILY_NOTIFY_LIMIT, MAX_SUBS_PER_USER

    await update.message.reply_text(
        "📖 <b>Допомога</b>\n\n"
        "<b>📡 Мої фільтри — пошук нових лотів</b>\n"
        "Створіть фільтр, і я сам знайду лоти під ваші критерії:\n"
        "• тип торгів (земля, оренда, приватизація…)\n"
        "• регіон\n"
        "• ключові слова та слова-винятки\n"
        "• ціна, площа, організатор, кадастровий номер\n\n"
        f"Фільтрів можна мати до {MAX_SUBS_PER_USER}. "
        f"Ліміт — {DAILY_NOTIFY_LIMIT} лотів на добу на кожен фільтр: "
        "якщо впираєтесь у нього, критерії варто звузити.\n"
        "Фільтр можна тимчасово вимкнути, не видаляючи.\n\n"
        "<b>📋 Мої аукціони — стеження за конкретним лотом</b>\n"
        "Натисніть «➕ Додати аукціон» і надішліть посилання або ID "
        "(<code>LLE001-UA-20260713-73886</code>). "
        "Сповіщу про зміну статусу й дати.\n\n"
        "<b>Команди</b>\n"
        "/filters — мої фільтри\n"
        "/list — мої аукціони\n"
        "/history — історія змін\n"
        "/export — вивантажити історію в Excel\n\n"
        "<b>Основні статуси</b>\n"
        "📝 Подання заявок · 🟡 Виправлення умов · 🔨 Аукціон триває\n"
        "✅ Завершено · ❌ Відмінено",
        parse_mode="HTML",
        reply_markup=get_main_menu(update.effective_chat.id)
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /add - додати аукціон"""
    await update.message.reply_text(
        "📤 Надішли мені посилання або ID аукціону, "
        "який хочеш відстежувати.\n\n"
        "Наприклад:\n"
        "• LLE001-UA-20260713-73886\n"
        "• https://prozorro.sale/auction/UA-...\n"
        "• https://procedure.prozorro.sale/api/procedures/...",
        reply_markup=get_main_menu(update.effective_chat.id)
    )
    
    context.user_data["waiting_for_auction"] = True


async def list_auctions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /list - показати список аукціонів (тільки перегляд)"""
    try:
        chat_id = update.effective_chat.id
        auctions = get_user_auctions(chat_id)
        
        if not auctions:
            await update.message.reply_text(
                "📭 У вас немає аукціонів у відстеженні.\n"
                "Натисніть '➕ Додати аукціон' щоб додати.",
                reply_markup=get_main_menu(update.effective_chat.id)
            )
            return
        
        context.user_data['auctions_list'] = auctions
        context.user_data['current_page'] = 0
        
        # edit=False - нове повідомлення
        await send_auctions_page(update, context, 0, edit=False)
        
    except Exception as e:
        logger.error(f"❌ Помилка в list_auctions: {e}")
        await update.message.reply_text(
            "❌ Помилка при отриманні списку аукціонів.",
            reply_markup=get_main_menu(update.effective_chat.id)
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /history - показати історію змін аукціону з пагінацією"""
    try:
        chat_id = update.effective_chat.id
        auctions = get_user_auctions(chat_id)
        
        if not auctions:
            await update.message.reply_text(
                "📭 У вас немає аукціонів у відстеженні.",
                reply_markup=get_main_menu(update.effective_chat.id)
            )
            return
        
        context.user_data['history_list'] = auctions
        context.user_data['history_page'] = 0
        
        await send_history_page(update, context, 0)
        
    except Exception as e:
        logger.error(f"❌ Помилка в history_command: {e}")
        await update.message.reply_text(
            "❌ Помилка при отриманні історії.",
            reply_markup=get_main_menu(update.effective_chat.id)
        )


async def show_history_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує детальну історію для вибраного аукціону"""
    try:
        query = update.callback_query
        await query.answer()
        
        chat_id = update.effective_chat.id
        data = query.data
        
        if data.startswith("history_show_"):
            auction_id = data.replace("history_show_", "")
            
            history = get_auction_history(chat_id, auction_id, limit=20)
            
            if not history:
                await query.edit_message_text(
                    f"📭 Для аукціону `{auction_id}` ще немає змін.",
                    parse_mode="Markdown"
                )
                return
            
            message = f"📜 Історія змін для `{auction_id}`\n\n"
            
            for idx, (old_status, new_status, old_date, new_date, changed_at) in enumerate(history, 1):
                old_status_text = translate_status(old_status)
                new_status_text = translate_status(new_status)
                
                old_date_f = format_date(old_date)
                new_date_f = format_date(new_date)
                changed_at_f = format_date(changed_at)
                
                message += (
                    f"*#{idx}* {changed_at_f}\n"
                    f"📊 {old_status_text} ➜ {new_status_text}\n"
                    f"🕒 {old_date_f} ➜ {new_date_f}\n\n"
                )
            
            if len(message) > 4000:
                message = message[:4000] + "\n\n... (історія обрізана)"
            
            keyboard = [[InlineKeyboardButton("🔙 Назад до списку", callback_data="history_back")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                message,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            
    except Exception as e:
        logger.error(f"❌ Помилка в show_history_detail: {e}")
        try:
            await query.edit_message_text("❌ Помилка при отриманні історії.")
        except:
            pass


async def history_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повернення до списку історії"""
    try:
        query = update.callback_query
        await query.answer()
        
        await send_history_page(update, context, context.user_data.get('history_page', 0))
        
    except Exception as e:
        logger.error(f"❌ Помилка в history_back: {e}")
        try:
            await query.edit_message_text("❌ Помилка повернення до списку.")
        except:
            pass


async def quick_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /remove_quick - швидке видалення з пагінацією"""
    try:
        chat_id = update.effective_chat.id
        auctions = get_user_auctions(chat_id)
        
        if not auctions:
            await update.message.reply_text(
                "📭 У вас немає аукціонів для видалення.",
                reply_markup=get_main_menu(update.effective_chat.id)
            )
            return
        
        context.user_data['remove_list'] = auctions
        context.user_data['remove_page'] = 0
        
        await send_remove_page(update, context, 0)
        
    except Exception as e:
        logger.error(f"❌ Помилка в quick_remove: {e}")
        await update.message.reply_text(
            "❌ Помилка при видаленні.",
            reply_markup=get_main_menu(update.effective_chat.id)
        )


async def export_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /export - експорт історії в Excel (тільки аукціони зі змінами)"""
    try:
        chat_id = update.effective_chat.id
        
        all_auctions = get_user_auctions(chat_id)
        
        if not all_auctions:
            await update.message.reply_text(
                "📭 У вас немає аукціонів для експорту.",
                reply_markup=get_main_menu(update.effective_chat.id)
            )
            return
        
        # Фільтруємо - залишаємо тільки ті, де є історія змін
        auctions_with_history = []
        for auction_id, url, title, status, date_modified in all_auctions:
            history = get_auction_history(chat_id, auction_id, limit=1)
            if history:
                auctions_with_history.append((auction_id, url, title, status, date_modified))
        
        if not auctions_with_history:
            await update.message.reply_text(
                "📭 Немає аукціонів з історією змін для експорту.\n\n"
                "💡 Історія з'являється після того, як відбудуться зміни в аукціоні.",
                reply_markup=get_main_menu(update.effective_chat.id)
            )
            return
        
        context.user_data['export_list'] = auctions_with_history
        context.user_data['export_page'] = 0
        
        await send_export_page(update, context, 0)
        
    except Exception as e:
        logger.error(f"❌ Помилка в export_history: {e}")
        await update.message.reply_text(
            "❌ Помилка при експорті.",
            reply_markup=get_main_menu(update.effective_chat.id)
        )


async def export_do_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник натискання кнопки експорту"""
    try:
        query = update.callback_query
        await query.answer()
        
        chat_id = update.effective_chat.id
        data = query.data
        
        if data.startswith("export_do_"):
            auction_id = data.replace("export_do_", "")
            
            await query.edit_message_text("⏳ Створюю Excel файл...")
            
            history = get_full_auction_history(chat_id, auction_id)
            details = get_auction_details(chat_id, auction_id)
            
            if not history:
                await query.edit_message_text(
                    f"📭 Для аукціону `{auction_id}` ще немає змін.",
                    parse_mode="Markdown"
                )
                return
            
            if not details:
                await query.edit_message_text(
                    f"❌ Аукціон `{auction_id}` не знайдено.",
                    parse_mode="Markdown"
                )
                return
            
            filepath, filename = export_history_to_excel(chat_id, auction_id, history, details)
            
            await context.bot.send_document(
                chat_id=chat_id,
                document=open(filepath, 'rb'),
                filename=filename,
                caption=f"📊 Історія змін для аукціону {auction_id}\n\n"
                        f"📄 Всього змін: {len(history)}\n"
                        f"📅 Експорт створено: {format_date(datetime.now().isoformat())}"
            )
            
            os.remove(filepath)
            
            await query.edit_message_text(
                "✅ Експорт завершено! Файл надіслано."
            )
            
            cleanup_old_exports()
            
    except Exception as e:
        logger.error(f"❌ Помилка в export_do_callback: {e}")
        try:
            await query.edit_message_text(f"❌ Помилка: {str(e)}")
        except:
            pass


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник натискань кнопок"""
    try:
        query = update.callback_query
        await query.answer()
        
        chat_id = update.effective_chat.id
        data = query.data

        # Підписки, майстер фільтрів і кнопка «стежити за лотом»
        if data.startswith("s_"):
            await handle_sub_callback(update, context, data)
            return

        if data.startswith("w_"):
            await handle_wizard_callback(update, context, data)
            return

        if data.startswith("track_"):
            await handle_track_callback(update, context, data)
            return

        if data.startswith("b_"):
            await handle_bids_callback(update, context, data)
            return

        # Обробка пагінації списку
        if data.startswith("list_page_"):
            try:
                page = int(data.replace("list_page_", ""))
                context.user_data['current_page'] = page
                await send_auctions_page(update, context, page, edit=True)
            except Exception as e:
                logger.error(f"❌ Помилка зміни сторінки: {e}")
                await query.edit_message_text("❌ Помилка при зміні сторінки.")
            return
        
        # Обробка пагінації видалення
        if data.startswith("remove_page_"):
            try:
                page = int(data.replace("remove_page_", ""))
                context.user_data['remove_page'] = page
                await send_remove_page(update, context, page)
            except Exception as e:
                logger.error(f"❌ Помилка зміни сторінки: {e}")
                await query.edit_message_text("❌ Помилка при зміні сторінки.")
            return
        
        # Обробка пагінації історії
        if data.startswith("history_page_"):
            try:
                page = int(data.replace("history_page_", ""))
                context.user_data['history_page'] = page
                await send_history_page(update, context, page)
            except Exception as e:
                logger.error(f"❌ Помилка зміни сторінки історії: {e}")
                await query.edit_message_text("❌ Помилка при зміні сторінки.")
            return
        
        # Обробка пагінації експорту
        if data.startswith("export_page_"):
            try:
                page = int(data.replace("export_page_", ""))
                context.user_data['export_page'] = page
                await send_export_page(update, context, page)
            except Exception as e:
                logger.error(f"❌ Помилка зміни сторінки експорту: {e}")
                await query.edit_message_text("❌ Помилка при зміні сторінки.")
            return
        
        if data == "noop":
            await query.answer("Ви на поточній сторінці")
            return
        
        if data == "back_to_main":
            await query.edit_message_text(
                "👋 Головне меню:",
                reply_markup=None
            )
            await query.message.reply_text(
                "Виберіть дію:",
                reply_markup=get_main_menu(update.effective_chat.id)
            )
            return
        
        # ОНОВЛЕННЯ СПИСКУ - ВИПРАВЛЕНО
        if data == "refresh_list":
            try:
                auctions = get_user_auctions(chat_id)
                if auctions:
                    context.user_data['auctions_list'] = auctions
                    await send_auctions_page(update, context, 0, edit=True)
                else:
                    await query.edit_message_text("📭 У вас немає аукціонів у відстеженні.")
                    await query.message.reply_text(
                        "Виберіть дію:",
                        reply_markup=get_main_menu(update.effective_chat.id)
                    )
            except Exception as e:
                logger.error(f"❌ Помилка оновлення списку: {e}")
                if "Message is not modified" not in str(e):
                    await query.edit_message_text("❌ Помилка при оновленні списку.")
            return
        
        if data == "refresh_remove":
            auctions = get_user_auctions(chat_id)
            if auctions:
                context.user_data['remove_list'] = auctions
                await send_remove_page(update, context, 0)
            return
        
        # Показати історію аукціону
        if data.startswith("history_show_"):
            await show_history_detail(update, context)
            return
        
        # Повернення до списку історії
        if data == "history_back":
            await history_back(update, context)
            return
        
        # Швидке видалення
        if data.startswith("remove_quick_"):
            auction_id = data.replace("remove_quick_", "")
            remove_user_auction(chat_id, auction_id)
            
            auctions = get_user_auctions(chat_id)
            context.user_data['remove_list'] = auctions
            
            if not auctions:
                await query.edit_message_text("✅ Всі аукціони видалено!")
                await query.message.reply_text(
                    "Виберіть дію:",
                    reply_markup=get_main_menu(update.effective_chat.id)
                )
                return
            
            current_page = context.user_data.get('remove_page', 0)
            await send_remove_page(update, context, current_page)
            return
        
        # Експорт
        if data.startswith("export_do_"):
            await export_do_callback_handler(update, context)
            return
            
    except Exception as e:
        print(f"❌ Помилка в button_handler: {e}")
        try:
            await query.edit_message_text("❌ Помилка при обробці запиту.")
        except:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка текстових повідомлень"""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    MENU_BUTTONS = (
        "📡 Мої фільтри", "📋 Мої аукціони", "➕ Додати аукціон", "📜 Історія змін",
        "⚡ Швидке видалення", "📊 Експорт в Excel", "📖 Допомога",
        "🎯 Заявки на лотах", "📈 Стан моніторингу",
    )

    # Натиснута кнопка меню під час роботи майстра — виходимо з майстра
    if text in MENU_BUTTONS and get_state(context):
        clear_wizard(context)

    # Текст належить майстру фільтрів
    if text not in MENU_BUTTONS and get_state(context):
        if await handle_wizard_text(update, context, text):
            return

    # Обробка кнопок головного меню
    if text == "📡 Мої фільтри":
        await filters_command(update, context)
        return

    if text == "🎯 Заявки на лотах":
        await bids_command(update, context)
        return

    if text == "📈 Стан моніторингу":
        await feedstat_command(update, context)
        return

    if text == "📋 Мої аукціони":
        await list_auctions(update, context)
        return

    if text == "➕ Додати аукціон":
        await add_command(update, context)
        return
    
    if text == "📜 Історія змін":
        await history_command(update, context)
        return
    
    if text == "⚡ Швидке видалення":
        await quick_remove_command(update, context)
        return
    
    if text == "📊 Експорт в Excel":
        await export_history_command(update, context)
        return
    
    if text == "📖 Допомога":
        await help_command(update, context)
        return
    
    if text.startswith("/"):
        return
    
    # Обробка введення аукціону
    try:
        if text.startswith("https://procedure.prozorro.sale/api/procedures/"):
            url = text
        else:
            auction_id = extract_auction_id(text)
            
            if auction_id is None:
                await update.message.reply_text(
                    "❌ Не вдалося знайти ID аукціону.\n"
                    "Перевірте правильність посилання або ID.\n\n"
                    "Приклади правильних форматів:\n"
                    "• LLE001-UA-20260713-73886\n"
                    "• https://prozorro.sale/auction/UA-...\n"
                    "• https://procedure.prozorro.sale/api/procedures/...",
                    reply_markup=get_main_menu(update.effective_chat.id)
                )
                return
            
            url = auction_id_to_api_url(auction_id)
        
        # Отримуємо інформацію про аукціон
        info = get_auction_info(url)
        
        # ПЕРЕВІРКА НА ДУБЛІКАТ
        # Отримуємо всі аукціони користувача
        user_auctions = get_user_auctions(chat_id)
        
        # Перевіряємо чи вже є такий аукціон
        for existing_auction_id, _, _, _, _ in user_auctions:
            if existing_auction_id == info['auction_id']:
                # Форматуємо дату для повідомлення
                date_f = format_date(info['date_modified'])
                status_text = translate_status(info['status'])
                
                await update.message.reply_text(
                    f"⚠️ **Аукціон вже додано!**\n\n"
                    f"🆔 ID: `{info['auction_id']}`\n"
                    f"📌 Статус: {status_text}\n"
                    f"📄 Назва: {info['title']}\n"
                    f"🕒 Оновлено: {date_f}\n\n"
                    f"💡 Використовуйте `/list` для перегляду всіх аукціонів.",
                    parse_mode="Markdown",
                    reply_markup=get_main_menu(update.effective_chat.id)
                )
                return
        
        # Якщо аукціону немає - додаємо
        add_user_auction(chat_id, info, url)
        
        context.user_data["waiting_for_auction"] = False
        
        date_f = format_date(info['date_modified'])
        status_text = translate_status(info['status'])
        
        await update.message.reply_text(
            f"✅ **Аукціон додано!**\n\n"
            f"🆔 ID: `{info['auction_id']}`\n"
            f"📌 Статус: {status_text}\n"
            f"📄 Назва: {info['title']}\n"
            f"🕒 Оновлено: {date_f}\n\n"
            f"Використовуйте кнопки для керування:",
            parse_mode="Markdown",
            reply_markup=get_main_menu(update.effective_chat.id)
        )
        
    except Exception as e:
        print(f"❌ Помилка в handle_message: {e}")
        await update.message.reply_text(
            f"❌ Помилка:\n{str(e)}",
            reply_markup=get_main_menu(update.effective_chat.id)
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats - статистика бота (тільки для адмінів)"""
    try:
        chat_id = update.effective_chat.id
        
        if chat_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ У вас немає доступу до цієї команди.")
            return
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM user_auctions")
        auctions_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM auction_history")
        changes_count = cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT chat_id, COUNT(*) 
            FROM user_auctions 
            GROUP BY chat_id 
            ORDER BY COUNT(*) DESC 
            LIMIT 5
        """)
        top_users = cursor.fetchall()
        
        conn.close()
        
        message = (
            f"📊 **Статистика бота**\n\n"
            f"👥 Користувачів: `{users_count}`\n"
            f"📋 Аукціонів: `{auctions_count}`\n"
            f"🔄 Змін: `{changes_count}`\n"
            f"⏱ Інтервал: `{CHECK_INTERVAL}с`\n\n"
            f"🏆 **Топ користувачів:**\n"
        )
        
        for i, (chat_id, count) in enumerate(top_users, 1):
            message += f"  {i}. ID `{chat_id}` - {count} аукціонів\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Помилка в stats: {e}")
        await update.message.reply_text("❌ Помилка отримання статистики")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /users - список користувачів (тільки для адмінів)"""
    try:
        chat_id = update.effective_chat.id
        
        if chat_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ У вас немає доступу до цієї команди.")
            return
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT chat_id, username, first_name, registered_at
            FROM users
            ORDER BY registered_at DESC
            LIMIT 20
        """)
        
        users = cursor.fetchall()
        conn.close()
        
        if not users:
            await update.message.reply_text("📭 Немає зареєстрованих користувачів.")
            return
        
        message = "👥 **Останні користувачі:**\n\n"
        for chat_id, username, first_name, registered_at in users:
            name = first_name or username or str(chat_id)
            date_f = format_date(registered_at)
            message += f"• {name} (ID: `{chat_id}`) - {date_f}\n"
        
        await update.message.reply_text(message, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Помилка в users: {e}")
        await update.message.reply_text("❌ Помилка отримання списку користувачів")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /broadcast - розсилка повідомлень (тільки для адмінів)"""
    try:
        chat_id = update.effective_chat.id
        
        if chat_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ У вас немає доступу до цієї команди.")
            return
        
        args = context.args
        if not args:
            await update.message.reply_text(
                "📝 **Як використовувати:**\n"
                "/broadcast Текст повідомлення\n\n"
                "Наприклад:\n"
                "/broadcast Важливе оголошення для всіх користувачів!"
            )
            return
        
        message_text = " ".join(args)
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users")
        users = cursor.fetchall()
        conn.close()
        
        if not users:
            await update.message.reply_text("📭 Немає користувачів для розсилки.")
            return
        
        sent = 0
        failed = 0
        
        for (user_id,) in users:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 **Оголошення від адміністратора:**\n\n{message_text}",
                    parse_mode="Markdown"
                )
                sent += 1
            except Exception as e:
                failed += 1
                logger.error(f"Помилка відправки {user_id}: {e}")
        
        await update.message.reply_text(
            f"✅ **Розсилку завершено!**\n\n"
            f"📨 Надіслано: `{sent}`\n"
            f"❌ Помилок: `{failed}`"
        )
        
    except Exception as e:
        logger.error(f"Помилка в broadcast: {e}")
        await update.message.reply_text("❌ Помилка розсилки")


async def force_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /force_check - примусова перевірка (тільки для адмінів)"""
    try:
        chat_id = update.effective_chat.id
        
        if chat_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ У вас немає доступу до цієї команди.")
            return
        
        await update.message.reply_text("🔄 Запускаю примусову перевірку...")
        
        await check_auctions(context)
        
        await update.message.reply_text("✅ Перевірку завершено!")
        
    except Exception as e:
        logger.error(f"Помилка в force_check: {e}")
        await update.message.reply_text("❌ Помилка при перевірці")


async def scheduled_backup(context):
    """Плановий бек-ап бази даних"""
    try:
        backup_database()
        logger.info("✅ Плановий бек-ап створено")
    except Exception as e:
        logger.error(f"❌ Помилка планового бек-апу: {e}")


def main():
    logger.info("🚀 Запуск бота...")
    
    app = ApplicationBuilder() \
        .token(TOKEN) \
        .connect_timeout(30.0) \
        .read_timeout(30.0) \
        .write_timeout(30.0) \
        .build()
    
    # Створюємо бек-ап при запуску
    try:
        backup_database()
        logger.info("✅ Бек-ап створено")
    except Exception as e:
        logger.error(f"❌ Помилка створення бек-апу: {e}")
    
    # Додаємо моніторинг
    app.job_queue.run_repeating(
        check_auctions,
        interval=CHECK_INTERVAL,
        first=10,
    )
    
    # Додаємо плановий бек-ап (кожні 24 години)
    app.job_queue.run_repeating(
        scheduled_backup,
        interval=86400,
        first=3600,
    )

    # Разове наповнення кешу історією — щоб ретроспектива мала на чому працювати
    app.job_queue.run_once(bootstrap_job, when=20, name="bootstrap_history")

    # Перенос уже накопиченої історії змін у лічильник заявок
    app.job_queue.run_once(import_bid_history_job, when=10, name="import_bid_history")

    # Пошук нових лотів за підписками користувачів
    app.job_queue.run_repeating(
        watch_new_lots,
        interval=FEED_INTERVAL,
        first=15,
        name="watch_new_lots",
    )

    # Лічильник заявок по лотах адмінів
    app.job_queue.run_repeating(
        poll_bid_watch,
        interval=BID_WATCH_INTERVAL,
        first=45,
        name="poll_bid_watch",
    )

    # Підсумок перед закриттям подання заявок
    app.job_queue.run_repeating(
        bid_deadline_summary,
        interval=600,
        first=120,
        name="bid_deadline_summary",
    )

    # Чистка застарілого кешу процедур
    app.job_queue.run_repeating(
        daily_cache_cleanup,
        interval=86400,
        first=7200,
        name="cache_cleanup",
    )

    # Команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("list", list_auctions))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("remove_quick", quick_remove_command))
    app.add_handler(CommandHandler("export", export_history_command))
    app.add_handler(CommandHandler("filters", filters_command))

    # Адмін команди
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("force_check", force_check_command))
    app.add_handler(CommandHandler("bids", bids_command))
    app.add_handler(CommandHandler("feedstat", feedstat_command))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    
    print("✅ Бот запущено! Моніторинг активовано для всіх користувачів.")
    print("📋 Доступні команди: /start, /add, /list, /history, /remove_quick, /export, /help")
    print("👑 Адмін команди: /stats, /users, /broadcast, /force_check")
    print("💡 Також доступні кнопки в інтерфейсі Telegram")
    
    app.run_polling()


if __name__ == "__main__":
    main()