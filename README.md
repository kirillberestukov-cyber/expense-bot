# Expense Bot

Telegram-бот для учёта личных расходов. Пишешь `кофе 300` — выбираешь категорию — записано.

Поддерживает мультивалютность, совместный учёт, бюджеты, аналитику и экспорт — всё внутри Telegram.

## Требования

- Python 3.9+
- Токен бота от [@BotFather](https://t.me/BotFather)

## Установка

```bash
git clone https://github.com/kirillberestukov-cyber/expense-bot.git
cd expense-bot
pip install -r requirements.txt
```

## Запуск

```bash
BOT_TOKEN=your_token_here python3 bot.py
```

Или вставь токен прямо в `bot.py`, заменив строку `ВСТАВЬ_ТОКЕН_СЮДА`, и запускай просто:

```bash
python3 bot.py
```

## Автозапуск на macOS (launchd)

Чтобы бот работал постоянно и перезапускался сам:

**1. Создай plist-файл:**

```bash
nano ~/Library/LaunchAgents/com.expensebot.plist
```

**2. Вставь содержимое:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.expensebot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/ПОЛНЫЙ/ПУТЬ/К/bot.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>BOT_TOKEN</key>
        <string>ВАШ_ТОКЕН</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/ПОЛНЫЙ/ПУТЬ/К/ПАПКЕ</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/ПОЛНЫЙ/ПУТЬ/К/ПАПКЕ/bot.log</string>
    <key>StandardErrorPath</key>
    <string>/ПОЛНЫЙ/ПУТЬ/К/ПАПКЕ/bot.log</string>
</dict>
</plist>
```

**3. Активируй:**

```bash
launchctl load ~/Library/LaunchAgents/com.expensebot.plist
```

**Управление:**

```bash
# Остановить
launchctl unload ~/Library/LaunchAgents/com.expensebot.plist

# Запустить снова
launchctl load ~/Library/LaunchAgents/com.expensebot.plist

# Логи в реальном времени
tail -f bot.log
```

## Использование

Напиши расход в формате `название сумма`:

```
кофе 300
такси 1000
продукты в пятёрке 2500
```

Мультивалютность (Pro): добавь символ валюты — бот конвертирует в рубли автоматически:

```
coffee 5$
lunch 15€
```

Бот покажет клавиатуру с категориями — выбери нужную, и расход сохранится.

## Команды

### Базовые (бесплатно)

| Команда | Описание |
|---|---|
| `/start` | Инструкция и список команд |
| `/stats` | Статистика за текущий месяц |
| `/history` | Последние 10 записей |
| `/add` | Добавить свою категорию |
| `/add Название` | Добавить категорию сразу |
| `/categories` | Список всех категорий |
| `/undo` | Удалить последнюю запись |

### Pro ⭐

Разблокируются покупкой за Telegram Stars (1 звезда — навсегда).

| Команда | Описание |
|---|---|
| `/regular` | Регулярные расходы (аренда, подписки) — автозапись каждый месяц |
| `/budget` | Бюджеты по категориям с прогресс-баром и предупреждениями |
| `/limit` | Дневные и недельные лимиты трат |
| `/compare` | Сравнение текущего и прошлого месяца |
| `/year` | Годовая сводка по месяцам |
| `/weekday` | Аналитика по дням недели (HTML-отчёт) |
| `/wallet` | Совместный учёт с другими людьми |
| `/goal` | Цели накоплений с прогрессом |
| `/export` | Экспорт расходов в CSV / JSON / Excel |

Мультивалютный ввод (`5$`, `15€`, `100£`) тоже доступен только в Pro.

### Админ

| Команда | Описание |
|---|---|
| `/admin` | Панель управления (статистика, пользователи, выдача/отзыв Pro) |

## Категории по умолчанию

🍕 Еда · 🚕 Транспорт · 🎬 Развлечения · 💊 Здоровье · 👕 Одежда · 🏠 Жильё/ЖКХ · 📱 Связь · 🛒 Покупки · ✈️ Путешествия · 📦 Другое

Добавляй свои командой `/add`.
