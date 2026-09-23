import asyncio
import os
import logging
import uuid
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.filters import Command
from dotenv import load_dotenv

from ocr import recognize_receipt
from llm import parse_receipt_text, generate_recipes
from db import (
    init_db,
    add_product,
    get_fridge,
    get_expiring,
    get_all_for_cooking,
    delete_product,
    delete_expired,
    delete_all,
)

load_dotenv()

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Убираем спам от aiohttp, оставляем только важное
logging.getLogger("aiogram.event").setLevel(logging.INFO)

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher()
pending = {}
processing = asyncio.Semaphore(2)

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        "Привет! Я — умный холодильник 🧊\n\n"
        "📸 Отправь фото чека — я добавлю продукты.\n"
        "📋 /fridge — посмотреть холодильник.\n"
        "🍳 /cook — что приготовить из того, что скоро испортится.\n"
        "🗑️ /del ID — удалить товар по ID.\n"
        "🧹 /clear_expired — удалить всё просроченное.\n"
        "🚫 /clear — очистить холодильник полностью."
    )

@dp.message(Command("help"))
async def help_cmd(msg: Message):
    await msg.answer(
        "🧊 *FridgeBot — что я умею:*\n\n"
        "📸 *Отправь фото чека* — распознаю товары и добавлю в холодильник.\n\n"
        "*/fridge* — посмотреть, что лежит в холодильнике (сгруппировано по срочности).\n"
        "*/cook* — предложу 3 рецепта из того, что скоро испортится.\n"
        "*/del ID* — удалить товар по ID (можно несколько через пробел).\n"
        "*/clear_expired* — удалить всё просроченное.\n"
        "*/clear* — очистить холодильник полностью.\n"
        "*/help* — эта справка.\n\n"
        "💡 *Совет:* отправляйте чек сразу после магазина — "
        "тогда сроки годности будут точнее.",
        parse_mode="Markdown"
    )

@dp.message(F.photo)
async def handle_photo(msg: Message):
    await msg.answer("🔍 Распознаю чек...")
    file = await bot.get_file(msg.photo[-1].file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()
    try:
        async with processing:
            raw_text = await asyncio.to_thread(recognize_receipt, image_bytes)
    except Exception as e:
        await msg.answer(f"❌ Ошибка OCR: {e}")
        return
    try:
        async with processing:
            products, purchase_date = await asyncio.to_thread(parse_receipt_text, raw_text)
    except Exception as e:
        await msg.answer(f"❌ Ошибка парсинга: {e}")
        return
    if not products:
        await msg.answer("🤔 Не удалось найти товары в чеке.")
        return
    confirmation_id = uuid.uuid4().hex[:12]
    pending[confirmation_id] = {
        "user_id": msg.from_user.id,
        "products": products,
        "purchase_date": purchase_date,
    }
    lines = ["📋 *Распознанные товары:*\n"]
    for i, p in enumerate(products, 1):
        lines.append(f"{i}. {p['name']} — {p['quantity']} {p['unit']} — {p['price']} ₽")
    lines.append("\nВсё верно?")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data=f"save:{confirmation_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel:{confirmation_id}")],
    ])
    await msg.answer("\n".join(lines), reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("save:"))
async def save_products(cb: CallbackQuery):
    confirmation_id = cb.data.split(":", 1)[1]
    pending_data = pending.get(confirmation_id)
    if not pending_data:
        await cb.message.edit_text("Срок подтверждения истёк. Отправьте чек ещё раз.")
        return
    if pending_data["user_id"] != cb.from_user.id:
        await cb.answer("Это не ваш чек.", show_alert=True)
        return
    pending.pop(confirmation_id, None)

    products = pending_data["products"]
    purchase_date = pending_data.get("purchase_date")
    for p in products:
        add_product(
            cb.from_user.id,
            p["name"],
            p["quantity"],
            p["unit"],
            p["price"],
            p.get("category", "не еда"),
            purchase_date,
        )
    await cb.message.edit_text(f"✅ Сохранено {len(products)} товаров в холодильник.")

@dp.callback_query(F.data.startswith("cancel:"))
async def cancel(cb: CallbackQuery):
    confirmation_id = cb.data.split(":", 1)[1]
    pending_data = pending.get(confirmation_id)
    if pending_data and pending_data["user_id"] != cb.from_user.id:
        await cb.answer("Это не ваш чек.", show_alert=True)
        return
    pending.pop(confirmation_id, None)
    await cb.message.edit_text("❌ Отменено.")

@dp.message(Command("fridge"))
async def show_fridge(msg: Message):
    from datetime import datetime
    user_id = msg.from_user.id

    def format_days_left(days_left):
        if days_left < 0:
            return f"срок годности истёк {abs(days_left)} дн. назад"
        if days_left == 0:
            return "срок годности истекает сегодня"
        if days_left == 1:
            return "срок годности: около 1 дня"
        return f"срок годности: около {days_left} дней"

    rows = get_fridge(user_id)
    if not rows:
        await msg.answer("🧊 Холодильник пуст.")
        return

    red, yellow, green, no_date = [], [], [], []
    for r in rows:
        pid, name, qty, unit, price, cat, expiry, purchase_date, status, _ = r
        if not expiry:
            no_date.append((pid, name, qty, unit))
            continue
        days_left = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days
        if days_left < 0:
            red.append((pid, name, qty, unit, days_left))
        elif days_left <= 3:
            yellow.append((pid, name, qty, unit, days_left))
        else:
            green.append((pid, name, qty, unit, days_left))

    lines = ["🧊 *Холодильник:*\n"]

    if red:
        lines.append("🔴 *Просрочено / истекает сегодня:*")
        for pid, name, qty, unit, d in red:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit} ({format_days_left(d)})")
        lines.append("")

    if yellow:
        lines.append("🟡 *Скоро испортится (1–3 дня):*")
        for pid, name, qty, unit, d in yellow:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit} ({format_days_left(d)})")
        lines.append("")

    if green:
        lines.append("🟢 *Свежее:*")
        for pid, name, qty, unit, d in green:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit} ({format_days_left(d)})")
        lines.append("")

    if no_date:
        lines.append("⚪ *Без срока (специи, бакалея):*")
        for pid, name, qty, unit in no_date:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit}")

    lines.append("\n💡 Удалить: `/del ID` (можно несколько: `/del 3 5 7`)")
    await msg.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("del"))
async def delete_items(msg: Message):
    user_id = msg.from_user.id
    args = msg.text.split()[1:]
    if not args:
        await msg.answer(
            "Использование: `/del ID` или `/del ID1 ID2 ID3`\n\nID показаны в `/fridge` рядом с названием.",
            parse_mode="Markdown",
        )
        return

    deleted = 0
    not_found = []
    for arg in args:
        try:
            pid = int(arg)
            if delete_product(user_id, pid):
                deleted += 1
            else:
                not_found.append(arg)
        except ValueError:
            not_found.append(arg)

    reply = f"✅ Удалено: {deleted}"
    if not_found:
        reply += f"\n⚠️ Не найдены: {', '.join(not_found)}"
    await msg.answer(reply)

@dp.message(Command("clear_expired"))
async def clear_expired_cmd(msg: Message):
    deleted = delete_expired(msg.from_user.id)
    if deleted:
        await msg.answer(f"🗑️ Удалено просроченных товаров: {deleted}")
    else:
        await msg.answer("✨ Просроченных товаров нет.")

@dp.message(Command("clear"))
async def clear_all_cmd(msg: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Да, очистить всё", callback_data=f"clear_confirm:{msg.from_user.id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="clear_cancel")],
    ])
    await msg.answer("⚠️ Удалить **все** товары из холодильника?", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("clear_confirm:"))
async def clear_confirm(cb: CallbackQuery):
    owner_id = int(cb.data.split(":", 1)[1])
    if owner_id != cb.from_user.id:
        await cb.answer("Это не ваш холодильник.", show_alert=True)
        return
    deleted = delete_all(cb.from_user.id)
    await cb.message.edit_text(f"🗑️ Холодильник очищен. Удалено: {deleted}")

@dp.callback_query(F.data == "clear_cancel")
async def clear_cancel(cb: CallbackQuery):
    await cb.message.edit_text("Отменено.")

@dp.message(Command("cook"))
async def cook(msg: Message):
    from datetime import datetime

    products_raw = get_all_for_cooking(msg.from_user.id)
    if not products_raw:
        await msg.answer("🧊 В холодильнике нет продуктов для готовки.")
        return

    lines = []
    for name, qty, unit, category, expiry in products_raw:
        if expiry:
            days_left = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days
            if days_left <= 3:
                marker = "🔴 СРОЧНО"
            elif days_left <= 7:
                marker = "🟡 скоро"
            else:
                marker = "🟢 свежее"
        else:
            marker = "⚪ без срока"
        lines.append(f"{marker} | {name} — {qty} {unit}")

    product_list = "\n".join(lines)
    wait_msg = await msg.answer("🍳 Думаю, что приготовить... (до 1 минуты)")
    try:
        async with processing:
            recipes = await asyncio.to_thread(generate_recipes, product_list)
    except requests.exceptions.Timeout:
        await wait_msg.edit_text("⏳ Сервис рецептов не ответил вовремя. Попробуйте ещё раз через минуту.")
        return
    except requests.exceptions.RequestException:
        await wait_msg.edit_text("⚠️ Не удалось подключиться к сервису рецептов. Проверьте интернет и попробуйте позже.")
        return
    except Exception:
        logging.exception("Ошибка генерации рецептов")
        await wait_msg.edit_text("⚠️ Не удалось приготовить ответ с рецептами. Попробуйте ещё раз.")
        return
    try:
        await wait_msg.edit_text(recipes, parse_mode="Markdown")
    except TelegramBadRequest as error:
        if "can't parse entities" not in str(error).lower():
            raise
        await wait_msg.edit_text(recipes)

async def main():
    init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="fridge", description="Мой холодильник"),
        BotCommand(command="cook", description="Что приготовить"),
        BotCommand(command="del", description="Удалить товар по ID"),
        BotCommand(command="clear_expired", description="Удалить просроченное"),
        BotCommand(command="clear", description="Очистить всё"),
        BotCommand(command="help", description="Справка"),
    ])
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("Polling остановлен")

if __name__ == "__main__":
    asyncio.run(main())