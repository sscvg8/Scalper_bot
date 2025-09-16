import os
import json
import ccxt
import time
import logging
import threading
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from datetime import datetime
import sys
import io
import sqlite3
from calendar import monthrange
import fetch_deposits
import psutil
import dotenv
from dotenv import load_dotenv

load_dotenv()

# Исправление кодировки для консоли Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


# Создание БД
def init_profit_db():
    conn = sqlite3.connect('profits.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS profits
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  profit REAL NOT NULL,
                  timestamp REAL NOT NULL,
                  symbol TEXT NOT NULL,
                  buy_price REAL NOT NULL,
                  sell_price REAL NOT NULL)''')

    # Добавляем индексы
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON profits (user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON profits (timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON profits (symbol)")

    conn.commit()
    conn.close()


# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
SETTINGS_PATH = "trading_bot_settings.json"
LOG_FILE = "bot_errors.log"
PRICE_UPDATE_INTERVAL = 10  # секунд
ADMINS_ID = [2044576483, 6060803148]
start_time = time.time()

WALLETS = ["0x45c68833dd040FfacCC009bB811299bF50380fC8",
           "0x15001369896D53cd69139705C14028343f2ea1af",
           "0x28F31a6bb7A6De1F17F24e57Bb0Fcc6C8993E10b",
           "0x1A20Fde6451bd02d9dc685bb45545ae9F422b37d",
           "0xEE9EAD813B4d6cB655d5045d8e7106FB2D8038aE"]
active_wallets = []  # Кошельки в обработке
WALLET_RESERVE_TIME = 3600  # 60 минут в секундах
wallets_lock = threading.Lock()
last_wallet_index = -1
sent_notifications = set()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('TRADING_BOT')

# Настройки по умолчанию для пользователя
DEFAULT_SETTINGS = {
    'symbol': 'BTC/USDT',
    'api_key': '',
    'api_secret': '',
    'fall_percent': 1.0,
    'rise_percent': 1.5,
    'cooldown': 60,
    'amount': 10.0,
    'orders_limit': 0,
    'subscription_price': 30,
    'subscription_end': 0,
    'enabled': False,
    'sub': 0
}

# Глобальные переменные
settings = {}
user_states = {}
user_threads = {}
price_cache = {}
price_cache_lock = threading.Lock()
exchange_instances = {}

# Инициализация Telegram бота
bot = telebot.TeleBot(BOT_TOKEN)


#############################################################################
# Функции для работы с настройками
#############################################################################

def load_settings():
    global settings
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, 'r') as f:
                settings = json.load(f)
        else:
            settings = {'users': {}}
            save_settings()
    except Exception as e:
        logger.error(f"Ошибка загрузки настроек: {e}")
        settings = {'users': {}}


def save_settings():
    try:
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")


def get_user_settings(user_id):
    user_id_str = str(user_id)
    if user_id_str not in settings['users']:
        settings['users'][user_id_str] = DEFAULT_SETTINGS.copy()
        settings['users'][user_id_str]['subscription_end'] = time.time() + 172800
        print(settings['users'][user_id_str])
        save_settings()
    return settings['users'][user_id_str]


def update_user_settings(user_id, new_settings):
    user_id_str = str(user_id)
    settings['users'][user_id_str] = new_settings
    save_settings()


def extend_subscription(user_id, seconds=0):
    """Изменяет дату окончания подписки пользователя на указанное количество секунд или дней"""
    try:
        user_settings = get_user_settings(user_id)
        current_time = time.time()
        if current_time > user_settings['subscription_end']:
            # Если подписка истекла, начинаем отсчет с текущего момента
            new_end = current_time + seconds
        else:
            # Если подписка активна, изменяем от текущей даты окончания
            new_end = user_settings['subscription_end'] + seconds

        user_settings['subscription_end'] = new_end
        update_user_settings(user_id, user_settings)

        global sent_notifications
        sent_notifications = {item for item in sent_notifications if item[0] != user_id}

        return new_end
    except Exception as e:
        logger.error(f"Ошибка изменения подписки для {user_id}: {e}")
        raise


def get_available_wallet():
    global last_wallet_index
    with wallets_lock:
        current_time = time.time()
        # Удаляем только устаревшие кошельки (независимо от статуса проверки)
        active_wallets[:] = [
            w for w in active_wallets
            if current_time - w['reserved_at'] < WALLET_RESERVE_TIME
        ]

        # Создаем список всех активных адресов
        occupied_addresses = [w['address'] for w in active_wallets]

        # Поиск свободного кошелька
        start_index = (last_wallet_index + 1) % len(WALLETS)
        for i in range(len(WALLETS)):
            current_index = (start_index + i) % len(WALLETS)
            candidate = WALLETS[current_index]
            if candidate not in occupied_addresses:
                last_wallet_index = current_index
                return candidate
        return None


#############################################################################
# Система обновления цен
#############################################################################

def price_updater():
    """Фоновый поток для обновления цен"""
    logger.info("Запуск системы обновления цен")
    last_cleanup = time.time()
    while True:
        try:
            # Собираем уникальные символы из всех пользовательских настроек
            symbols = set()
            for user_id, user_settings in settings['users'].items():
                if user_settings.get('enabled', False):
                    symbols.add(user_settings['symbol'])

            # Обновляем цены для каждого символа
            for symbol in symbols:
                try:
                    # Создаем временный экземпляр биржи
                    API_KEY = os.getenv("API_TICKER_UPDATER")
                    API_SECRET = os.getenv("API_TICKER_UPDATER_SECRET")
                    temp_exchange = ccxt.mexc({
                        'apiKey': API_KEY,
                        'secret': API_SECRET,
                        'enableRateLimit': True,
                    })
                    ticker = temp_exchange.fetch_ticker(symbol)

                    with price_cache_lock:
                        price_cache[symbol] = {
                            'price': ticker['last'],
                            'timestamp': time.time()
                        }

                    logger.debug(f"Обновлена цена для {symbol}: {ticker['last']}")
                except Exception as e:
                    logger.error(f"Ошибка обновления цены для {symbol}: {e}")

                    # Очистка каждые 10 минут
            if time.time() - last_cleanup > 600:
                with price_cache_lock:
                    current_time = time.time()
                    # Создаем копию ключей для безопасной итерации
                    for symbol in list(price_cache.keys()):
                        data = price_cache[symbol]
                        if current_time - data['timestamp'] > 7200:
                            del price_cache[symbol]
                            logger.debug(f"Удалён устаревший символ: {symbol}")
                last_cleanup = time.time()

            # Пауза между обновлениями
            time.sleep(PRICE_UPDATE_INTERVAL)

        except Exception as e:
            logger.error(f"Ошибка в потоке обновления цен: {e}")
            time.sleep(30)


def get_cached_price(symbol):
    """Получение кэшированной цены для символа"""
    with price_cache_lock:
        if symbol in price_cache:
            # Проверяем, не устарели ли данные
            if time.time() - price_cache[symbol]['timestamp'] < PRICE_UPDATE_INTERVAL * 2:
                return price_cache[symbol]['price']

    # Если данных нет или они устарели, делаем прямой запрос
    try:
        temp_exchange = ccxt.mexc()
        ticker = temp_exchange.fetch_ticker(symbol)
        price = ticker['last']

        with price_cache_lock:
            price_cache[symbol] = {
                'price': price,
                'timestamp': time.time()
            }

        return price
    except Exception as e:
        logger.error(f"Ошибка получения цены для {symbol}: {e}")
        return None


#############################################################################
# Торговая логика
#############################################################################
# Сохранение информации о сделке
def record_profit(user_id, profit, symbol, buy_price, sell_price):
    conn = sqlite3.connect('profits.db')
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO profits (user_id, profit, timestamp, symbol, buy_price, sell_price) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, profit, time.time(), symbol, buy_price, sell_price))
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка записи прибыли: {e}")
    finally:
        conn.close()


def user_trading_bot(user_id):
    """Основной торговый цикл для пользователя"""
    logger.info(f"Запуск торгового бота для пользователя {user_id}")

    user_settings = get_user_settings(user_id)
    user_id_str = str(user_id)
    if time.time() > user_settings['subscription_end']:
        bot.send_message(user_id, "❌ Ваша подписка истекла! Бот не может быть запущен.")
        return

    # Создаем экземпляр биржи для пользователя
    try:
        exchange = ccxt.mexc({
            'apiKey': user_settings['api_key'],
            'secret': user_settings['api_secret'],
            'enableRateLimit': True,
            'options': {'recvWindow': 60000}
        })
        exchange_instances[user_id_str] = exchange
    except Exception as e:
        error_msg = f"Ошибка создания экземпляра биржи: {e}"
        logger.error(error_msg)
        bot.send_message(user_id, error_msg)
        return

    # Переменные состояния для пользователя
    active_orders = []
    last_buy_price = None
    last_subscription_check = time.time()  # Время последней проверки подписки
    SUBSCRIPTION_CHECK_INTERVAL = 60  # Проверять подписку каждые 60 секунд
    last_user_msg = ''

    def user_log(text):
        """Логирование для конкретного пользователя"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_text = f"{timestamp} - {text}"

            # Всегда отправляем логи в Telegram
            try:
                bot.send_message(user_id, log_text)
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")

            logger.info(f"[USER {user_id}] {text}")
        except Exception as e:
            logger.error(f"Ошибка логирования для {user_id}: {e}")

    user_log("Торговый бот запущен")

    try:
        while user_settings.get('enabled', False):
            try:
                current_time = time.time()
                if current_time - last_subscription_check > SUBSCRIPTION_CHECK_INTERVAL:
                    # Обновляем настройки для получения актуальных данных
                    user_settings = get_user_settings(user_id)
                    last_subscription_check = current_time

                    # Проверяем, не истекла ли подписка
                    if current_time > user_settings['subscription_end']:
                        user_log("❌ Ваша подписка истекла! Бот остановлен.")
                        # Отключаем бота
                        user_settings['enabled'] = False
                        update_user_settings(user_id, user_settings)
                        break  # Немедленный выход из цикла
                # Проверка активных ордеров
                for order in active_orders.copy():
                    try:
                        order_info = exchange.fetch_order(order['id'], user_settings['symbol'])

                        if order_info is None:
                            logger.error(f"Ошибка: не получена информация об ордере {order['id']}")
                            if time.time() - order['timestamp'] > 600:
                                active_orders.remove(order)
                            continue

                            # Проверка типа данных
                        if not isinstance(order_info, dict):
                            logger.error(f"Некорректный формат ордера: {type(order_info)}")
                            continue

                        if order_info['status'] == 'closed':

                            # Расчет прибыли
                            try:
                                buy_price = float(order['buy_price'])
                                sell_price = float(order_info.get('price'))
                                buy_fee = float(order.get('buy_fee') or 0)
                                sell_fee = float(order_info.get('fee') or 0)
                                amount = float(order_info.get('amount') or 0)

                                if sell_price <= 0 or amount <= 0:
                                    logger.error(
                                        f"Некорректные данные для расчета прибыли: sell_price={sell_price}, amount={amount}")
                                    active_orders.remove(order)
                                    continue

                                profit = round((sell_price - buy_price) * amount - buy_fee - sell_fee, 6)
                            except (ValueError, TypeError) as e:
                                logger.error(f"Ошибка расчета прибыли: {e}, данные ордера: {order_info}")
                                active_orders.remove(order)
                                continue

                            record_profit(user_id, profit, user_settings['symbol'], buy_price,
                                          float(order_info['price']))

                            active_orders.remove(order)

                            # Пауза перед следующей покупкой
                            if float(user_settings['cooldown']) > 0:
                                time.sleep(float(user_settings['cooldown']))

                            last_buy_price = None

                            user_log(f"Ордер {order_info['id']} исполнен по цене {order_info['price']}\n"
                                     f"Прибыль: {profit:.6f} USDT\n")
                            last_user_msg = ''


                        elif order_info['status'] == 'canceled':
                            user_log(f"Ордер {order['id']} отменен")
                            active_orders.remove(order)
                            last_user_msg = ''

                    except ccxt.OrderNotFound:
                        user_log(f"Ордер {order['id']} не найден, удаление")
                        last_user_msg = ''
                        active_orders.remove(order)
                        continue
                    except ccxt.RateLimitExceeded:
                        logger.error("Превышен лимит запросов, пауза 60 секунд")
                        time.sleep(60)
                    except ccxt.NetworkError as e:
                        logger.error(f"Сетевая ошибка при проверке ордера: {e}")
                        continue
                    except Exception as e:
                        logger.error(f"Ошибка при выполнении операции: {e}")

                # Получаем текущую цену
                try:
                    current_price = get_cached_price(user_settings['symbol'])
                    if current_price is None:
                        logger.error("Не удалось получить текущую цену, пропускаем цикл")
                        time.sleep(10)
                        continue
                except Exception as e:
                    logger.error(f"Ошибка получения цены: {e}")
                    time.sleep(30)
                    continue

                should_buy = True
                # Проверка условий для покупки
                if last_buy_price is None:
                    should_buy = True
                else:
                    if last_buy_price <= 0:
                        logger.error(f"Некорректное значение last_buy_price: {last_buy_price}")
                    else:
                        price_drop = (last_buy_price - current_price) / last_buy_price * 100
                        should_buy = price_drop >= user_settings['fall_percent']

                if should_buy:
                    if len(active_orders) <= user_settings['orders_limit'] or user_settings['orders_limit'] == 0:
                        # Выполнение покупки
                        if current_price <= 0:
                            logger.error(f"Некорректная текущая цена: {current_price}")
                            continue

                        # Проверяем доступный баланс USDT перед покупкой
                        try:
                            balance = exchange.fetch_balance()
                            available_balance = balance['USDT']['free']
                        except Exception as e:
                            logger.error(f"Ошибка получения баланса: {e}")
                            available_balance = 0

                        if available_balance < float(user_settings['amount']):
                            if last_user_msg != "Недостаточно средств для операции":
                                user_log("Недостаточно средств для операции")
                                last_user_msg = "Недостаточно средств для операции"
                            time.sleep(5)
                            continue

                        amount = float(user_settings['amount']) / current_price

                        try:
                            # Рыночная покупка
                            buy_order = exchange.create_market_buy_order(
                                user_settings['symbol'],
                                amount
                            )
                            buy_order_info = exchange.fetch_order(buy_order['id'], user_settings['symbol'])

                            # Проверяем наличие необходимых данных
                            if buy_order_info.get('average') is None or buy_order_info.get('amount') is None:
                                logger.error(f"Ошибка: buy_order_info содержит None значения: {buy_order_info}")
                                continue

                            last_buy_price = float(buy_order_info['average'])

                            # Лимитная продажа
                            sell_price = last_buy_price * (1 + float(user_settings['rise_percent']) / 100)
                            sell_order = exchange.create_limit_sell_order(
                                user_settings['symbol'],
                                float(buy_order_info['amount']),
                                sell_price
                            )

                            user_log(f"Куплено {amount:.6f} {user_settings['symbol']} по {current_price:.6f}\n"
                                     f"Выставлен ордер на продажу по {sell_price:.6f}")
                            last_user_msg = ''

                            active_orders.append({
                                'id': sell_order['id'],
                                'amount': float(sell_order['amount']) if sell_order.get('amount') else 0.0,
                                'sell_price': sell_price,
                                'timestamp': time.time(),
                                'buy_price': float(buy_order_info['average']),
                                'buy_fee': float(buy_order_info.get('fee') or 0),
                            })

                        except ccxt.InsufficientFunds:
                            user_log("Недостаточно средств для операции")
                            last_user_msg = ''
                    else:
                        continue

                time.sleep(2)

            except Exception as e:
                logger.error(f"Ошибка в торговом цикле: {e}")
                time.sleep(30)

    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        user_log("Торговый бот остановлен")
        user_settings = get_user_settings(user_id)
        # Обновляем статус пользователя
        user_settings['enabled'] = False
        update_user_settings(user_id, user_settings)


#############################################################################
# Обработчики команд Telegram
#############################################################################

def make_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, row_width=2)
    markup.add(KeyboardButton('Купить подписку'), KeyboardButton('Поддержка'))
    return markup


def payment_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton('✅ Оплатил'), KeyboardButton('❌ Отмена'))
    return markup


@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    get_user_settings(user_id)  # Создаем настройки, если их нет
    user_settings = get_user_settings(user_id)
    subscription_end = datetime.fromtimestamp(user_settings['subscription_end'])
    formatted_date = subscription_end.strftime('%d.%m.%Y %H:%M:%S')

    if user_settings['sub'] == 0:
        bot.send_message(
            message.chat.id,
            f"🤖 Добро пожаловать в торгового бота!\n\n"
            f"Ваш ID: {user_id}\n"
            "Вам начислено 48 часов подписки,\n"
            f"конец подписки: {formatted_date}\n\n"
            "Используйте /help для списка команд",
            reply_markup=make_keyboard())
        user_settings['sub'] = 1
    else:
        bot.send_message(
            message.chat.id,
            f"🤖 Добро пожаловать в торгового бота!\n\n"
            f"Ваш ID: {user_id}\n\n"
            "Используйте /help для списка команд",
            reply_markup=make_keyboard())


@bot.message_handler(commands=['subscription'])
def show_subscription_info(message):
    user_id = message.from_user.id
    user_settings = get_user_settings(user_id)

    # Получаем текущее время
    current_time = time.time()
    subscription_end = user_settings['subscription_end']

    # Рассчитываем оставшееся время
    time_left = subscription_end - current_time

    # Форматируем даты
    current_date = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
    end_date = datetime.fromtimestamp(subscription_end).strftime('%d.%m.%Y %H:%M:%S')

    # Определяем статус подписки
    if time_left > 0:
        status = "🟢 АКТИВНА"
        # Рассчитываем дни/часы/минуты
        days = int(time_left // (24 * 3600))
        hours = int((time_left % (24 * 3600)) // 3600)
        minutes = int((time_left % 3600) // 60)
        time_left_str = f"{days} дн. {hours} ч. {minutes} мин."
    else:
        status = "🔴 ИСТЕКЛА"
        time_left_str = "0 дн. 0 ч. 0 мин."

    # Формируем ответ
    response = (
        f"📅 <b>ИНФОРМАЦИЯ О ПОДПИСКЕ</b>\n\n"
        f"• <b>Текущее время:</b> {current_date}\n"
        f"• <b>Статус:</b> {status}\n"
        f"• <b>Окончание подписки:</b> {end_date}\n"
        f"• <b>Осталось:</b> {time_left_str}\n"
        f"• <b>Стоимость продления:</b> {user_settings['subscription_price']} USDT"
    )

    # Отправляем сообщение
    bot.send_message(
        message.chat.id,
        response,
        parse_mode='HTML',
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['help'])
def handle_help(message):
    help_text = (
        "📋 <b>Доступные команды:</b>\n\n"
        "/instruction - инструкция по использованию бота\n"
        "/subscription - информация о подписке\n\n"
        "⚙️ <b>Настройки:</b>\n"
        "/set_symbol - Установить торговую пару\n"
        "/set_amount - Установить сумму покупки\n"
        "/set_api_key - Установить API Key\n"
        "/set_api_secret - Установить API Secret\n"
        "/set_fall_percent - Установить процент падения\n"
        "/set_rise_percent - Установить процент роста\n"
        "/set_cooldown - Установить время ожидания\n"
        "/set_orders_limit - Установить ограничение на количество ордеров\n"
        "/view_settings - Показать ваши настройки\n\n"
        "🚀 <b>Управление ботом:</b>\n"
        "/start_bot - Запустить торгового бота\n"
        "/stop_bot - Остановить торгового бота\n"
        "/get_profit - Показать прибыль за месяц\n\n"
        "ℹ️ <b>Прочее:</b>\n"
        "/status - Показать статус бота"
    )
    if message.chat.id in ADMINS_ID:
        help_text += (
            "\n\n\n👑 <b>Команды администратора:</b>\n\n"
            "/admin_broadcast - Рассылка сообщений пользователям\n\n"
            "👤 <b>Управление пользователями:</b>\n"
            "/admin_users - Список пользователей\n"
            "/admin_user_info [user_id] - Сведения о пользователе\n"
            "/admin_edit_user [user_id] [параметр] [значение] - Изменить настройку\n"
            "/admin_add_subscription [user_id] [секунды] - Изменить подписку\n\n"
            "⚙️ <b>Система:</b>\n"
            "/get_logs - Скачать файл логов\n"
            "/admin_status - Статус системы")
    bot.send_message(message.chat.id, help_text, reply_markup=make_keyboard(), parse_mode='HTML')


@bot.message_handler(commands=['instruction'])
def send_instruction(message):
    instruction_text = """
📘 ИНСТРУКЦИЯ ПО ИСПОЛЬЗОВАНИЮ ТОРГОВОГО БОТА

1. НАЧАЛО РАБОТЫ
- Нажмите /start для активации
- Вы получите 48 часов бесплатной подписки
- Все команды: /help

2. НАСТРОЙКА API-КЛЮЧЕЙ (ОБЯЗАТЕЛЬНО)
1. Получите ключи в разделе "API Management" на бирже MEXC
2. Введите в боте:
   - /set_api_key → ваш API Key
   - /set_api_secret → ваш API Secret

⚠️ Никому не сообщайте эти ключи!

3. ОСНОВНЫЕ НАСТРОЙКИ
- /set_symbol - Торговая пара (пример: BTC/USDT)
- /set_amount - Сумма покупки в USDT (рекомендуем от 10$)
- /set_fall_percent - % падения для покупки (пример: 1.5)
- /set_rise_percent - % роста для продажи (пример: 2.0)
- /set_cooldown - Пауза между сделками (секунды)
- /set_orders_limit - Макс. активных ордеров (рекомендуем 3-5)

4. УПРАВЛЕНИЕ БОТОМ
- Запуск: /start_bot
- Остановка: /stop_bot
- Статус: /status
- Прибыль: /get_profit
- Настройки: /view_settings

5. ПРОДЛЕНИЕ ПОДПИСКИ
1. Нажмите кнопку «Купить подписку»
2. Отправьте USDT на предоставленный адрес:
   - Сеть: BSC (BEP-20)
   - Токен: ТОЛЬКО USDT
3. После оплаты нажмите «✅ Оплатил»
4. Ожидайте подтверждения (до 10 мин)

6. ВАЖНЫЕ ПРАВИЛА
• Всегда проверяйте адрес кошелька
• При проблемах используйте кнопку «Поддержка»
• Не храните крупные суммы на торговом аккаунте
• После подтверждения перевода подписка автоматически продлевается на 30 дней

7. БЕЗОПАСНОСТЬ
- Бот никогда не запрашивает пароли
- Все транзакции только на ваш кошелек
- API-ключи хранятся защищенно

8. ПОДДЕРЖКА
✉️ tradingbasemain@gmail.com
Или кнопка «Поддержка» в меню

💡 Совет: Включите уведомления Telegram!
    """

    # Отправляем инструкцию с поддержкой форматирования
    bot.send_message(
        message.chat.id,
        instruction_text,
        parse_mode='HTML',
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['admin_users'])
def handle_admin_users(message):
    if message.from_user.id not in ADMINS_ID:
        return

    users = settings['users'].keys()
    response = "👥 <b>Пользователи:</b>\n" + "\n".join(f"• ID: {user_id}" for user_id in users)
    bot.send_message(message.chat.id, response, parse_mode='HTML')


@bot.message_handler(commands=['get_logs'])
def handle_get_logs(message):
    if message.from_user.id not in ADMINS_ID:
        return

    try:
        # Проверяем существование файла логов
        if not os.path.exists(LOG_FILE):
            bot.reply_to(message, "❌ Файл логов не найден")
            return

        # Отправляем файл
        with open(LOG_FILE, 'rb') as log_file:
            bot.send_document(
                message.chat.id,
                log_file,
                caption="📁 Файл логов бота"
            )

    except Exception as e:
        logger.error(f"Ошибка отправки логов: {e}")
        bot.reply_to(message, f"❌ Ошибка при отправке файла логов: {e}")


@bot.message_handler(commands=['admin_broadcast'])
def handle_admin_broadcast(message):
    if message.from_user.id not in ADMINS_ID:
        return

    try:
        # Разбиваем сообщение на части: /admin_broadcast [target] [text]
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            raise ValueError("Недостаточно параметров. Формат: /admin_broadcast [all/user_id] [текст сообщения]")

        target = parts[1].strip().lower()
        text = parts[2].strip()

        if target == 'all':
            # Отправка всем пользователям
            user_ids = [int(uid) for uid in settings['users'].keys()]
            success = 0
            failed = 0

            for uid in user_ids:
                try:
                    bot.send_message(uid, f"📢 <b>Важное сообщение:</b>\n\n{text}", parse_mode='HTML')
                    success += 1
                    time.sleep(0.1)  # Защита от флуд-контроля
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения {uid}: {e}")
                    failed += 1

            report = (
                f"✅ Рассылка завершена!\n"
                f"• Успешно: {success}\n"
                f"• Не удалось: {failed}"
            )
        elif target.isdigit():
            # Отправка конкретному пользователю
            try:
                bot.send_message(int(target), f"📢 <b>Сообщение от администратора:</b>\n\n{text}", parse_mode='HTML')
                report = f"✅ Сообщение отправлено пользователю {target}"
            except Exception as e:
                report = f"❌ Ошибка отправки: {e}"
        else:
            report = "❌ Неверный формат получателя. Используйте 'all' или user_id"

        bot.reply_to(message, report)

    except Exception as e:
        logger.error(f"Ошибка рассылки: {e}")
        bot.reply_to(message, f"❌ Ошибка: {str(e)}\n\n"
                              "Используйте формат:\n"
                              "<code>/admin_broadcast all Ваш текст</code>\n"
                              "или\n"
                              "<code>/admin_broadcast 123456789 Ваш текст</code>",
                     parse_mode='HTML')


@bot.message_handler(commands=['admin_user_info'])
def handle_admin_user_info(message):
    if message.from_user.id not in ADMINS_ID:
        return

    try:
        # Парсим команду: /admin_user_info 123456789 [limit]
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError("Не указан ID пользователя")

        target_user_id = int(parts[1])
        trade_limit = 3  # По умолчанию 5 последних сделок
        if len(parts) > 2:
            trade_limit = int(parts[2])

        # Получаем настройки пользователя
        user_settings = get_user_settings(target_user_id)

        # Форматируем информацию о пользователе
        subscription_end = datetime.fromtimestamp(user_settings['subscription_end'])
        user_info = (
            f"👤 <b>Информация о пользователе {target_user_id}:</b>\n\n"
            f"• <b>Статус бота:</b> {'🟢 запущен' if user_settings.get('enabled', False) else '🔴 остановлен'}\n"
            f"• <b>Подписка активна до:</b> {subscription_end.strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"• <b>Торговая пара:</b> {user_settings['symbol']}\n"
            f"• <b>Процент падения:</b> {user_settings['fall_percent']}%\n"
            f"• <b>Процент роста:</b> {user_settings['rise_percent']}%\n"
            f"• <b>Сумма покупки:</b> {user_settings['amount']} USDT\n"
            f"• <b>Лимит ордеров:</b> {user_settings['orders_limit']}\n"
            f"• <b>Время ожидания:</b> {user_settings['cooldown']} сек\n\n"
        )

        # Получаем статистику прибыли
        conn = sqlite3.connect('profits.db')
        c = conn.cursor()

        # Общая прибыль
        c.execute('''SELECT SUM(profit) FROM profits WHERE user_id = ?''', (target_user_id,))
        total_profit = c.fetchone()[0] or 0

        # Общее количество сделок
        c.execute('''SELECT COUNT(*) FROM profits WHERE user_id = ?''', (target_user_id,))
        total_trades = c.fetchone()[0]

        # Прибыль по месяцам
        c.execute('''SELECT 
                     strftime('%Y-%m', datetime(timestamp, 'unixepoch')) as month,
                     SUM(profit), COUNT(*)
                     FROM profits 
                     WHERE user_id = ?
                     GROUP BY month
                     ORDER BY month DESC
                     LIMIT 6''', (target_user_id,))

        monthly_data = c.fetchall()

        profit_info = (
            f"📊 <b>Финансовая статистика:</b>\n"
            f"• Общая прибыль: <b>{total_profit:.2f} USDT</b>\n"
            f"• Всего сделок: <b>{total_trades}</b>\n\n"
        )

        if monthly_data:
            profit_info += "<b>Прибыль по месяцам:</b>\n"
            for row in monthly_data:
                month, profit, trades = row
                profit_info += f"• {month}: {profit:.2f} USDT ({trades} сделок)\n"
            profit_info += "\n"

        # Последние сделки
        trade_info = ""
        if trade_limit > 0 and total_trades > 0:
            c.execute('''SELECT * FROM profits 
                         WHERE user_id = ? 
                         ORDER BY timestamp DESC
                         LIMIT ?''', (target_user_id, trade_limit))

            trades = c.fetchall()

            trade_info = f"📝 <b>Последние {len(trades)} сделок:</b>\n\n"

            for trade in trades:
                trade_id, _, profit, timestamp, symbol, buy_price, sell_price = trade
                trade_time = datetime.fromtimestamp(timestamp).strftime('%d.%m.%Y %H:%M')
                trade_info += (
                    f"⚙️ <b>Сделка #{trade_id}</b>\n"
                    f"• Время: {trade_time}\n"
                    f"• Пара: {symbol}\n"
                    f"• Куплено по: {buy_price:.6f}\n"
                    f"• Продано по: {sell_price:.6f}\n"
                    f"• Прибыль: {profit:.6f} USDT\n\n"
                )
        else:
            trade_info = "ℹ️ Нет данных о сделках\n"

        conn.close()
        # Собираем полный ответ
        full_response = user_info + profit_info + trade_info
        # Отправляем сообщение
        bot.send_message(message.chat.id, full_response, parse_mode='HTML')

    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")


@bot.message_handler(commands=['admin_edit_user'])
def handle_admin_edit_user(message):
    if message.from_user.id not in ADMINS_ID:
        return

    try:
        # Парсим команду: /admin_edit_user 123456789 fall_percent 1.5
        parts = message.text.split()
        if len(parts) < 4:
            raise ValueError("Недостаточно параметров. Формат: /admin_edit_user [user_id] [параметр] [значение]")

        target_user_id = int(parts[1])
        setting_name = parts[2]
        setting_value = " ".join(parts[3:])

        # Запрещенные для изменения параметры
        if setting_name in ['api_key', 'api_secret']:
            raise ValueError("Изменение API ключей запрещено")

        user_settings = get_user_settings(target_user_id)

        # Преобразование типов
        if setting_name in ['fall_percent', 'rise_percent', 'amount']:
            setting_value = float(setting_value)
        elif setting_name in ['cooldown', 'orders_limit', 'subscription_price']:
            setting_value = int(setting_value)
        elif setting_name == 'enabled':
            setting_value = setting_value.lower() in ['true', '1', 'yes', 'y']
        elif setting_name == 'symbol':
            setting_value = setting_value.upper()

        user_settings[setting_name] = setting_value
        update_user_settings(target_user_id, user_settings)

        bot.reply_to(
            message,
            f"✅ Настройка обновлена:\n"
            f"Пользователь: {target_user_id}\n"
            f"Параметр: {setting_name}\n"
            f"Значение: {setting_value}"
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")


@bot.message_handler(commands=['admin_status'])
def handle_admin_status(message):
    if message.from_user.id not in ADMINS_ID:
        return

    def format_uptime(seconds):
        days = seconds // (24 * 3600)
        seconds %= (24 * 3600)
        hours = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60
        return f"{int(days)} дн. {int(hours)} ч. {int(minutes)} мин. {int(seconds)} сек."

    # Статистика пользователей
    total_users = len(settings['users'])
    active_users = sum(1 for u in settings['users'].values() if u.get('enabled', False))
    users_with_api = sum(1 for u in settings['users'].values() if u.get('api_key') and u.get('api_secret'))

    # Статистика потоков
    total_threads = threading.active_count()
    active_trading_threads = []
    for user_id_str, data in user_threads.items():
        if data['thread'].is_alive():
            user_id = int(user_id_str)
            user_settings = get_user_settings(user_id)
            runtime = time.time() - data['start_time']
            mins, secs = divmod(int(runtime), 60)
            hours, mins = divmod(mins, 60)
            active_trading_threads.append(
                f"• ID: {user_id} | Пара: {user_settings['symbol']} | "
                f"Время: {hours:02d}:{mins:02d}:{secs:02d} | "
                f"Рестартов: {data.get('restart_count', 0)}"
            )

    # Статистика кошельков
    with wallets_lock:
        reserved_wallets = len(active_wallets)
        checking_wallets = sum(1 for w in active_wallets if w.get('checking', False))
        wallet_occupation = f"{reserved_wallets}/{len(WALLETS)}"

    # Статистика памяти
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    mem_usage = mem_info.rss / (1024 ** 2)  # в MB

    # Статистика файлов
    log_size = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    db_size = os.path.getsize('profits.db') if os.path.exists('profits.db') else 0
    settings_size = os.path.getsize(SETTINGS_PATH) if os.path.exists(SETTINGS_PATH) else 0

    # Статистика ошибок
    error_count = 0
    if os.path.exists(LOG_FILE):
        try:
            # Пробуем прочитать в UTF-8
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if 'ERROR' in line or 'CRITICAL' in line:
                        error_count += 1
        except UnicodeDecodeError:
            try:
                # Если не получилось, пробуем Windows-1251
                with open(LOG_FILE, 'r', encoding='cp1251') as f:
                    for line in f:
                        if 'ERROR' in line or 'CRITICAL' in line:
                            error_count += 1
            except Exception as e:
                logger.error(f"Ошибка чтения лог-файла: {e}")
        except Exception as e:
            logger.error(f"Ошибка чтения лог-файла: {e}")

    # Формирование отчета
    response = (
        "📡 <b>ДЕТАЛЬНЫЙ СТАТУС СИСТЕМЫ</b>\n\n"

        "👥 <b>Пользователи:</b>\n"
        f"• Всего: {total_users}\n"
        f"• Активных ботов: {active_users}\n"
        f"• С настроенными API: {users_with_api}\n\n"

        "🧵 <b>Потоки:</b>\n"
        f"• Всего потоков: {total_threads}\n"
        f"• Торговых потоков: {len(active_trading_threads)}\n\n"

        "💼 <b>Кошельки:</b>\n"
        f"• Занято/Всего: {wallet_occupation}\n"
        f"• В процессе проверки: {checking_wallets}\n\n"

        "📊 <b>Данные:</b>\n"
        f"• Кэш цен: {len(price_cache)} символов\n"
        f"• Размер лога: {log_size / 1024:.1f} KB\n"
        f"• Размер БД: {db_size / 1024:.1f} KB\n"
        f"• Размер настроек: {settings_size / 1024:.1f} KB\n"
        f"• Ошибок в логе: {error_count}\n\n"

        "💻 <b>Ресурсы:</b>\n"
        f"• Память: {mem_usage:.1f} MB\n"
        f"• Загрузка CPU: {psutil.cpu_percent()}%\n"
        f"• Загрузка RAM: {psutil.virtual_memory().percent}%\n\n"

        "⏱ <b>Время работы:</b>\n"
        f"• Системы: {format_uptime(time.time() - start_time)}"
    )

    # Активные торговые потоки
    if active_trading_threads:
        response += "🔥 <b>Активные торговые потоки:</b>\n" + "\n".join(active_trading_threads)
    else:
        response += "\nℹ️ Нет активных торговых потоков"

    # Отправка отчета
    bot.send_message(message.chat.id, response, parse_mode='HTML')


@bot.message_handler(commands=['admin_add_subscription'])
def handle_add_subscription(message):
    user_id = message.from_user.id
    if user_id not in ADMINS_ID:
        bot.reply_to(message, "❌ Эта команда доступна только администратору")
        return

    user_states[user_id] = 'waiting_subscription_data'
    bot.reply_to(
        message,
        "Введите данные в формате:\n"
        "<ID пользователя> <секунды>",
        reply_markup=make_keyboard())


@bot.message_handler(commands=['view_settings'])
def handle_view_settings(message):
    user_id = message.from_user.id
    user_settings = get_user_settings(user_id)

    # Русские названия параметров
    setting_names = {
        'symbol': 'Торговая пара',
        'api_key': 'API Key',
        'api_secret': 'API Secret',
        'fall_percent': 'Процент падения',
        'rise_percent': 'Процент роста',
        'cooldown': 'Время ожидания',
        'amount': 'Сумма покупки',
        'orders_limit': 'Лимит ордеров',
        'subscription_price': 'Цена подписки',
        'subscription_end': 'Окончание подписки',
        'enabled': 'Статус бота'
    }

    # Форматирование значений
    formatted_settings = []
    for key, value in user_settings.items():
        # Пропускаем ненужные параметры
        if key in ['subscription_price', 'sub']:
            continue

        # Форматирование специальных значений
        if key == 'subscription_end':
            value = datetime.fromtimestamp(value).strftime('%d.%m.%Y %H:%M:%S')
        elif key == 'enabled':
            value = "🟢 запущен" if value else "🔴 остановлен"
        elif key == 'api_key' and value:
            value = '*****' + value[-4:]
        elif key == 'api_secret' and value:
            value = '*****' + value[-4:]

        # Добавляем форматированную строку
        if key in setting_names:
            formatted_settings.append(f"• <b>{setting_names[key]}</b>: {value}")

    response = "🔧 <b>Ваши настройки:</b>\n" + "\n".join(formatted_settings)
    bot.send_message(message.chat.id, response, reply_markup=make_keyboard(), parse_mode='HTML')


@bot.message_handler(commands=['set_symbol'])
def set_symbol(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_symbol'
    bot.send_message(
        message.chat.id,
        "Введите торговую пару (например BTC/USDT):\n"
        "(Рекомендуемые пары: XRP/USDT; SOL/USDT)",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['set_orders_limit'])
def set_symbol(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_orders_limit'
    bot.send_message(
        message.chat.id,
        "Введите лимит одновременно активных ордеров:",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['set_amount'])
def set_amount(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_amount'
    bot.send_message(
        message.chat.id,
        "Введите сумму покупки в USDT:",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['set_api_key'])
def set_api_key(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_api_key'
    bot.send_message(
        message.chat.id,
        "Введите ваш API Key:",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['set_api_secret'])
def set_api_secret(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_api_secret'
    bot.send_message(
        message.chat.id,
        "Введите ваш API Secret:",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['set_fall_percent'])
def set_fall_percent(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_fall_percent'
    bot.send_message(
        message.chat.id,
        "Введите процент падения (например 1.5):",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['set_rise_percent'])
def set_rise_percent(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_rise_percent'
    bot.send_message(
        message.chat.id,
        "Введите процент роста (например 2.0):",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['set_cooldown'])
def set_cooldown(message):
    user_id = message.from_user.id
    user_states[user_id] = 'waiting_cooldown'
    bot.send_message(
        message.chat.id,
        "Введите время ожидания в секундах:",
        reply_markup=make_keyboard()
    )


@bot.message_handler(commands=['start_bot'])
def start_user_bot(message):
    user_id = message.from_user.id
    user_id_str = str(user_id)
    user_settings = get_user_settings(user_id)

    if time.time() > user_settings['subscription_end']:
        bot.reply_to(message, "❌ Ваша подписка истекла! Продлите подписку для запуска бота.")
        return

    # Проверка обязательных параметров
    required = ['api_key', 'api_secret', 'amount']
    missing = [p for p in required if not user_settings.get(p)]

    if missing:
        bot.reply_to(
            message,
            f"❌ <b>Не заданы параметры:</b> {', '.join(missing)}\n"
            "Пожалуйста, настройте бота перед запуском.",
            parse_mode='HTML'
        )
        return

    # Проверка, не запущен ли уже бот
    if user_settings.get('enabled', False):
        bot.reply_to(message, "✅ Бот уже запущен")
        return

    # Запуск бота
    user_settings['enabled'] = True
    update_user_settings(user_id, user_settings)

    # Создаем и запускаем поток для пользователя
    thread = threading.Thread(
        target=user_trading_bot,
        args=(user_id,),
        daemon=True
    )
    user_threads[user_id_str] = {
        'thread': thread,
        'start_time': time.time(),
        'restart_count': 0
    }
    thread.start()

    bot.reply_to(message, "🚀 Торговый бот запущен!")


@bot.message_handler(commands=['stop_bot'])
def stop_user_bot(message):
    user_id = message.from_user.id
    user_id_str = str(user_id)
    user_settings = get_user_settings(user_id)

    if not user_settings.get('enabled', False):
        bot.reply_to(message, "❌ Бот не был запущен")
        return

    # Остановка бота
    user_settings['enabled'] = False
    update_user_settings(user_id, user_settings)

    bot.reply_to(message, "Остановка...")


@bot.message_handler(commands=['get_profit'])
def get_user_profit(message):
    user_id = message.from_user.id
    try:
        conn = sqlite3.connect('profits.db')
        c = conn.cursor()

        # Рассчитываем начало и конец текущего месяца
        today = datetime.now()
        first_day = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _, last_day_of_month = monthrange(today.year, today.month)
        last_day = today.replace(day=last_day_of_month, hour=23, minute=59, second=59)

        # Выполняем запрос
        c.execute('''SELECT SUM(profit), COUNT(*), symbol 
                     FROM profits 
                     WHERE user_id = ? 
                     AND timestamp BETWEEN ? AND ? 
                     GROUP BY symbol''',
                  (user_id, first_day.timestamp(), last_day.timestamp()))

        results = c.fetchall()
        conn.close()

        if not results:
            response = "ℹ️ Нет данных о сделках за текущий месяц"
            bot.reply_to(message, response, parse_mode='HTML')
            return

        response = "💰 <b>Статистика прибыли:</b>\n"
        total_profit = 0
        total_trades = 0

        for row in results:
            profit, trades, symbol = row
            total_profit += profit if profit else 0
            total_trades += trades if trades else 0
            response += f"\n• <b>{symbol}</b>:\n" \
                        f"  Прибыль: <b>{profit:.2f} USDT</b>\n" \
                        f"  Сделки: <b>{trades}</b>\n"

        response += f"\n<b>Итого за {today.strftime('%B')}:</b>\n" \
                    f"• Общая прибыль: <b>{total_profit:.2f} USDT</b>\n" \
                    f"• Всего сделок: <b>{total_trades}</b>"

        bot.reply_to(message, response, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Ошибка получения прибыли: {e}")
        bot.reply_to(message, f"❌ Ошибка получения данных: {str(e)}")


@bot.message_handler(commands=['status'])
def bot_status(message):
    user_id = message.from_user.id
    user_settings = get_user_settings(user_id)
    status = "🟢 запущен" if user_settings.get('enabled', False) else "🔴 остановлен"

    # Проверка цен
    try:
        symbol = user_settings['symbol']
        price = get_cached_price(symbol)
        price_info = f"Текущая цена {symbol}: {price:.2f}"
    except:
        price_info = "Не удалось получить цену"

    response = (
        f"📊 <b>Статус вашего бота:</b> {status}\n"
        f"{price_info}\n\n"
        "Используйте /view_settings для просмотра настроек"
    )
    bot.reply_to(message, response, parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == 'Поддержка')
def handle_support(message):
    bot.send_message(
        message.chat.id,
        "📧 По всем вопросам пишите на почту:\n"
        "<b>tradingbasemain@gmail.com</b>",
        reply_markup=make_keyboard(),
        parse_mode='HTML'
    )


@bot.message_handler(func=lambda m: m.text == 'Купить подписку')
def handle_buy_subscription(message):
    user_id = message.from_user.id
    user_settings = get_user_settings(user_id)

    # Проверяем, не имеет ли пользователь уже активный кошелек
    with wallets_lock:
        user_wallets = [w for w in active_wallets if w['user_id'] == user_id]
        if user_wallets:
            wallet = user_wallets[0]
            remaining = int(WALLET_RESERVE_TIME - (time.time() - wallet['reserved_at']))
            mins, secs = divmod(remaining, 60)

            bot.send_message(
                message.chat.id,
                f"⚠️ У вас уже зарезервирован кошелек:\n<code>{wallet['address']}</code>\n\n"
                f"⏳ Осталось времени: {mins} мин {secs} сек\n\n"
                "После оплаты нажмите '✅ Оплатил'",
                parse_mode='HTML',
                reply_markup=payment_keyboard()
            )
            return

    # Выдаем свободный кошелек
    wallet_address = get_available_wallet()
    if not wallet_address:
        bot.send_message(
            message.chat.id,
            "😔 Все кошельки заняты. Попробуйте позже.",
            reply_markup=make_keyboard()
        )
        return

    # Резервируем кошелек
    subscription_price = user_settings['subscription_price']
    with wallets_lock:
        active_wallets.append({
            'address': wallet_address,
            'user_id': user_id,
            'reserved_at': time.time(),
            'amount': subscription_price})

    # Отправляем информацию пользователю
    subscription_price = user_settings['subscription_price']
    bot.send_message(message.chat.id,
                     f"Стоимость подписки {subscription_price} USDT за 30 дней\n"
                     f"💳 Для оплаты подписки отправьте {subscription_price} USDT на кошелек:\n\n"
                     f"<code>{wallet_address}</code>\n\n"
                     "⚠️ Внимание:\n"
                     "1. Отправляйте только USDT (сеть - BSC-20).\n"
                     "2. Проверяйте адрес перед отправкой, утерянные средства невозможно вернуть.\n"
                     "3. В случае Вашей ошибки при совершении транзакции потерянные средства возвращены не будут!\n\n"
                     "⏳ Кошелек зарезервирован на 60 минут\n"
                     "После оплаты нажмите '✅ Оплатил'",
                     parse_mode='HTML',
                     reply_markup=payment_keyboard())


# Добавляем функцию для обработки платежа в отдельном потоке
def process_payment_confirmation(user_id, wallet_data):
    try:
        wallet_address = wallet_data['address']
        amount = wallet_data['amount']

        payment_confirmed = fetch_deposits.sync_main(amount, wallet_address)

        with wallets_lock:
            # Удаляем кошелек только после успешной проверки
            if payment_confirmed and wallet_data in active_wallets:
                active_wallets.remove(wallet_data)
            elif wallet_data in active_wallets:
                # Снимаем флаг проверки при неудаче
                wallet_data['checking'] = False

        if payment_confirmed:
            # Продлеваем подписку
            new_end = extend_subscription(user_id, seconds=30 * 24 * 60 * 60)
            end_date = datetime.fromtimestamp(new_end).strftime('%d.%m.%Y %H:%M:%S')
            bot.send_message(user_id, f"✅ Оплата подтверждена! Ваша подписка продлена до {end_date}",
                             reply_markup=make_keyboard())
        else:
            bot.send_message(
                user_id,
                f"❌ Платеж на кошелек <code>{wallet_address}</code> не обнаружен.\n\n"
                "Если вы отправили средства:\n"
                "1. Дождитесь подтверждения сети\n"
                "2. Повторите попытку через 5-10 минут\n"
                "3. Если проблема сохраняется, свяжитесь с поддержкой",
                parse_mode='HTML',
                reply_markup=payment_keyboard())

    except Exception as e:
        logger.error(f"Ошибка обработки платежа для {user_id}: {e}")
        # Снимаем флаг проверки при ошибке
        with wallets_lock:
            if wallet_data in active_wallets:
                wallet_data['checking'] = False

        bot.send_message(user_id, "⚠️ Произошла ошибка при обработке платежа. Попробуйте позже.",
                         reply_markup=payment_keyboard())


@bot.message_handler(func=lambda m: m.text == '✅ Оплатил')
def handle_payment_confirmation(message):
    user_id = message.from_user.id

    with wallets_lock:
        user_wallets = [w for w in active_wallets if w['user_id'] == user_id]
        if not user_wallets:
            bot.send_message(
                message.chat.id,
                "❌ У вас нет активного резерва кошелька.\n"
                "Начните процесс оплаты заново.",
                reply_markup=make_keyboard())
            return

        wallet = user_wallets[0]
        # Добавляем метку о начале проверки (НЕ УДАЛЯЕМ!)
        wallet['checking'] = True
        wallet['checking_start'] = time.time()

    # Уведомляем пользователя
    bot.send_message(message.chat.id, "🔄 Проверяем ваш платеж... Это может занять несколько минут.",
                     reply_markup=ReplyKeyboardRemove())

    # Запускаем проверку
    thread = threading.Thread(
        target=process_payment_confirmation,
        args=(user_id, wallet),
        daemon=True)
    thread.start()


# Обработчик отмены оплаты
@bot.message_handler(func=lambda m: m.text == '❌ Отмена')
def handle_payment_cancel(message):
    user_id = message.from_user.id

    with wallets_lock:
        user_wallets = [w for w in active_wallets if w['user_id'] == user_id]
        if user_wallets:
            # Удаляем кошелек из активных
            active_wallets.remove(user_wallets[0])

            bot.send_message(
                message.chat.id,
                "❌ Резерв кошелька отменен.\n"
                "Вы можете начать заново в любое время.",
                reply_markup=make_keyboard()
            )
        else:
            bot.send_message(
                message.chat.id,
                "ℹ️ У вас нет активного резерва кошелька.",
                reply_markup=make_keyboard())


@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_id = message.from_user.id

    if user_id in user_states:
        state = user_states[user_id]
        user_settings = get_user_settings(user_id)

        if state == 'waiting_symbol':
            user_settings['symbol'] = message.text.upper()
            bot.reply_to(message, f"✅ Торговая пара установлена: {message.text}")

        elif state == 'waiting_amount':
            try:
                amount = float(message.text)
                user_settings['amount'] = amount
                bot.reply_to(message, f"✅ Сумма покупки установлена: {amount} USDT")
            except:
                bot.reply_to(message, "❌ Неверный формат суммы. Введите число.")

        elif state == 'waiting_api_key':
            user_settings['api_key'] = message.text
            bot.reply_to(message, "✅ API Key сохранен")

        elif state == 'waiting_api_secret':
            user_settings['api_secret'] = message.text
            bot.reply_to(message, "✅ API Secret сохранен")

        elif state == 'waiting_fall_percent':
            try:
                percent = float(message.text)
                user_settings['fall_percent'] = percent
                bot.reply_to(message, f"✅ Процент падения установлен: {percent}%")
            except:
                bot.reply_to(message, "❌ Неверный формат. Введите число (например 1.5).")

        elif state == 'waiting_rise_percent':
            try:
                percent = float(message.text)
                user_settings['rise_percent'] = percent
                bot.reply_to(message, f"✅ Процент роста установлен: {percent}%")
            except:
                bot.reply_to(message, "❌ Неверный формат. Введите число (например 2.0).")

        elif state == 'waiting_cooldown':
            try:
                seconds = int(message.text)
                user_settings['cooldown'] = seconds
                bot.reply_to(message, f"✅ Время ожидания установлено: {seconds} сек")
            except:
                bot.reply_to(message, "❌ Неверный формат. Введите целое число секунд.")

        elif state == 'waiting_orders_limit':
            try:
                orders_limit = int(message.text)
                user_settings['orders_limit'] = orders_limit
                bot.reply_to(message, f"✅ Лимит одновременно активных ордеров установлен: {orders_limit}")
            except:
                bot.reply_to(message, "❌ Неверный формат. Введите целое число.")

        elif state == 'waiting_subscription_data':
            try:
                # Парсим ввод: ID и секунд
                data = message.text.split()
                if len(data) < 2:
                    raise ValueError("Необходимо указать ID пользователя и количество секунд")
                target_user_id = int(data[0])
                seconds = int(data[1])
                new_end = extend_subscription(target_user_id, seconds=seconds)
                # Форматируем дату для читаемости
                end_date = datetime.fromtimestamp(new_end).strftime('%d.%m.%Y %H:%M:%S')
                bot.reply_to(message, f"✅ Подписка пользователя {target_user_id} изменена.\n"
                                      f"Новое окончание: {end_date}")

            except ValueError as ve:
                bot.reply_to(message, f"❌ Ошибка формата: {str(ve)}\n"
                                      "Используйте формат: <ID> <секунды>")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка изменения подписки: {str(e)}")

        # Сохраняем обновленные настройки
        update_user_settings(user_id, user_settings)
        del user_states[user_id]

    else:
        bot.send_message(
            message.chat.id,
            "Используйте /help для списка команд",
            reply_markup=make_keyboard()
        )


def subscription_notifier():
    """Фоновый поток для уведомлений об окончании подписки"""
    logger.info("Запуск системы уведомлений о подписках")
    while True:
        try:
            current_time = time.time()
            # Проверяем всех пользователей
            for user_id_str, user_settings in settings['users'].items():
                try:
                    user_id = int(user_id_str)
                    end_time = user_settings['subscription_end']

                    # Рассчитываем оставшееся время в днях
                    days_left = (end_time - current_time) / (24 * 3600)

                    # Если осталось 3 дня или меньше и уведомление еще не отправлялось
                    if 0 < days_left <= 3 and (user_id, int(days_left)) not in sent_notifications:
                        # Форматируем дату окончания
                        end_date = datetime.fromtimestamp(end_time).strftime('%d.%m.%Y %H:%M:%S')

                        # Формируем сообщение
                        message = (
                            f"⚠️ <b>ВАЖНОЕ УВЕДОМЛЕНИЕ</b>\n\n"
                            f"Ваша подписка истекает через <b>{int(days_left)} дня</b>!\n"
                            f"Окончание: {end_date}\n\n"
                            "Для продолжения работы бота необходимо продлить подписку.\n"
                            "Используйте команду /subscription для просмотра информации о подписке."
                        )

                        # Отправляем сообщение
                        bot.send_message(user_id, message, parse_mode='HTML')

                        # Запоминаем, что уведомление отправлено
                        sent_notifications.add((user_id, int(days_left)))
                        logger.info(f"Уведомление отправлено пользователю {user_id} (осталось {int(days_left)} дн.)")

                except Exception as e:
                    logger.error(f"Ошибка обработки пользователя {user_id_str}: {e}")

            # Пауза между проверками (1 час)
            time.sleep(3600)

        except Exception as e:
            logger.error(f"Ошибка в потоке уведомлений: {e}")
            time.sleep(600)


def run_telegram_bot():
    """Запуск Telegram бота"""
    while True:
        try:
            logger.info("Запуск Telegram бота...")
            bot.infinity_polling()
        except Exception as e:
            logger.error(f"Ошибка Telegram бота: {e}, перезапуск через 10 секунд")
            time.sleep(10)


#############################################################################
# Основной цикл программы
#############################################################################

if __name__ == "__main__":
    init_profit_db()
    load_settings()

    # Запуск системы обновления цен
    price_thread = threading.Thread(target=price_updater, daemon=True)
    price_thread.start()

    # Запуск Telegram бота
    telegram_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    telegram_thread.start()

    # Запуск системы уведомлений о подписках
    notifier_thread = threading.Thread(target=subscription_notifier, daemon=True)
    notifier_thread.start()

    # Автозапуск торговых потоков для включенных пользователей
    for user_id_str, user_data in settings['users'].items():
        if user_data.get('enabled', False):
            user_id = int(user_id_str)
            logger.info(f"Автозапуск торгового бота для пользователя {user_id}")

            thread = threading.Thread(
                target=user_trading_bot,
                args=(user_id,),
                daemon=True
            )
            user_threads[user_id_str] = {
                'thread': thread,
                'start_time': time.time(),
                'restart_count': 0
            }
            thread.start()

    # Основной цикл мониторинга
    while True:
        try:
            # Проверка состояния потоков
            for user_id_str, data in list(user_threads.items()):
                thread = data['thread']
                user_id = int(user_id_str)

                if not thread.is_alive():
                    user_settings = get_user_settings(user_id)

                    # Если бот должен быть активен - перезапускаем
                    if user_settings.get('enabled', False):
                        restart_count = data.get('restart_count', 0) + 1

                        # Логируем перезапуск
                        logger.warning(f"Перезапуск торгового потока для {user_id} (попытка #{restart_count})")

                        # Создаем новый поток
                        new_thread = threading.Thread(
                            target=user_trading_bot,
                            args=(user_id,),
                            daemon=True
                        )
                        new_thread.start()

                        # Обновляем данные потока
                        user_threads[user_id_str] = {
                            'thread': new_thread,
                            'start_time': time.time(),
                            'restart_count': restart_count
                        }
                    else:
                        # Удаляем неактивный поток
                        del user_threads[user_id_str]
                        logger.info(f"Удален остановленный поток для {user_id}")

            # Пауза между проверками
            time.sleep(15)

        except KeyboardInterrupt:
            logger.info("Завершение работы...")
            # Останавливаем все пользовательские боты
            for user_id_str in list(settings['users'].keys()):
                settings['users'][user_id_str]['enabled'] = False
            save_settings()
        except Exception as e:
            logger.error(f"Ошибка в основном цикле: {e}")
            time.sleep(30)
