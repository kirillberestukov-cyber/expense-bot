#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import logging
import os
import re
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
    Message,
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── FSM States ───────────────────────────────────────────────────────────────

class Form(StatesGroup):
    choosing_category = State()
    adding_category = State()
    adding_category_cmd = State()


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
        await db.commit()


async def db_add_expense(user_id: int, name: str, amount: float, category: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO expenses (user_id, name, amount, category, created_at) VALUES (?,?,?,?,?)",
            (user_id, name, amount, category, datetime.now().isoformat()),
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
            """SELECT name, amount, category, created_at FROM expenses
               WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",
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


# ── Утилиты ───────────────────────────────────────────────────────────────────

def parse_expense(text: str) -> tuple[str | None, float | None]:
    """Разбирает 'название сумма' — число должно быть в конце строки."""
    m = re.match(r"^(.+?)\s+(\d+(?:[.,]\d+)?)\s*$", text.strip())
    if not m:
        return None, None
    try:
        amount = float(m.group(2).replace(",", "."))
        return m.group(1).strip(), amount
    except ValueError:
        return None, None


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
    await message.answer(
        "👋 <b>Бот учёта расходов</b>\n\n"
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
        "  /add — добавить свою категорию",
        parse_mode="HTML",
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
    for name, amount, category, created_at in rows:
        dt = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
        lines.append(f"<i>{dt}</i>  {category}\n  <b>{name}</b> — {amount:,.0f} ₽\n")
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
    name, amount = parse_expense(message.text or "")
    if name is None:
        await message.answer(
            "Не понял 🤔 Напиши расход так:\n<code>название сумма</code>\n\n"
            "Например: <code>кофе 300</code>",
            parse_mode="HTML",
        )
        return

    cats = await db_get_categories(message.from_user.id)
    await state.set_state(Form.choosing_category)
    await state.update_data(name=name, amount=amount)
    await message.answer(
        f"💸 <b>{name}</b> — {amount:,.0f} ₽\n\nВыбери категорию:",
        reply_markup=category_keyboard(cats),
        parse_mode="HTML",
    )


async def callback_choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    cat = callback.data.removeprefix("cat:")

    if cat == "__new__":
        await state.set_state(Form.adding_category)
        await callback.message.edit_text("Введи название новой категории:")
        await callback.answer()
        return

    data = await state.get_data()
    name, amount = data.get("name"), data.get("amount")
    await state.clear()

    await db_add_expense(callback.from_user.id, name, amount, cat)
    await callback.message.edit_text(
        f"✅ <b>Записано!</b>\n\n{cat}\n<b>{name}</b> — {amount:,.0f} ₽",
        parse_mode="HTML",
    )
    await callback.answer()


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
        await db_add_expense(message.from_user.id, name, amount, cat_name)
        await message.answer(
            f"✅ Категория «{cat_name}» создана и расход записан!\n\n"
            f"{cat_name}\n<b>{name}</b> — {amount:,.0f} ₽",
            parse_mode="HTML",
        )
    else:
        await message.answer(f"✅ Категория «{cat_name}» создана!")


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

    # FSM
    dp.message.register(handle_new_category_name, Form.adding_category)
    dp.message.register(handle_adding_category_cmd, Form.adding_category_cmd)
    dp.message.register(handle_expense_input, StateFilter(None))
    dp.callback_query.register(
        callback_choose_category, F.data.startswith("cat:"), Form.choosing_category
    )

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
