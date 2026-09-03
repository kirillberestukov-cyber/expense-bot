#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import logging
import os
import re
import csv
import io
import json
import random
import string
import aiohttp
import aiosqlite
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
)

# ── Конфиг ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
DB_PATH = "expenses.db"

DEFAULT_CATEGORIES = [
    "🍕 Еда",
    "🚕 Транспорт",
    "🎬 Развлечения",
    "💊 Здоровье",
    "👕 Одежда",
    "🏠 Жильё/ЖКХ",
    "📱 Связь",
    "🛒 Покупки",
    "✈️ Путешествия",
    "📦 Другое",
]

MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

PRO_BUTTON_TEXT = "⭐ Купить Pro"
PRO_PRICE = 1  # минимум — 1 звезда
ADMIN_ID = int(os.getenv("ADMIN_ID", "5540838704"))
CHANNEL_USERNAME = "@finance_hacks_ru_orig"

CURRENCY_MAP = {
    "$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY",
    "¥": "CNY", "₸": "KZT", "₴": "UAH", "₽": "RUB",
}
CURRENCY_SYMBOL = {v: k for k, v in CURRENCY_MAP.items()}

_rates_cache: dict = {"rates": {}, "ts": 0.0}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── FSM States ───────────────────────────────────────────────────────────────

class Form(StatesGroup):
    choosing_category = State()
    adding_category = State()
    adding_category_cmd = State()
    admin_grant_pro = State()
    admin_revoke_pro = State()
    regular_input = State()
    regular_category = State()
    regular_day = State()
    budget_category = State()
    budget_amount = State()
    wallet_create = State()
    wallet_join = State()
    choosing_scope = State()
    limit_amount = State()
    goal_name = State()
    goal_amount = State()


# ── База данных ───────────────────────────────────────────────────────────────

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                amount      REAL    NOT NULL,
                category    TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS custom_categories (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                name     TEXT    NOT NULL,
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id     INTEGER PRIMARY KEY,
                purchased_at TEXT NOT NULL
            )
        """)
        async with db.execute("PRAGMA table_info(expenses)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "original_amount" not in cols:
            await db.execute("ALTER TABLE expenses ADD COLUMN original_amount REAL")
            await db.execute("ALTER TABLE expenses ADD COLUMN original_currency TEXT")
        if "wallet_id" not in cols:
            await db.execute("ALTER TABLE expenses ADD COLUMN wallet_id INTEGER")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                category     TEXT    NOT NULL,
                limit_amount REAL    NOT NULL,
                UNIQUE(user_id, category)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wallet_members (
                wallet_id INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                role      TEXT NOT NULL DEFAULT 'member',
                UNIQUE(wallet_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wallet_invites (
                code      TEXT PRIMARY KEY,
                wallet_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                name          TEXT    NOT NULL,
                amount        REAL    NOT NULL,
                category      TEXT    NOT NULL,
                day_of_month  INTEGER NOT NULL,
                last_recorded TEXT,
                created_at    TEXT    NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_limits (
                user_id     INTEGER PRIMARY KEY,
                daily       REAL,
                weekly      REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                target      REAL    NOT NULL,
                saved       REAL    NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            )
        """)
        await db.commit()


async def db_add_expense(user_id: int, name: str, amount: float, category: str,
                         original_amount: float | None = None,
                         original_currency: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO expenses
               (user_id, name, amount, category, created_at, original_amount, original_currency)
               VALUES (?,?,?,?,?,?,?)""",
            (user_id, name, amount, category, datetime.now().isoformat(),
             original_amount, original_currency),
        )
        await db.commit()


async def db_get_categories(user_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM custom_categories WHERE user_id=? ORDER BY name", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return DEFAULT_CATEGORIES + [r[0] for r in rows]


async def db_add_category(user_id: int, name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO custom_categories (user_id, name) VALUES (?,?)", (user_id, name)
            )
            await db.commit()
        except Exception:
            pass  # уже существует


async def db_month_stats(user_id: int):
    now = datetime.now()
    month_start = f"{now.year}-{now.month:02d}-01"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT category, SUM(amount) FROM expenses
               WHERE user_id=? AND created_at>=?
               GROUP BY category ORDER BY SUM(amount) DESC""",
            (user_id, month_start),
        ) as cur:
            return await cur.fetchall()


async def db_last_expenses(user_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT name, amount, category, created_at,
                      original_amount, original_currency
               FROM expenses WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ) as cur:
            return await cur.fetchall()


async def db_delete_last(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM expenses WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM expenses WHERE id=?", (row[0],))
        await db.commit()
        return True


async def db_is_pro(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=?", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def db_set_pro(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, purchased_at) VALUES (?,?)",
            (user_id, datetime.now().isoformat()),
        )
        await db.commit()


async def db_add_recurring(user_id: int, name: str, amount: float,
                           category: str, day: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO recurring_expenses
               (user_id, name, amount, category, day_of_month, created_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, name, amount, category, day, datetime.now().isoformat()),
        )
        await db.commit()


async def db_get_recurring(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, name, amount, category, day_of_month
               FROM recurring_expenses WHERE user_id=?
               ORDER BY day_of_month""",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


async def db_delete_recurring(rec_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM recurring_expenses WHERE id=? AND user_id=?",
            (rec_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def db_process_recurring() -> list[tuple[int, str, float, str]]:
    now = datetime.now()
    current_month = f"{now.year}-{now.month:02d}"
    today = now.day
    recorded = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, user_id, name, amount, category
               FROM recurring_expenses
               WHERE day_of_month <= ?
                 AND (last_recorded IS NULL OR last_recorded != ?)""",
            (today, current_month),
        ) as cur:
            rows = await cur.fetchall()
        for rec_id, user_id, name, amount, category in rows:
            await db.execute(
                """INSERT INTO expenses
                   (user_id, name, amount, category, created_at)
                   VALUES (?,?,?,?,?)""",
                (user_id, name, amount, category, now.isoformat()),
            )
            await db.execute(
                "UPDATE recurring_expenses SET last_recorded=? WHERE id=?",
                (current_month, rec_id),
            )
            recorded.append((user_id, name, amount, category))
        await db.commit()
    return recorded


# ── DB: Бюджеты ──────────────────────────────────────────────────────────────

async def db_set_budget(user_id: int, category: str, limit_amount: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO budgets (user_id, category, limit_amount) VALUES (?,?,?)
               ON CONFLICT(user_id, category) DO UPDATE SET limit_amount=?""",
            (user_id, category, limit_amount, limit_amount),
        )
        await db.commit()


async def db_get_budgets(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, category, limit_amount FROM budgets WHERE user_id=? ORDER BY category",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


async def db_delete_budget(budget_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM budgets WHERE id=? AND user_id=?", (budget_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def db_check_budget(user_id: int, category: str) -> str | None:
    now = datetime.now()
    month_start = f"{now.year}-{now.month:02d}-01"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT limit_amount FROM budgets WHERE user_id=? AND category=?",
            (user_id, category),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        limit_amount = row[0]
        async with db.execute(
            """SELECT COALESCE(SUM(amount),0) FROM expenses
               WHERE user_id=? AND category=? AND created_at>=?""",
            (user_id, category, month_start),
        ) as cur:
            spent = (await cur.fetchone())[0]
    pct = spent / limit_amount * 100 if limit_amount else 0
    if pct >= 100:
        return f"🚨 <b>Бюджет превышен!</b> {category}: {spent:,.0f}/{limit_amount:,.0f} ₽ ({pct:.0f}%)"
    if pct >= 80:
        return f"⚠️ <b>Бюджет на исходе!</b> {category}: {spent:,.0f}/{limit_amount:,.0f} ₽ ({pct:.0f}%)"
    return None


# ── DB: Кошельки ─────────────────────────────────────────────────────────────

async def db_create_wallet(name: str, owner_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO wallets (name, created_at) VALUES (?,?)",
            (name, datetime.now().isoformat()),
        )
        wallet_id = cur.lastrowid
        await db.execute(
            "INSERT INTO wallet_members (wallet_id, user_id, role) VALUES (?,?,'owner')",
            (wallet_id, owner_id),
        )
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        await db.execute(
            "INSERT INTO wallet_invites (code, wallet_id, created_at) VALUES (?,?,?)",
            (code, wallet_id, datetime.now().isoformat()),
        )
        await db.commit()
    return wallet_id


async def db_get_user_wallet(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT w.id, w.name, wm.role FROM wallets w
               JOIN wallet_members wm ON w.id = wm.wallet_id
               WHERE wm.user_id=?""",
            (user_id,),
        ) as cur:
            return await cur.fetchone()


async def db_wallet_members(wallet_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, role FROM wallet_members WHERE wallet_id=?",
            (wallet_id,),
        ) as cur:
            return await cur.fetchall()


async def db_get_invite_code(wallet_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT code FROM wallet_invites WHERE wallet_id=? LIMIT 1",
            (wallet_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""


async def db_join_wallet(code: str, user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT wallet_id FROM wallet_invites WHERE code=?", (code.upper(),)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        wallet_id = row[0]
        existing = await db_get_user_wallet(user_id)
        if existing:
            return None
        try:
            await db.execute(
                "INSERT INTO wallet_members (wallet_id, user_id, role) VALUES (?,?,'member')",
                (wallet_id, user_id),
            )
            await db.commit()
        except Exception:
            return None
        async with db.execute("SELECT name FROM wallets WHERE id=?", (wallet_id,)) as cur:
            r = await cur.fetchone()
            return r[0] if r else "Кошелёк"


async def db_leave_wallet(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM wallet_members WHERE user_id=?", (user_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def db_add_expense_wallet(user_id: int, name: str, amount: float, category: str,
                                wallet_id: int | None = None,
                                original_amount: float | None = None,
                                original_currency: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO expenses
               (user_id, name, amount, category, created_at,
                original_amount, original_currency, wallet_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (user_id, name, amount, category, datetime.now().isoformat(),
             original_amount, original_currency, wallet_id),
        )
        await db.commit()


async def db_wallet_month_stats(wallet_id: int):
    now = datetime.now()
    month_start = f"{now.year}-{now.month:02d}-01"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT category, SUM(amount) FROM expenses
               WHERE wallet_id=? AND created_at>=?
               GROUP BY category ORDER BY SUM(amount) DESC""",
            (wallet_id, month_start),
        ) as cur:
            return await cur.fetchall()


# ── DB: Аналитика ────────────────────────────────────────────────────────────

async def db_month_stats_for(user_id: int, year: int, month: int):
    month_start = f"{year}-{month:02d}-01"
    if month == 12:
        month_end = f"{year + 1}-01-01"
    else:
        month_end = f"{year}-{month + 1:02d}-01"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT category, SUM(amount) FROM expenses
               WHERE user_id=? AND created_at>=? AND created_at<?
               GROUP BY category ORDER BY SUM(amount) DESC""",
            (user_id, month_start, month_end),
        ) as cur:
            return await cur.fetchall()


async def db_year_by_month(user_id: int, year: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT CAST(SUBSTR(created_at, 6, 2) AS INTEGER) as m, SUM(amount)
               FROM expenses
               WHERE user_id=? AND created_at>=? AND created_at<?
               GROUP BY m ORDER BY m""",
            (user_id, f"{year}-01-01", f"{year + 1}-01-01"),
        ) as cur:
            return await cur.fetchall()


async def db_revoke_pro(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
        await db.commit()
        return cur.rowcount > 0


async def db_admin_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM expenses") as c:
            total_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM subscriptions") as c:
            pro_users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM expenses") as c:
            row = await c.fetchone()
            total_expenses, total_sum = row[0], row[1]
        now = datetime.now()
        month_start = f"{now.year}-{now.month:02d}-01"
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM expenses WHERE created_at>=?",
            (month_start,),
        ) as c:
            row = await c.fetchone()
            month_expenses, month_sum = row[0], row[1]
    return {
        "total_users": total_users,
        "pro_users": pro_users,
        "total_expenses": total_expenses,
        "total_sum": total_sum,
        "month_expenses": month_expenses,
        "month_sum": month_sum,
    }


async def db_admin_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT e.user_id, COUNT(*) as cnt, SUM(e.amount) as total,
                   CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END as is_pro
            FROM expenses e
            LEFT JOIN subscriptions s ON e.user_id = s.user_id
            GROUP BY e.user_id ORDER BY total DESC
        """) as cur:
            return await cur.fetchall()


# ── Утилиты ───────────────────────────────────────────────────────────────────

_SYM_CHARS = re.escape("".join(CURRENCY_MAP.keys()))
_EXPENSE_RE = re.compile(
    rf"^(.+?)\s+([{_SYM_CHARS}]?)(\d+(?:[.,]\d+)?)([{_SYM_CHARS}]?)\s*$"
)


def parse_expense(text: str) -> tuple[str | None, float | None, str]:
    """Разбирает 'название сумма' с опциональным символом валюты."""
    m = _EXPENSE_RE.match(text.strip())
    if not m:
        return None, None, "RUB"
    sym = m.group(2) or m.group(4)
    currency = CURRENCY_MAP.get(sym, "RUB")
    try:
        amount = float(m.group(3).replace(",", "."))
        return m.group(1).strip(), amount, currency
    except ValueError:
        return None, None, "RUB"


async def get_rate_to_rub(currency: str) -> float | None:
    if currency == "RUB":
        return 1.0
    now = datetime.now().timestamp()
    if now - _rates_cache["ts"] > 21600:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://open.er-api.com/v6/latest/USD", timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    _rates_cache["rates"] = data["rates"]
                    _rates_cache["ts"] = now
        except Exception as e:
            log.error("Ошибка получения курсов: %s", e)
            if not _rates_cache["rates"]:
                return None
    rates = _rates_cache["rates"]
    if currency not in rates or "RUB" not in rates:
        return None
    return rates["RUB"] / rates[currency]


def pro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=PRO_BUTTON_TEXT, callback_data="buy_pro")]
    ])


def category_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    rows = []
    pair: list[InlineKeyboardButton] = []
    for cat in categories:
        pair.append(InlineKeyboardButton(text=cat, callback_data=f"cat:{cat}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="➕ Своя категория", callback_data="cat:__new__")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Хэндлеры команд ───────────────────────────────────────────────────────────

async def cmd_start(message: Message) -> None:
    is_pro = await db_is_pro(message.from_user.id)
    pro_line = "⭐ <b>Pro активирован</b>\n\n" if is_pro else ""
    text = (
        "👋 <b>Бот учёта расходов</b>\n\n"
        f"{pro_line}"
        "Пиши расход в формате:\n"
        "  <code>название сумма</code>\n\n"
        "Примеры:\n"
        "  <code>кофе 300</code>\n"
        "  <code>такси 1000</code>\n"
        "  <code>продукты в пятёрке 2500</code>\n\n"
        "Команды:\n"
        "  /stats — статистика за месяц\n"
        "  /history — последние 10 записей\n"
        "  /undo — отменить последнюю запись\n"
        "  /categories — мои категории\n"
        "  /add — добавить свою категорию\n"
        "  /regular — регулярные расходы ⭐\n"
        "  /budget — бюджеты по категориям ⭐\n"
        "  /compare — сравнение месяцев ⭐\n"
        "  /year — годовая сводка ⭐\n"
        "  /wallet — совместный учёт ⭐\n"
        "  /export — экспорт расходов ⭐\n"
        "  /limit — дневные/недельные лимиты ⭐\n"
        "  /weekday — аналитика по дням недели ⭐\n"
        "  /goal — цели накоплений ⭐"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    if not is_pro:
        await message.answer(
            "Разблокируй все функции:", reply_markup=pro_keyboard()
        )


async def cmd_stats(message: Message) -> None:
    rows = await db_month_stats(message.from_user.id)
    if not rows:
        await message.answer("За этот месяц расходов нет.")
        return

    now = datetime.now()
    total = sum(r[1] for r in rows)
    lines = [f"📊 <b>{MONTHS_RU[now.month]} {now.year}</b>\n"]
    for cat, amount in rows:
        pct = amount / total * 100
        lines.append(f"• {cat}  <b>{amount:,.0f} ₽</b>  <i>({pct:.0f}%)</i>")
    lines.append(f"\n💰 Итого: <b>{total:,.0f} ₽</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def cmd_history(message: Message) -> None:
    rows = await db_last_expenses(message.from_user.id)
    if not rows:
        await message.answer("Расходов пока нет.")
        return

    lines = ["📋 <b>Последние записи:</b>\n"]
    for row in rows:
        name, amount, category, created_at = row[0], row[1], row[2], row[3]
        orig_amount, orig_currency = row[4], row[5]
        dt = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
        if orig_currency and orig_currency != "RUB":
            sym = CURRENCY_SYMBOL.get(orig_currency, orig_currency)
            price = f"{orig_amount:,.2f} {sym} (≈ {amount:,.0f} ₽)"
        else:
            price = f"{amount:,.0f} ₽"
        lines.append(f"<i>{dt}</i>  {category}\n  <b>{name}</b> — {price}\n")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def cmd_undo(message: Message) -> None:
    deleted = await db_delete_last(message.from_user.id)
    if deleted:
        await message.answer("↩️ Последняя запись удалена.")
    else:
        await message.answer("Нечего удалять.")


async def cmd_add(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    # /add Название — сразу добавляем
    arg = text[len("/add"):].strip()
    if arg:
        if len(arg) > 40:
            await message.answer("Слишком длинное название (макс. 40 символов).")
            return
        await db_add_category(message.from_user.id, arg)
        await message.answer(f"✅ Категория «{arg}» добавлена!")
        return
    # /add без аргумента — спрашиваем имя
    await state.set_state(Form.adding_category_cmd)
    await message.answer("Введи название новой категории:")


async def handle_adding_category_cmd(message: Message, state: FSMContext) -> None:
    cat_name = (message.text or "").strip()
    if not cat_name or len(cat_name) > 40:
        await message.answer("Название должно быть от 1 до 40 символов. Попробуй ещё раз:")
        return
    await db_add_category(message.from_user.id, cat_name)
    await state.clear()
    await message.answer(f"✅ Категория «{cat_name}» добавлена!")


async def cmd_categories(message: Message) -> None:
    cats = await db_get_categories(message.from_user.id)
    custom = cats[len(DEFAULT_CATEGORIES):]
    lines = ["📁 <b>Стандартные категории:</b>"]
    for c in DEFAULT_CATEGORIES:
        lines.append(f"  • {c}")
    if custom:
        lines.append("\n<b>Мои категории:</b>")
        for c in custom:
            lines.append(f"  • {c}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ── Хэндлеры расходов (FSM) ───────────────────────────────────────────────────

async def handle_expense_input(message: Message, state: FSMContext) -> None:
    name, amount, currency = parse_expense(message.text or "")
    if name is None:
        await message.answer(
            "Не понял 🤔 Напиши расход так:\n<code>название сумма</code>\n\n"
            "Например: <code>кофе 300</code> или <code>кофе 5$</code>",
            parse_mode="HTML",
        )
        return

    original_amount = None
    original_currency = None

    if currency != "RUB":
        if not await db_is_pro(message.from_user.id):
            await message.answer(
                "🔒 Мультивалютность доступна в Pro.\n"
                "Напиши сумму в рублях или оформи подписку.",
                reply_markup=pro_keyboard(),
            )
            return
        rate = await get_rate_to_rub(currency)
        if rate is None:
            await message.answer("Не удалось получить курс валют. Попробуй позже.")
            return
        original_amount = amount
        original_currency = currency
        amount = round(amount * rate, 2)
        sym = CURRENCY_SYMBOL.get(currency, currency)
        display = (
            f"💸 <b>{name}</b> — {original_amount:,.2f} {sym}"
            f" (≈ {amount:,.0f} ₽)\n\nВыбери категорию:"
        )
    else:
        display = f"💸 <b>{name}</b> — {amount:,.0f} ₽\n\nВыбери категорию:"

    cats = await db_get_categories(message.from_user.id)
    await state.set_state(Form.choosing_category)
    await state.update_data(
        name=name, amount=amount,
        original_amount=original_amount, original_currency=original_currency,
    )
    await message.answer(display, reply_markup=category_keyboard(cats), parse_mode="HTML")


async def callback_choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.removeprefix("cat:")

    if cat == "__new__":
        await state.set_state(Form.adding_category)
        await callback.message.edit_text("Введи название новой категории:")
        await callback.answer()
        return

    data = await state.get_data()
    await state.update_data(category=cat)

    wallet = await db_get_user_wallet(callback.from_user.id)
    if wallet and await db_is_pro(callback.from_user.id):
        await state.set_state(Form.choosing_scope)
        await callback.message.edit_text(
            "Куда записать расход?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="👤 Личный", callback_data="scope:personal"),
                    InlineKeyboardButton(text="👥 Общий", callback_data="scope:shared"),
                ],
            ]),
        )
        await callback.answer()
        return

    await _save_expense(callback, state)


async def callback_choose_scope(callback: CallbackQuery, state: FSMContext) -> None:
    scope = callback.data.removeprefix("scope:")
    if scope == "shared":
        wallet = await db_get_user_wallet(callback.from_user.id)
        if wallet:
            await state.update_data(wallet_id=wallet[0])
    await _save_expense(callback, state)


async def _save_expense(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    name, amount = data.get("name"), data.get("amount")
    cat = data.get("category")
    orig_amount = data.get("original_amount")
    orig_currency = data.get("original_currency")
    wallet_id = data.get("wallet_id")
    await state.clear()

    await db_add_expense_wallet(
        callback.from_user.id, name, amount, cat,
        wallet_id, orig_amount, orig_currency,
    )
    if orig_currency:
        sym = CURRENCY_SYMBOL.get(orig_currency, orig_currency)
        price = f"{orig_amount:,.2f} {sym} (≈ {amount:,.0f} ₽)"
    else:
        price = f"{amount:,.0f} ₽"
    scope_label = " (👥 общий)" if wallet_id else ""
    await callback.message.edit_text(
        f"✅ <b>Записано!{scope_label}</b>\n\n{cat}\n<b>{name}</b> — {price}",
        parse_mode="HTML",
    )
    await callback.answer()

    warning = await db_check_budget(callback.from_user.id, cat)
    if warning:
        await callback.message.answer(warning, parse_mode="HTML")

    limit_warning = await db_check_limits(callback.from_user.id)
    if limit_warning:
        await callback.message.answer(limit_warning)


async def handle_new_category_name(message: Message, state: FSMContext) -> None:
    cat_name = (message.text or "").strip()
    if not cat_name or len(cat_name) > 40:
        await message.answer("Название должно быть от 1 до 40 символов. Попробуй ещё раз:")
        return

    data = await state.get_data()
    name, amount = data.get("name"), data.get("amount")

    await db_add_category(message.from_user.id, cat_name)
    await state.clear()

    if name and amount:
        orig_amount = data.get("original_amount")
        orig_currency = data.get("original_currency")
        await db_add_expense(message.from_user.id, name, amount, cat_name,
                             orig_amount, orig_currency)
        if orig_currency:
            sym = CURRENCY_SYMBOL.get(orig_currency, orig_currency)
            price = f"{orig_amount:,.2f} {sym} (≈ {amount:,.0f} ₽)"
        else:
            price = f"{amount:,.0f} ₽"
        await message.answer(
            f"✅ Категория «{cat_name}» создана и расход записан!\n\n"
            f"{cat_name}\n<b>{name}</b> — {price}",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"✅ Категория «{cat_name}» создана!")


# ── Регулярные расходы (Pro) ───────────────────────────────────────────────────

def day_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data="rday:1"),
            InlineKeyboardButton(text="5", callback_data="rday:5"),
            InlineKeyboardButton(text="10", callback_data="rday:10"),
            InlineKeyboardButton(text="15", callback_data="rday:15"),
        ],
        [
            InlineKeyboardButton(text="20", callback_data="rday:20"),
            InlineKeyboardButton(text="25", callback_data="rday:25"),
            InlineKeyboardButton(text="28", callback_data="rday:28"),
        ],
    ])


def regular_category_kb(categories: list[str]) -> InlineKeyboardMarkup:
    rows = []
    pair: list[InlineKeyboardButton] = []
    for cat in categories:
        pair.append(InlineKeyboardButton(text=cat, callback_data=f"rcat:{cat}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cmd_regular(message: Message, state: FSMContext) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer(
            "🔒 Регулярные расходы доступны в Pro.",
            reply_markup=pro_keyboard(),
        )
        return

    items = await db_get_recurring(message.from_user.id)
    if not items:
        lines = ["🔄 <b>Регулярные расходы</b>\n\nПока пусто."]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="reg:add")]
        ])
    else:
        lines = ["🔄 <b>Регулярные расходы</b>\n"]
        buttons = []
        for rec_id, name, amount, category, day in items:
            lines.append(f"• <b>{name}</b> — {amount:,.0f} ₽  [{category}]  каждое {day}-е число")
            buttons.append([InlineKeyboardButton(
                text=f"🗑 {name}", callback_data=f"reg:del:{rec_id}"
            )])
        buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="reg:add")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


async def callback_regular(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data.removeprefix("reg:")

    if data == "add":
        await state.set_state(Form.regular_input)
        await callback.message.edit_text(
            "Напиши регулярный расход:\n<code>название сумма</code>\n\n"
            "Например: <code>аренда 45000</code>",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if data.startswith("del:"):
        rec_id = int(data.removeprefix("del:"))
        deleted = await db_delete_recurring(rec_id, callback.from_user.id)
        if deleted:
            await callback.answer("Удалено!")
        else:
            await callback.answer("Не найдено.", show_alert=True)
            return
        items = await db_get_recurring(callback.from_user.id)
        if not items:
            text = "🔄 <b>Регулярные расходы</b>\n\nПока пусто."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить", callback_data="reg:add")]
            ])
        else:
            lines = ["🔄 <b>Регулярные расходы</b>\n"]
            buttons = []
            for rid, name, amount, category, day in items:
                lines.append(f"• <b>{name}</b> — {amount:,.0f} ₽  [{category}]  каждое {day}-е число")
                buttons.append([InlineKeyboardButton(
                    text=f"🗑 {name}", callback_data=f"reg:del:{rid}"
                )])
            buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="reg:add")])
            text = "\n".join(lines)
            kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


async def handle_regular_input(message: Message, state: FSMContext) -> None:
    name, amount, _ = parse_expense(message.text or "")
    if name is None:
        await message.answer(
            "Не понял. Напиши так: <code>аренда 45000</code>", parse_mode="HTML"
        )
        return
    cats = await db_get_categories(message.from_user.id)
    await state.update_data(reg_name=name, reg_amount=amount)
    await state.set_state(Form.regular_category)
    await message.answer(
        f"🔄 <b>{name}</b> — {amount:,.0f} ₽\n\nВыбери категорию:",
        reply_markup=regular_category_kb(cats),
        parse_mode="HTML",
    )


async def callback_regular_category(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.removeprefix("rcat:")
    await state.update_data(reg_category=cat)
    await state.set_state(Form.regular_day)
    await callback.message.edit_text(
        f"Какого числа списывать каждый месяц?",
        reply_markup=day_keyboard(),
    )
    await callback.answer()


async def callback_regular_day(callback: CallbackQuery, state: FSMContext) -> None:
    day = int(callback.data.removeprefix("rday:"))
    data = await state.get_data()
    name = data["reg_name"]
    amount = data["reg_amount"]
    category = data["reg_category"]

    await db_add_recurring(callback.from_user.id, name, amount, category, day)
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Регулярный расход создан!</b>\n\n"
        f"{category}\n<b>{name}</b> — {amount:,.0f} ₽\n"
        f"Каждое {day}-е число месяца",
        parse_mode="HTML",
    )
    await callback.answer()


async def recurring_scheduler(bot: Bot) -> None:
    while True:
        try:
            recorded = await db_process_recurring()
            for user_id, name, amount, category in recorded:
                try:
                    await bot.send_message(
                        user_id,
                        f"🔄 Автоматически записан регулярный расход:\n"
                        f"{category}\n<b>{name}</b> — {amount:,.0f} ₽",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            if recorded:
                log.info("Записано %d регулярных расходов", len(recorded))
        except Exception as e:
            log.error("Ошибка recurring_scheduler: %s", e)
        await asyncio.sleep(3600)


# ── Бюджеты (Pro) ─────────────────────────────────────────────────────────────

def budget_category_kb(categories: list[str]) -> InlineKeyboardMarkup:
    rows = []
    pair: list[InlineKeyboardButton] = []
    for cat in categories:
        pair.append(InlineKeyboardButton(text=cat, callback_data=f"bcat:{cat}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cmd_budget(message: Message) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Бюджеты доступны в Pro.", reply_markup=pro_keyboard())
        return
    budgets = await db_get_budgets(message.from_user.id)
    now = datetime.now()
    month_start = f"{now.year}-{now.month:02d}-01"
    if not budgets:
        text = "📏 <b>Бюджеты</b>\n\nПока не установлены."
    else:
        lines = [f"📏 <b>Бюджеты — {MONTHS_RU[now.month]}</b>\n"]
        async with aiosqlite.connect(DB_PATH) as db:
            for bid, cat, limit_amt in budgets:
                async with db.execute(
                    """SELECT COALESCE(SUM(amount),0) FROM expenses
                       WHERE user_id=? AND category=? AND created_at>=?""",
                    (message.from_user.id, cat, month_start),
                ) as cur:
                    spent = (await cur.fetchone())[0]
                pct = spent / limit_amt * 100 if limit_amt else 0
                bar_len = min(int(pct / 10), 10)
                bar = "▓" * bar_len + "░" * (10 - bar_len)
                icon = "🚨" if pct >= 100 else "⚠️" if pct >= 80 else "✅"
                lines.append(
                    f"{icon} {cat}\n"
                    f"  {bar} {pct:.0f}%\n"
                    f"  {spent:,.0f} / {limit_amt:,.0f} ₽\n"
                )
        text = "\n".join(lines)
    buttons = []
    if budgets:
        for bid, cat, _ in budgets:
            buttons.append([InlineKeyboardButton(
                text=f"🗑 {cat}", callback_data=f"bdel:{bid}"
            )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить бюджет", callback_data="badd")])
    await message.answer(text, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def callback_budget_add(callback: CallbackQuery, state: FSMContext) -> None:
    cats = await db_get_categories(callback.from_user.id)
    await state.set_state(Form.budget_category)
    await callback.message.edit_text(
        "Выбери категорию для бюджета:", reply_markup=budget_category_kb(cats)
    )
    await callback.answer()


async def callback_budget_cat(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.removeprefix("bcat:")
    await state.update_data(budget_cat=cat)
    await state.set_state(Form.budget_amount)
    await callback.message.edit_text(
        f"Введи месячный лимит для <b>{cat}</b> (в рублях):", parse_mode="HTML"
    )
    await callback.answer()


async def handle_budget_amount(message: Message, state: FSMContext) -> None:
    try:
        limit_amt = float((message.text or "").strip().replace(",", "."))
        if limit_amt <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное число.")
        return
    data = await state.get_data()
    cat = data["budget_cat"]
    await db_set_budget(message.from_user.id, cat, limit_amt)
    await state.clear()
    await message.answer(
        f"✅ Бюджет установлен: <b>{cat}</b> — {limit_amt:,.0f} ₽/мес",
        parse_mode="HTML",
    )


async def callback_budget_del(callback: CallbackQuery) -> None:
    bid = int(callback.data.removeprefix("bdel:"))
    deleted = await db_delete_budget(bid, callback.from_user.id)
    await callback.answer("Удалено!" if deleted else "Не найдено.")


# ── Аналитика (Pro) ──────────────────────────────────────────────────────────

async def cmd_compare(message: Message) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Аналитика доступна в Pro.", reply_markup=pro_keyboard())
        return
    now = datetime.now()
    cur_y, cur_m = now.year, now.month
    prev_m = cur_m - 1 if cur_m > 1 else 12
    prev_y = cur_y if cur_m > 1 else cur_y - 1

    cur_data = dict(await db_month_stats_for(message.from_user.id, cur_y, cur_m))
    prev_data = dict(await db_month_stats_for(message.from_user.id, prev_y, prev_m))

    if not cur_data and not prev_data:
        await message.answer("Недостаточно данных для сравнения.")
        return

    all_cats = sorted(set(list(cur_data.keys()) + list(prev_data.keys())))
    cur_total = sum(cur_data.values())
    prev_total = sum(prev_data.values())

    lines = [
        f"📊 <b>{MONTHS_RU[prev_m]} → {MONTHS_RU[cur_m]}</b>\n"
    ]
    for cat in all_cats:
        c = cur_data.get(cat, 0)
        p = prev_data.get(cat, 0)
        if p > 0:
            diff_pct = (c - p) / p * 100
            arrow = "📈" if diff_pct > 0 else "📉"
            lines.append(
                f"• {cat}\n"
                f"  {p:,.0f} → {c:,.0f} ₽  {arrow} {diff_pct:+.0f}%"
            )
        elif c > 0:
            lines.append(f"• {cat}\n  0 → {c:,.0f} ₽  🆕")

    diff_total = ((cur_total - prev_total) / prev_total * 100) if prev_total else 0
    arrow_total = "📈" if diff_total > 0 else "📉"
    lines.append(
        f"\n💰 <b>Итого:</b> {prev_total:,.0f} → {cur_total:,.0f} ₽"
        f"  {arrow_total} {diff_total:+.0f}%"
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


async def cmd_year(message: Message) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Аналитика доступна в Pro.", reply_markup=pro_keyboard())
        return
    now = datetime.now()
    rows = await db_year_by_month(message.from_user.id, now.year)
    if not rows:
        await message.answer(f"За {now.year} год расходов нет.")
        return

    total = sum(r[1] for r in rows)
    max_amount = max(r[1] for r in rows)
    lines = [f"📅 <b>{now.year} год</b>\n"]
    for month_num, amount in rows:
        bar_len = int(amount / max_amount * 10) if max_amount else 0
        bar = "▓" * bar_len + "░" * (10 - bar_len)
        lines.append(f"  {MONTHS_RU[month_num][:3]}  {bar}  {amount:,.0f} ₽")
    lines.append(f"\n💰 Итого: <b>{total:,.0f} ₽</b>")
    lines.append(f"📊 Среднее: <b>{total / len(rows):,.0f} ₽/мес</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ── Совместный учёт (Pro) ────────────────────────────────────────────────────

async def cmd_wallet(message: Message, state: FSMContext) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Совместный учёт доступен в Pro.", reply_markup=pro_keyboard())
        return
    wallet = await db_get_user_wallet(message.from_user.id)
    if not wallet:
        await message.answer(
            "👥 <b>Совместный учёт</b>\n\nТы пока не в кошельке.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать кошелёк", callback_data="wal:create")],
                [InlineKeyboardButton(text="🔑 Присоединиться", callback_data="wal:join")],
            ]),
        )
        return
    wallet_id, wallet_name, role = wallet
    members = await db_wallet_members(wallet_id)
    code = await db_get_invite_code(wallet_id)
    stats = await db_wallet_month_stats(wallet_id)

    lines = [f"👥 <b>{wallet_name}</b>\n"]
    lines.append(f"Участников: {len(members)}")
    if code:
        lines.append(f"Код приглашения: <code>{code}</code>\n")
    if stats:
        total = sum(r[1] for r in stats)
        lines.append(f"<b>{MONTHS_RU[datetime.now().month]}:</b>")
        for cat, amount in stats:
            lines.append(f"  • {cat}: {amount:,.0f} ₽")
        lines.append(f"  💰 Итого: <b>{total:,.0f} ₽</b>")
    else:
        lines.append("За этот месяц общих расходов нет.")

    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚪 Покинуть кошелёк", callback_data="wal:leave")],
        ]),
    )


async def callback_wallet(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.removeprefix("wal:")

    if action == "create":
        await state.set_state(Form.wallet_create)
        await callback.message.edit_text("Введи название кошелька (например: Семья):")
        await callback.answer()

    elif action == "join":
        await state.set_state(Form.wallet_join)
        await callback.message.edit_text("Введи код приглашения (6 символов):")
        await callback.answer()

    elif action == "leave":
        left = await db_leave_wallet(callback.from_user.id)
        if left:
            await callback.message.edit_text("✅ Ты покинул кошелёк.")
        else:
            await callback.answer("Ты не в кошельке.", show_alert=True)
        await callback.answer()


async def handle_wallet_create(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 30:
        await message.answer("Название от 1 до 30 символов. Попробуй ещё раз:")
        return
    existing = await db_get_user_wallet(message.from_user.id)
    if existing:
        await state.clear()
        await message.answer("Ты уже в кошельке. Сначала покинь его.")
        return
    wallet_id = await db_create_wallet(name, message.from_user.id)
    code = await db_get_invite_code(wallet_id)
    await state.clear()
    await message.answer(
        f"✅ Кошелёк «{name}» создан!\n\n"
        f"Код приглашения: <code>{code}</code>\n"
        f"Отправь этот код партнёру.",
        parse_mode="HTML",
    )


async def handle_wallet_join(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer("Введи код:")
        return
    wallet_name = await db_join_wallet(code, message.from_user.id)
    await state.clear()
    if wallet_name:
        await message.answer(f"✅ Ты присоединился к «{wallet_name}»!")
    else:
        await message.answer("Код не найден или ты уже в кошельке.")


# ── DB: Лимиты ──────────────────────────────────────────────────────────────

async def db_set_limit(user_id: int, daily: float | None, weekly: float | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO daily_limits (user_id, daily, weekly) VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET daily=?, weekly=?""",
            (user_id, daily, weekly, daily, weekly),
        )
        await db.commit()


async def db_get_limit(user_id: int) -> tuple[float | None, float | None]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT daily, weekly FROM daily_limits WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


async def db_delete_limit(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM daily_limits WHERE user_id=?", (user_id,))
        await db.commit()
        return cur.rowcount > 0


async def db_spent_today(user_id: int) -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND created_at>=?",
            (user_id, today),
        ) as cur:
            return (await cur.fetchone())[0]


async def db_spent_this_week(user_id: int) -> float:
    now = datetime.now()
    monday = now.date() - __import__("datetime").timedelta(days=now.weekday())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND created_at>=?",
            (user_id, monday.isoformat()),
        ) as cur:
            return (await cur.fetchone())[0]


async def db_check_limits(user_id: int) -> str | None:
    daily, weekly = await db_get_limit(user_id)
    if not daily and not weekly:
        return None
    parts = []
    if daily:
        spent = await db_spent_today(user_id)
        pct = spent / daily * 100
        if pct >= 100:
            parts.append(f"🚨 Дневной лимит превышен! {spent:,.0f}/{daily:,.0f} ₽ ({pct:.0f}%)")
        elif pct >= 80:
            parts.append(f"⚠️ Дневной лимит на исходе: {spent:,.0f}/{daily:,.0f} ₽ ({pct:.0f}%)")
    if weekly:
        spent = await db_spent_this_week(user_id)
        pct = spent / weekly * 100
        if pct >= 100:
            parts.append(f"🚨 Недельный лимит превышен! {spent:,.0f}/{weekly:,.0f} ₽ ({pct:.0f}%)")
        elif pct >= 80:
            parts.append(f"⚠️ Недельный лимит на исходе: {spent:,.0f}/{weekly:,.0f} ₽ ({pct:.0f}%)")
    return "\n".join(parts) if parts else None


# ── DB: Аналитика по дням недели ─────────────────────────────────────────────

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def db_weekday_stats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT created_at, amount, category FROM expenses
               WHERE user_id=? ORDER BY created_at""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return None, None, None
    by_day = {i: 0.0 for i in range(7)}
    by_day_cat: dict[int, dict[str, float]] = {i: {} for i in range(7)}
    for created_at, amount, category in rows:
        wd = datetime.fromisoformat(created_at).weekday()
        by_day[wd] += amount
        by_day_cat[wd][category] = by_day_cat[wd].get(category, 0) + amount
    top_day = max(by_day, key=by_day.get)
    top_cats = sorted(by_day_cat[top_day].items(), key=lambda x: -x[1])[:3]
    return by_day, top_day, top_cats


# ── DB: Цели накоплений ─────────────────────────────────────────────────────

async def db_add_goal(user_id: int, name: str, target: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO goals (user_id, name, target, created_at) VALUES (?,?,?,?)",
            (user_id, name, target, datetime.now().isoformat()),
        )
        await db.commit()


async def db_get_goals(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, target, saved, created_at FROM goals WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


async def db_add_to_goal(goal_id: int, user_id: int, amount: float) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE goals SET saved = saved + ? WHERE id=? AND user_id=?",
            (amount, goal_id, user_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def db_delete_goal(goal_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM goals WHERE id=? AND user_id=?", (goal_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def db_avg_monthly_expenses(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT MIN(created_at), MAX(created_at), SUM(amount)
               FROM expenses WHERE user_id=?""",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return 0
    first = datetime.fromisoformat(row[0])
    last = datetime.fromisoformat(row[1])
    months = max(1, (last.year - first.year) * 12 + last.month - first.month)
    return row[2] / months


# ── Лимиты (Pro) ─────────────────────────────────────────────────────────────

async def cmd_limit(message: Message, state: FSMContext) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Лимиты доступны в Pro.", reply_markup=pro_keyboard())
        return
    daily, weekly = await db_get_limit(message.from_user.id)
    if daily or weekly:
        lines = ["🚦 <b>Твои лимиты</b>\n"]
        if daily:
            spent = await db_spent_today(message.from_user.id)
            pct = spent / daily * 100 if daily else 0
            lines.append(f"📅 Дневной: {spent:,.0f} / {daily:,.0f} ₽ ({pct:.0f}%)")
        if weekly:
            spent = await db_spent_this_week(message.from_user.id)
            pct = spent / weekly * 100 if weekly else 0
            lines.append(f"📆 Недельный: {spent:,.0f} / {weekly:,.0f} ₽ ({pct:.0f}%)")
        await message.answer(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✏️ Изменить", callback_data="lim:set"),
                    InlineKeyboardButton(text="🗑 Убрать", callback_data="lim:del"),
                ],
            ]),
        )
    else:
        await message.answer(
            "🚦 <b>Лимиты</b>\n\n"
            "Установи дневной и/или недельный лимит.\n"
            "При каждом расходе бот покажет, сколько осталось.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Установить", callback_data="lim:set")],
            ]),
        )


async def callback_limit(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.removeprefix("lim:")
    if action == "set":
        await state.set_state(Form.limit_amount)
        await callback.message.edit_text(
            "Введи лимиты через пробел:\n"
            "<code>дневной недельный</code>\n\n"
            "Например: <code>3000 15000</code>\n"
            "Чтобы задать только один — поставь 0 вместо другого:\n"
            "<code>3000 0</code> или <code>0 20000</code>",
            parse_mode="HTML",
        )
    elif action == "del":
        await db_delete_limit(callback.from_user.id)
        await callback.message.edit_text("✅ Лимиты убраны.")
    await callback.answer()


async def handle_limit_amount(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").strip().split()
    try:
        if len(parts) == 1:
            daily = float(parts[0].replace(",", "."))
            weekly = None
        elif len(parts) >= 2:
            daily = float(parts[0].replace(",", "."))
            weekly = float(parts[1].replace(",", "."))
        else:
            raise ValueError
        daily = daily if daily > 0 else None
        weekly = weekly if weekly and weekly > 0 else None
        if not daily and not weekly:
            raise ValueError
    except ValueError:
        await message.answer("Введи одно или два положительных числа.")
        return
    await db_set_limit(message.from_user.id, daily, weekly)
    await state.clear()
    parts_text = []
    if daily:
        parts_text.append(f"📅 Дневной: {daily:,.0f} ₽")
    if weekly:
        parts_text.append(f"📆 Недельный: {weekly:,.0f} ₽")
    await message.answer(
        "✅ <b>Лимиты установлены!</b>\n\n" + "\n".join(parts_text),
        parse_mode="HTML",
    )


# ── Аналитика по дням недели (Pro) ──────────────────────────────────────────

def _weekday_html(by_day: dict[int, float], top_day: int,
                   top_cats: list[tuple[str, float]]) -> str:
    max_val = max(by_day.values()) or 1
    total = sum(by_day.values())
    avg = total / 7
    bars = ""
    for i in range(7):
        pct = by_day[i] / max_val * 100 if max_val else 0
        amount = f"{by_day[i]:,.0f}".replace(",", " ")
        is_top = "top" if i == top_day else ""
        bars += (
            f'<div class="bar-row">'
            f'<span class="day">{WEEKDAYS_RU[i]}</span>'
            f'<div class="bar-wrap">'
            f'<div class="bar {is_top}" style="width:{max(pct, 2):.1f}%"></div>'
            f'</div>'
            f'<span class="amount">{amount} ₽</span>'
            f'</div>\n'
        )
    cats_html = ""
    for cat, amount in top_cats:
        a = f"{amount:,.0f}".replace(",", " ")
        cats_html += f'<div class="cat-row">• {cat} — {a} ₽</div>\n'
    avg_str = f"{avg:,.0f}".replace(",", " ")
    total_str = f"{total:,.0f}".replace(",", " ")
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Расходы по дням недели</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:#0f0f0f; color:#e0e0e0; padding:24px; min-height:100vh; }}
  .card {{ background:#1a1a2e; border-radius:16px; padding:28px; max-width:520px;
           margin:0 auto; box-shadow:0 8px 32px rgba(0,0,0,0.4); }}
  h1 {{ font-size:20px; margin-bottom:4px; color:#fff; }}
  .subtitle {{ font-size:13px; color:#888; margin-bottom:24px; }}
  .bar-row {{ display:flex; align-items:center; margin-bottom:10px; gap:10px; }}
  .day {{ width:28px; font-size:13px; font-weight:600; color:#aaa; flex-shrink:0; }}
  .bar-wrap {{ flex:1; height:28px; background:#222; border-radius:6px; overflow:hidden; }}
  .bar {{ height:100%; background:linear-gradient(90deg,#6366f1,#818cf8);
          border-radius:6px; transition:width .3s; min-width:4px; }}
  .bar.top {{ background:linear-gradient(90deg,#f59e0b,#fbbf24); }}
  .amount {{ font-size:13px; width:90px; text-align:right; color:#ccc;
             font-variant-numeric:tabular-nums; flex-shrink:0; }}
  .insight {{ margin-top:24px; padding:16px; background:#16213e; border-radius:12px;
              border-left:4px solid #f59e0b; }}
  .insight-title {{ font-size:14px; font-weight:600; color:#fbbf24; margin-bottom:8px; }}
  .cat-row {{ font-size:13px; color:#ccc; margin-bottom:4px; }}
  .stats {{ display:flex; gap:16px; margin-top:20px; }}
  .stat {{ flex:1; padding:12px; background:#16213e; border-radius:10px; text-align:center; }}
  .stat-val {{ font-size:18px; font-weight:700; color:#fff; }}
  .stat-label {{ font-size:11px; color:#888; margin-top:4px; }}
  .footer {{ text-align:center; margin-top:20px; font-size:11px; color:#555; }}
</style>
</head>
<body>
<div class="card">
  <h1>📅 Расходы по дням недели</h1>
  <div class="subtitle">Вся история</div>
  {bars}
  <div class="insight">
    <div class="insight-title">🏆 Больше всего — {WEEKDAYS_RU[top_day]}</div>
    {cats_html}
  </div>
  <div class="stats">
    <div class="stat">
      <div class="stat-val">{total_str} ₽</div>
      <div class="stat-label">Всего</div>
    </div>
    <div class="stat">
      <div class="stat-val">{avg_str} ₽</div>
      <div class="stat-label">Среднее / день недели</div>
    </div>
  </div>
</div>
<div class="footer">Expense Bot · Pro Analytics</div>
</body>
</html>"""


async def cmd_weekday(message: Message, bot: Bot) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Аналитика доступна в Pro.", reply_markup=pro_keyboard())
        return
    by_day, top_day, top_cats = await db_weekday_stats(message.from_user.id)
    if not by_day:
        await message.answer("Недостаточно данных. Добавь расходы!")
        return
    html = _weekday_html(by_day, top_day, top_cats)
    await bot.send_document(
        message.from_user.id,
        BufferedInputFile(html.encode("utf-8"), filename="weekday_report.html"),
        caption="📅 Аналитика по дням недели — открой файл в браузере",
    )


# ── Цели накоплений (Pro) ────────────────────────────────────────────────────

async def cmd_goal(message: Message, state: FSMContext) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Цели доступны в Pro.", reply_markup=pro_keyboard())
        return
    goals = await db_get_goals(message.from_user.id)
    avg = await db_avg_monthly_expenses(message.from_user.id)
    if not goals:
        text = "🎯 <b>Цели накоплений</b>\n\nПока нет целей."
    else:
        lines = ["🎯 <b>Цели накоплений</b>\n"]
        for gid, name, target, saved, created_at in goals:
            remaining = max(0, target - saved)
            pct = saved / target * 100 if target else 0
            bar_len = min(int(pct / 10), 10)
            bar = "▓" * bar_len + "░" * (10 - bar_len)
            icon = "✅" if pct >= 100 else "🎯"
            lines.append(
                f"{icon} <b>{name}</b>\n"
                f"  {bar} {pct:.0f}%\n"
                f"  {saved:,.0f} / {target:,.0f} ₽\n"
            )
            if remaining > 0 and avg > 0:
                months_left = remaining / (avg * 0.1)
                if months_left < 1:
                    lines.append(f"  💡 При 10% экономии — меньше месяца\n")
                else:
                    lines.append(f"  💡 При 10% экономии — ~{months_left:.0f} мес.\n")
        text = "\n".join(lines)
    buttons = []
    if goals:
        for gid, name, target, saved, _ in goals:
            row = []
            if saved < target:
                row.append(InlineKeyboardButton(text=f"💰 +{name}", callback_data=f"goal:add:{gid}"))
            row.append(InlineKeyboardButton(text=f"🗑 {name}", callback_data=f"goal:del:{gid}"))
            buttons.append(row)
    buttons.append([InlineKeyboardButton(text="➕ Новая цель", callback_data="goal:new")])
    await message.answer(text, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def callback_goal(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data.removeprefix("goal:")

    if data == "new":
        await state.set_state(Form.goal_name)
        await callback.message.edit_text("Введи название цели (например: Отпуск):")
        await callback.answer()
        return

    if data.startswith("del:"):
        gid = int(data.removeprefix("del:"))
        deleted = await db_delete_goal(gid, callback.from_user.id)
        await callback.answer("Удалено!" if deleted else "Не найдено.")
        return

    if data.startswith("add:"):
        gid = int(data.removeprefix("add:"))
        await state.set_state(Form.goal_amount)
        await state.update_data(goal_id=gid)
        await callback.message.edit_text("Сколько отложить? Введи сумму в рублях:")
        await callback.answer()


async def handle_goal_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 40:
        await message.answer("Название от 1 до 40 символов:")
        return
    await state.update_data(goal_name=name)
    await state.set_state(Form.goal_amount)
    await message.answer(f"Какая цель в рублях для «{name}»?")


async def handle_goal_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float((message.text or "").strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное число.")
        return
    data = await state.get_data()
    goal_id = data.get("goal_id")
    if goal_id:
        await db_add_to_goal(goal_id, message.from_user.id, amount)
        await state.clear()
        await message.answer(f"✅ Отложено {amount:,.0f} ₽!")
    else:
        name = data["goal_name"]
        await db_add_goal(message.from_user.id, name, amount)
        await state.clear()
        await message.answer(
            f"✅ Цель «{name}» создана: {amount:,.0f} ₽",
            parse_mode="HTML",
        )


# ── Экспорт (Pro) ────────────────────────────────────────────────────────────

async def db_all_expenses(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT name, amount, category, created_at,
                      original_amount, original_currency
               FROM expenses WHERE user_id=? ORDER BY created_at""",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


async def cmd_export(message: Message) -> None:
    if not await db_is_pro(message.from_user.id):
        await message.answer("🔒 Экспорт доступен в Pro.", reply_markup=pro_keyboard())
        return
    await message.answer(
        "📤 <b>Экспорт расходов</b>\n\nВыбери формат:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📄 CSV", callback_data="exp:csv"),
                InlineKeyboardButton(text="📋 JSON", callback_data="exp:json"),
            ],
            [
                InlineKeyboardButton(text="📊 Excel-CSV (;)", callback_data="exp:excel"),
            ],
        ]),
    )


async def callback_export(callback: CallbackQuery, bot: Bot) -> None:
    fmt = callback.data.removeprefix("exp:")
    rows = await db_all_expenses(callback.from_user.id)
    if not rows:
        await callback.answer("Расходов нет.", show_alert=True)
        return

    if fmt == "json":
        data = []
        for r in rows:
            entry = {
                "name": r[0], "amount": r[1], "category": r[2],
                "date": r[3],
            }
            if r[5]:
                entry["original_amount"] = r[4]
                entry["original_currency"] = r[5]
            data.append(entry)
        content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        filename = "expenses.json"

    else:
        buf = io.StringIO()
        sep = ";" if fmt == "excel" else ","
        writer = csv.writer(buf, delimiter=sep)
        writer.writerow(["Дата", "Название", "Сумма (₽)", "Категория",
                         "Оригинал", "Валюта"])
        for r in rows:
            dt = datetime.fromisoformat(r[3]).strftime("%Y-%m-%d %H:%M")
            writer.writerow([dt, r[0], r[1], r[2], r[4] or "", r[5] or ""])
        content = buf.getvalue().encode("utf-8-sig")
        filename = "expenses.csv"

    await bot.send_document(
        callback.from_user.id,
        BufferedInputFile(content, filename=filename),
        caption=f"📤 Экспорт: {len(rows)} записей",
    )
    await callback.answer()


# ── Канал: автопостинг ────────────────────────────────────────────────────────

CHANNEL_POSTS = [
    "💡 <b>Правило 50/30/20</b>\n\n"
    "50% дохода — на необходимое (жильё, еда, транспорт)\n"
    "30% — на желания (развлечения, покупки)\n"
    "20% — на накопления и долги\n\n"
    "Начни отслеживать свои категории прямо сейчас 👇",

    "☕ <b>Латте-фактор</b>\n\n"
    "Кофе за 300₽ каждый день = 9 000₽/мес = 108 000₽/год.\n\n"
    "Это не значит «не пей кофе». Это значит — знай, куда уходят деньги. "
    "Запиши свой первый расход — и удивись в конце месяца.",

    "📊 <b>Почему люди не копят?</b>\n\n"
    "Потому что не знают, сколько тратят. Серьёзно.\n\n"
    "Исследования показывают: люди, которые записывают расходы, "
    "тратят на 15-20% меньше уже в первый месяц.",

    "🎯 <b>Как поставить финансовую цель</b>\n\n"
    "1. Назови цель конкретно: не «накопить», а «отпуск в Турцию за 80 000₽»\n"
    "2. Раздели на месяцы: 80 000 / 8 мес = 10 000₽/мес\n"
    "3. Отслеживай прогресс\n\n"
    "Цель без плана — это просто мечта.",

    "🧠 <b>Эффект «фантомных денег»</b>\n\n"
    "Картой мы тратим на 12-18% больше, чем наличными. "
    "Мозг не «чувствует» цифровые траты.\n\n"
    "Решение: записывай каждый расход сразу после покупки. "
    "Это возвращает осознанность.",

    "📉 <b>Три расхода, которые незаметно съедают бюджет</b>\n\n"
    "1. Подписки, которыми не пользуешься\n"
    "2. Доставка еды вместо готовки\n"
    "3. Спонтанные покупки на маркетплейсах\n\n"
    "Проверь свою статистику за месяц — ты удивишься.",

    "💰 <b>Правило 24 часов</b>\n\n"
    "Хочешь купить что-то дороже 3 000₽? Подожди 24 часа.\n"
    "Если через сутки всё ещё хочешь — покупай.\n\n"
    "80% импульсивных покупок не переживают эту проверку.",

    "🔄 <b>Регулярные расходы — невидимый враг</b>\n\n"
    "Netflix + Spotify + YouTube Premium + iCloud + …\n"
    "Каждая подписка кажется мелочью, но вместе — тысячи в месяц.\n\n"
    "Запиши все свои подписки. Прямо сейчас. Потом спасибо скажешь.",

    "📅 <b>Знаешь свой самый дорогой день?</b>\n\n"
    "У большинства людей это пятница или суббота.\n"
    "Вечером расслабляешься — и кошелёк тоже.\n\n"
    "Узнай свой паттерн — и сможешь его контролировать.",

    "🏦 <b>Финансовая подушка</b>\n\n"
    "3-6 месячных расходов на экстренном счёте.\n"
    "Это не инвестиция, это страховка.\n\n"
    "Не знаешь, сколько тратишь в месяц? Начни записывать — и через 30 дней "
    "будешь знать точную цифру.",

    "⚡ <b>Метод конвертов (цифровой)</b>\n\n"
    "Раздели бюджет по категориям: Еда — 15 000, Транспорт — 5 000, Развлечения — 8 000.\n"
    "Как только категория исчерпана — стоп.\n\n"
    "Это именно то, для чего нужны бюджеты с лимитами.",

    "💡 <b>50₽ или 50 000₽?</b>\n\n"
    "Мы торгуемся за скидку на технику за 50 000₽, "
    "но не замечаем ежедневных трат по 50-200₽.\n\n"
    "А ведь 200₽ × 365 дней = 73 000₽. Больше, чем та техника.",

    "🎓 <b>Лучшая инвестиция</b>\n\n"
    "Это не крипта и не акции. Это привычка считать деньги.\n\n"
    "Люди, которые ведут учёт расходов хотя бы 3 месяца, "
    "в среднем увеличивают накопления на 30%.",

    "🌍 <b>Тратишь в разных валютах?</b>\n\n"
    "Путешествия, онлайн-покупки, подписки в долларах…\n"
    "Сложно понять реальные траты, когда они в 3 валютах.\n\n"
    "Автоматическая конвертация решает эту проблему.",

    "👥 <b>Ведёшь бюджет с партнёром?</b>\n\n"
    "Главная причина ссор из-за денег — "
    "«я не знал(а), что ты столько потратил(а)».\n\n"
    "Совместный учёт расходов — самый простой способ это исправить. "
    "Прозрачность = меньше конфликтов.",
]

_channel_post_index: dict = {"idx": 0, "running": False}


async def channel_scheduler(bot: Bot) -> None:
    _channel_post_index["running"] = True
    while _channel_post_index["running"]:
        try:
            idx = _channel_post_index["idx"] % len(CHANNEL_POSTS)
            text = CHANNEL_POSTS[idx]
            text += (
                "\n\n—\n"
                "🤖 <b>Бот для учёта расходов:</b> @tgbotexpensiveclaudecode_bot\n"
                "Пиши <code>кофе 300</code> — и контролируй финансы."
            )
            await bot.send_message(CHANNEL_USERNAME, text, parse_mode="HTML")
            _channel_post_index["idx"] = idx + 1
            log.info("Пост #%d опубликован в канал", idx + 1)
        except Exception as e:
            log.error("Ошибка постинга в канал: %s", e)
        await asyncio.sleep(43200)  # 12 часов


async def cmd_channel(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        f"📢 <b>Канал: {CHANNEL_USERNAME}</b>\n\n"
        f"Постов в очереди: {len(CHANNEL_POSTS)}\n"
        f"Следующий пост: #{_channel_post_index['idx'] % len(CHANNEL_POSTS) + 1}\n"
        f"Автопостинг: {'✅ вкл' if _channel_post_index['running'] else '❌ выкл'}\n"
        f"Интервал: каждые 12 часов",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Опубликовать сейчас", callback_data="ch:now")],
            [
                InlineKeyboardButton(text="⏸ Стоп", callback_data="ch:stop"),
                InlineKeyboardButton(text="▶️ Запуск", callback_data="ch:start"),
            ],
        ]),
    )


async def callback_channel(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    action = callback.data.removeprefix("ch:")

    if action == "now":
        idx = _channel_post_index["idx"] % len(CHANNEL_POSTS)
        text = CHANNEL_POSTS[idx]
        text += (
            "\n\n—\n"
            "🤖 <b>Бот для учёта расходов:</b> @tgbotexpensiveclaudecode_bot\n"
            "Пиши <code>кофе 300</code> — и контролируй финансы."
        )
        try:
            await bot.send_message(CHANNEL_USERNAME, text, parse_mode="HTML")
            _channel_post_index["idx"] = idx + 1
            await callback.answer(f"Пост #{idx + 1} опубликован!")
        except Exception as e:
            await callback.answer(f"Ошибка: {e}", show_alert=True)

    elif action == "stop":
        _channel_post_index["running"] = False
        await callback.answer("Автопостинг остановлен.")

    elif action == "start":
        if not _channel_post_index["running"]:
            asyncio.create_task(channel_scheduler(bot))
            await callback.answer("Автопостинг запущен!")
        else:
            await callback.answer("Уже работает.")


# ── Админка ───────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users")],
        [
            InlineKeyboardButton(text="🎁 Выдать Pro", callback_data="adm:grant"),
            InlineKeyboardButton(text="❌ Забрать Pro", callback_data="adm:revoke"),
        ],
    ])


async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🔧 <b>Админ-панель</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


async def callback_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    action = callback.data.removeprefix("adm:")

    if action == "stats":
        s = await db_admin_stats()
        now = datetime.now()
        text = (
            "📊 <b>Общая статистика</b>\n\n"
            f"Пользователей: <b>{s['total_users']}</b>\n"
            f"Pro-подписок: <b>{s['pro_users']}</b>\n"
            f"Доход от Stars: <b>{s['pro_users'] * PRO_PRICE} ⭐</b>\n\n"
            f"<b>Всего</b>\n"
            f"  Записей: {s['total_expenses']}\n"
            f"  Сумма: {s['total_sum']:,.0f} ₽\n\n"
            f"<b>{MONTHS_RU[now.month]} {now.year}</b>\n"
            f"  Записей: {s['month_expenses']}\n"
            f"  Сумма: {s['month_sum']:,.0f} ₽"
        )
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:back")]
            ]),
        )

    elif action == "users":
        rows = await db_admin_users()
        if not rows:
            await callback.answer("Пользователей пока нет.", show_alert=True)
            return
        lines = ["👥 <b>Пользователи</b>\n"]
        for uid, cnt, total, pro in rows:
            pro_badge = " ⭐" if pro else ""
            lines.append(
                f"<code>{uid}</code>{pro_badge}\n"
                f"  {cnt} записей · {total:,.0f} ₽\n"
            )
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:back")]
            ]),
        )

    elif action == "grant":
        await state.set_state(Form.admin_grant_pro)
        await callback.message.edit_text(
            "🎁 Введи Telegram ID пользователя, которому выдать Pro:"
        )

    elif action == "revoke":
        await state.set_state(Form.admin_revoke_pro)
        await callback.message.edit_text(
            "❌ Введи Telegram ID пользователя, у которого забрать Pro:"
        )

    elif action == "back":
        await callback.message.edit_text(
            "🔧 <b>Админ-панель</b>",
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )

    await callback.answer()


async def handle_admin_grant(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        uid = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Это не похоже на ID. Попробуй ещё раз:")
        return
    await db_set_pro(uid)
    await state.clear()
    await message.answer(
        f"✅ Pro выдан пользователю <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


async def handle_admin_revoke(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        uid = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Это не похоже на ID. Попробуй ещё раз:")
        return
    revoked = await db_revoke_pro(uid)
    if revoked:
        text = f"✅ Pro забран у пользователя <code>{uid}</code>"
    else:
        text = f"У пользователя <code>{uid}</code> и так нет Pro."
    await state.clear()
    await message.answer(text, parse_mode="HTML", reply_markup=admin_keyboard())


# ── Оплата Pro ────────────────────────────────────────────────────────────────

async def callback_buy_pro(callback: CallbackQuery, bot: Bot) -> None:
    if await db_is_pro(callback.from_user.id):
        await callback.answer("⭐ У тебя уже есть Pro!", show_alert=True)
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Pro подписка",
        description="Подписка Pro навсегда — разблокирует все будущие функции.",
        payload="pro_subscription",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Pro", amount=PRO_PRICE)],
    )
    await callback.answer()


async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


async def handle_successful_payment(message: Message) -> None:
    await db_set_pro(message.from_user.id)
    await message.answer(
        "🎉 <b>Спасибо! Pro активирован навсегда.</b>",
        parse_mode="HTML",
    )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main() -> None:
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Команды
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_history, Command("history"))
    dp.message.register(cmd_undo, Command("undo"))
    dp.message.register(cmd_categories, Command("categories"))
    dp.message.register(cmd_add, F.text.startswith("/add"), StateFilter(None))
    dp.message.register(cmd_regular, Command("regular"), StateFilter(None))
    dp.message.register(cmd_budget, Command("budget"), StateFilter(None))
    dp.message.register(cmd_compare, Command("compare"), StateFilter(None))
    dp.message.register(cmd_year, Command("year"), StateFilter(None))
    dp.message.register(cmd_wallet, Command("wallet"), StateFilter(None))
    dp.message.register(cmd_export, Command("export"))
    dp.callback_query.register(callback_export, F.data.startswith("exp:"))
    dp.message.register(cmd_limit, Command("limit"), StateFilter(None))
    dp.callback_query.register(callback_limit, F.data.startswith("lim:"))
    dp.message.register(handle_limit_amount, Form.limit_amount)
    dp.message.register(cmd_weekday, Command("weekday"))
    dp.message.register(cmd_goal, Command("goal"), StateFilter(None))
    dp.callback_query.register(callback_goal, F.data.startswith("goal:"))
    dp.message.register(handle_goal_name, Form.goal_name)
    dp.message.register(handle_goal_amount, Form.goal_amount)

    # Бюджеты
    dp.callback_query.register(callback_budget_add, F.data == "badd")
    dp.callback_query.register(callback_budget_cat, F.data.startswith("bcat:"), Form.budget_category)
    dp.callback_query.register(callback_budget_del, F.data.startswith("bdel:"))
    dp.message.register(handle_budget_amount, Form.budget_amount)

    # Кошельки
    dp.callback_query.register(callback_wallet, F.data.startswith("wal:"))
    dp.message.register(handle_wallet_create, Form.wallet_create)
    dp.message.register(handle_wallet_join, Form.wallet_join)

    # Регулярные расходы
    dp.message.register(handle_regular_input, Form.regular_input)
    dp.callback_query.register(callback_regular, F.data.startswith("reg:"))
    dp.callback_query.register(callback_regular_category, F.data.startswith("rcat:"), Form.regular_category)
    dp.callback_query.register(callback_regular_day, F.data.startswith("rday:"), Form.regular_day)

    # Админка
    dp.message.register(cmd_admin, Command("admin"))
    dp.message.register(cmd_channel, Command("channel"))
    dp.callback_query.register(callback_admin, F.data.startswith("adm:"))
    dp.callback_query.register(callback_channel, F.data.startswith("ch:"))
    dp.message.register(handle_admin_grant, Form.admin_grant_pro)
    dp.message.register(handle_admin_revoke, Form.admin_revoke_pro)

    # Оплата
    dp.callback_query.register(callback_buy_pro, F.data == "buy_pro")
    dp.pre_checkout_query.register(handle_pre_checkout)
    dp.message.register(handle_successful_payment, F.successful_payment)

    # FSM
    dp.message.register(handle_new_category_name, Form.adding_category)
    dp.message.register(handle_adding_category_cmd, Form.adding_category_cmd)
    dp.message.register(handle_expense_input, StateFilter(None))
    dp.callback_query.register(
        callback_choose_category, F.data.startswith("cat:"), Form.choosing_category
    )
    dp.callback_query.register(
        callback_choose_scope, F.data.startswith("scope:"), Form.choosing_scope
    )

    log.info("Бот запущен")
    asyncio.create_task(recurring_scheduler(bot))
    asyncio.create_task(channel_scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
