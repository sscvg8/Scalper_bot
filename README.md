# ScalperBot

**ScalperBot** – это Telegram-бот на Python, который торгует на бирже MEXC по стратегии DCA и ведёт учёт прибыли в SQLite-базе. Бот позволяет пользователям задавать собственные параметры торговли, управлять подпиской и наблюдать за сделками прямо из Telegram-чата.

---

## Возможности

* Автоматическая покупка/продажа по заданным порогам падения/роста цены
* Одновременная работа с несколькими пользователями
* Система подписок (оплата вне рамок репозитория)
* Хранение прибыли в базе `profits.db` и вывод статистики
* Отправка уведомлений о сделках и логов в Telegram
* Проверка входящих депозитов на указанные кошельки (скрипт `fetch_deposits.py`)

---

## Быстрый старт

1. **Клонируйте репозиторий**
   ```bash
   git clone <repo_url>
   cd scalper_for_git
   ```
2. **Создайте виртуальное окружение и установите зависимости**
   ```bash
   python -m venv venv
   venv\Scripts\activate          # Windows
   # source venv/bin/activate      # Linux / macOS
   pip install -r requirements.txt
   ```
3. **Добавьте переменные окружения**. Создайте файл `.env` (или экспортируйте переменные любым удобным способом):
   ```env
   TELEGRAM_BOT_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   MEXC_API_KEY=xxxxxxxxxxxxxxxx
   MEXC_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ETHERSCAN_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   Чтобы бот считал переменные из файла `.env`, установите пакет `python-dotenv` и раскомментируйте соответствующий код в `ScalperBot.py` (или добавьте свой).

4. **Настройте файл `trading_bot_settings.json`**
   При первом запуске он создаётся автоматически, но вы можете заранее задать глобальные настройки:
   ```json
   {
     "users": {
       "123456789": {
         "symbol": "BTC/USDT",
         "fall_percent": 1.0,
         "rise_percent": 1.5,
         "cooldown": 60,
         "amount": 10.0,
         "orders_limit": 0,
         "subscription_price": 30,
         "subscription_end": 0,
         "enabled": false,
         "sub": 0,
         "api_key": "",
         "api_secret": ""
       }
     }
   }
   ```
   Пользователь может изменить эти параметры через команды бота (`/set_api_key`, `/set_symbol`, `/start_bot` и т. д.).

5. **Запустите бота**
   ```bash
   python ScalperBot.py
   ```
   При успешном запуске бот выведет лог «Запуск торгового бота…», а в Telegram-чате `/start` откроет меню.

---

## Основные команды Telegram-бота

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/start_bot` \| `/stop_bot` | Включить/выключить торгового бота |
| `/set_symbol` | Задать торговую пару (например, `BTC/USDT`) |
| `/set_amount` | Установить сумму сделки (USDT) |
| `/set_fall_percent` | Процент падения цены для покупки |
| `/set_rise_percent` | Процент роста цены для продажи |
| `/set_api_key` \| `/set_api_secret` | Задать API-ключ и секрет MEXC |
| `/status` | Текущие настройки и состояние бота |
| `/profit` | Показать статистику прибыли |

*Для администраторов доступны дополнительные команды управления подписками.*

