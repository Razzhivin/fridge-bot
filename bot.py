import asyncio
import aiohttp
import os
import logging
import uuid
import requests
from collections import defaultdict
from datetime import datetime
from contextlib import suppress
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, ChatMemberUpdated
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
    get_expiration_alerts,
    mark_expiration_alert_sent,
    consume_photo_quota,
    can_process_receipt,
    activate_subscription,
    cleanup_expired_subscriptions,
    get_subscription_status,
)

load_dotenv()


MAX_PHOTO_BYTES = int(os.getenv("MAX_PHOTO_BYTES", str(10 * 1024 * 1024)))
MAX_COOK_PER_HOUR = int(os.getenv("MAX_COOK_PER_HOUR", "3"))
cook_usage = defaultdict(list)

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
ALERT_CHECK_INTERVAL = 60 * 60


def _consume_limit(usage, user_id, limit, period):
    now = datetime.now().timestamp()
    timestamps = [timestamp for timestamp in usage[user_id] if now - timestamp < period]
    if len(timestamps) >= limit:
        usage[user_id] = timestamps
        return False
    timestamps.append(now)
    usage[user_id] = timestamps
    return True


async def _deny_limit(message, text):
    await message.answer(text)

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        "Привет! Я — умный холодильник 🧊\n\n"
        "📸 Отправь фото чека — я добавлю продукты.\n"
        "📋 /fridge — посмотреть холодильник.\n"
        "🍳 /cook — что приготовить из того, что скоро испортится.\n"
        "🗑️ /del ID — удалить товар по ID.\n"
        "🧹 /clear_expired — удалить всё просроченное.\n"
        "🚫 /clear — очистить холодильник полностью.\n\n"
        "💎 Всего 4 бесплатных чека. "
        "Для безлимита — /subscribe (50 Stars, 30 дней)."
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
        "*/subscribe* — активировать подписку для распознавания чеков.\n"
        "*/help* — эта справка.\n\n"
        "💎 *Тарифы:*\n"
        "• Всего 4 бесплатных чека за всё время\n"
        "• Подписка — 50 Stars на 30 дней (без ограничений)\n\n"
        "💡 *Как купить Stars:*\n"
        "Откройте @PremiumBot → /start → «Telegram Stars».\n\n"
        "💡 *Совет:* отправляйте чек сразу после магазина — "
        "тогда сроки годности будут точнее.",
        parse_mode="Markdown"
    )

@dp.message(F.photo)
async def handle_photo(msg: Message):
    user_id = msg.from_user.id
    cleanup_expired_subscriptions()
    if not can_process_receipt(user_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Оплатить доступ", switch_inline_query_current_chat="")],
        ])
        await msg.answer(
            "🔒 Вы использовали все 4 бесплатных чека.\n\n"
            "Активируйте подписку для распознавания:\n"            
            "• Подписка — 50 Stars на 30 дней (без ограничений)\n\n"
            "Оплатить через @PremiumBot:\n"
            "1. Откройте @PremiumBot\n"
            "2. Отправьте /start\n"
            "3. Выберите «Telegram Stars»\n"
            "4. Отправьте мне фото чека — распознавание откроется\n\n"
            "Или используйте команду /subscribe.",
            reply_markup=kb,
        )
        return
    await msg.answer("🔍 Распознаю чек...")
    file = await bot.get_file(msg.photo[-1].file_id)
    file_bytes = await bot.download_file(file.file_path)
    image_bytes = file_bytes.read()
    if len(image_bytes) > MAX_PHOTO_BYTES:
        await msg.answer("📦 Фото слишком большое. Отправьте чек размером до 10 МБ.")
        return
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

    consume_photo_quota(cb.from_user.id)

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


# ==================== Telegram Stars Payment ====================

@dp.message(Command("subscribe"))
async def cmd_subscribe(msg: Message):
    user_id = msg.from_user.id

    subscription = get_subscription_status(user_id)
    if subscription and subscription["status"] == "paid" and subscription["paid_until"]:
        try:
            from datetime import datetime as dt
            until = dt.strptime(subscription["paid_until"], "%Y-%m-%d").date()
            await msg.answer(
                f"⭐ У вас активна подписка до {until.strftime('%d.%m.%Y')}.\n\n"
                f"Распознавание чеков доступно без ограничений."
            )
            return
        except ValueError:
            pass

    api_url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendInvoice"
    payload = {
        "chat_id": user_id,
        "reply_to_message_id": msg.message_id,
        "title": "Распознавание чеков",
        "description": "Подписка на распознавание чеков — 30 дней, без ограничений",
        "payload": "receipt_subscription",
        "provider_token": "",
        "currency": "XTR",
        "star_count": 50,
        "prices": [{"label": "Доступ к распознаванию чеков", "amount": 50}],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                logging.error("sendInvoice failed: %s", text)
                await msg.answer("⚠️ Не удалось создать платёж. Попробуйте позже.")
                return


@dp.pre_checkout_query()
async def handle_pre_checkout(query):
    if query.invoice_payload == "receipt_subscription":
        await bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Неизвестный товар")


@dp.message(F.successful_payment)
async def handle_successful_payment(msg: Message):
    payment_id = msg.successful_payment.invoice_payload
    user_id = msg.from_user.id

    if payment_id == "receipt_subscription":
        paid_until = activate_subscription(user_id, amount=50, currency="XTR", payment_id=msg.successful_payment.telegram_payment_charge_id)
        try:
            from datetime import datetime as dt
            until = dt.strptime(paid_until, "%Y-%m-%d").date()
            until_str = until.strftime("%d.%m.%Y")
            await msg.answer(
                f"✅ Оплата прошла успешно!\n\n"
                f"Подписка активирована до {until_str}.\n"
                f"Распознавание чеков доступно без ограничений."
            )
        except ValueError:
            until_str = paid_until
            await msg.answer("✅ Оплата прошла успешно! Подписка активирована.")

        # Уведомление админу об оплате
        username = f"@{msg.from_user.username}" if msg.from_user.username else "нет"
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        text = (
            f"💰 *Оплата получена!*\n\n"
            f"Пользователь: {msg.from_user.full_name} (ID: {user_id})\n"
            f"Username: {username}\n"
            f"Сумма: 50 Stars\n"
            f"Подписка до: {until_str}\n"
            f"Дата: {now}"
        )
        await send_admin_notification(text)
    else:
        logging.warning("Неизвестный платеж от пользователя %s: %s", user_id, payment_id)
        await msg.answer("⚠️ Не удалось активировать подписку. Попробуйте ещё раз.")

        # Уведомление админу о неизвестном платеже
        username = f"@{msg.from_user.username}" if msg.from_user.username else "нет"
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        text = (
            f"⚠️ *Неизвестный платёж*\n\n"
            f"Пользователь: {msg.from_user.full_name} (ID: {user_id})\n"
            f"Username: {username}\n"
            f"Payload: {payment_id}\n"
            f"Дата: {now}"
        )
        await send_admin_notification(text)

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
        for pid, name, qty, unit, d in red[:20]:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit} ({format_days_left(d)})")
        if len(red) > 20:
            lines.append(f"  ... и ещё {len(red) - 20}")
        lines.append("")

    if yellow:
        lines.append("🟡 *Скоро испортится (1–3 дня):*")
        for pid, name, qty, unit, d in yellow[:20]:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit} ({format_days_left(d)})")
        if len(yellow) > 20:
            lines.append(f"  ... и ещё {len(yellow) - 20}")
        lines.append("")

    if green:
        lines.append("🟢 *Свежее:*")
        for pid, name, qty, unit, d in green[:20]:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit} ({format_days_left(d)})")
        if len(green) > 20:
            lines.append(f"  ... и ещё {len(green) - 20}")
        lines.append("")

    if no_date:
        lines.append("⚪ *Без срока (специи, бакалея):*")
        for pid, name, qty, unit in no_date[:20]:
            lines.append(f"  `[id:{pid}]` {name} — {qty} {unit}")
        if len(no_date) > 20:
            lines.append(f"  ... и ещё {len(no_date) - 20}")

    lines.append("\n💡 Удалить: /del ID (можно несколько: /del 3 5 7)")
    text = "\n".join(lines)
    truncated = False
    if len(text) > 4000:
        text = text[:3997] + "\n\n⋯ (сообщение обрезано)"
        truncated = True
    if truncated:
        await msg.answer(text)
    else:
        try:
            await msg.answer(text, parse_mode="Markdown")
        except TelegramBadRequest:
            await msg.answer(text)

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


def _format_alert_message(rows):
    lines = ["🔔 *Сроки годности:*"]
    today = datetime.now().date()
    for product_id, user_id, name, quantity, unit, expiry in rows:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        days_left = (expiry_date - today).days
        if days_left < 0:
            label = f"просрочено на {abs(days_left)} дн."
        elif days_left == 0:
            label = "истекает сегодня"
        elif days_left == 1:
            label = "истекает завтра"
        else:
            label = f"истекает через {days_left} дн."
        lines.append(f"• {name} — {quantity} {unit}: {label}")
    lines.append("\nОткройте /fridge для подробностей.")
    return "\n".join(lines)


async def send_expiration_alerts(user_id=None):
    rows = get_expiration_alerts(user_id=user_id)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[1]].append(row)

    for recipient_id, recipient_rows in grouped.items():
        try:
            await bot.send_message(recipient_id, _format_alert_message(recipient_rows))
        except TelegramBadRequest as error:
            if "chat not found" in str(error).lower():
                logging.info("Пользователь %s удалил бота или заблокировал его", recipient_id)
            else:
                logging.exception("Не удалось отправить уведомление пользователю %s", recipient_id)
            continue
        except Exception:
            logging.exception("Не удалось отправить уведомление пользователю %s", recipient_id)
            continue

        today = datetime.now().strftime("%Y-%m-%d")
        for product_id, _, _, _, _, expiry in recipient_rows:
            alert_type = "expired" if expiry < today else "expiring"
            mark_expiration_alert_sent(recipient_id, product_id, alert_type, today)

    return len(rows)


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


# ==================== Уведомления админу ====================

@dp.my_chat_member()
async def chat_member_handler(update: ChatMemberUpdated):
    user = update.from_user
    if user and not user.is_bot:
        username = f"@{user.username}" if user.username else "нет"
        name = user.full_name
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        text = (
            f"👤 *Новый пользователь*\n\n"
            f"Имя: {name}\n"
            f"Username: {username}\n"
            f"ID: {user.id}\n"
            f"Дата: {now}"
        )
        await send_admin_notification(text)


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

    consume_photo_quota(cb.from_user.id)

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

    # Уведомление админу о чеке
    logging.info("save_products: отправка уведомления админу для пользователя %s", cb.from_user.id)
    username = f"@{cb.from_user.username}" if cb.from_user.username else "нет"
    lines = [f"🧾 *Новый чек сохранён*", "", f"Пользователь: {cb.from_user.full_name} (ID: {cb.from_user.id})"]
    if purchase_date:
        lines.append(f"Дата покупки: {purchase_date}")
    lines.append(f"Товаров: {len(products)}")
    lines.append("")
    lines.append("*Список:*")
    for i, p in enumerate(products, 1):
        if i <= 10:
            lines.append(f"{i}. {p['name']} — {p['quantity']} {p['unit']} — {p['price']} ₽")
        else:
            lines.append(f"... и ещё {len(products) - 10}")
            break
    lines.append("")
    lines.append("📱 *Холодильник обновлён.*")
    await send_admin_notification("\n".join(lines))

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


@dp.message(Command("cook"))
async def cook(msg: Message):
    from datetime import datetime

    user_id = msg.from_user.id
    if not _consume_limit(cook_usage, user_id, MAX_COOK_PER_HOUR, 60 * 60):
        await _deny_limit(msg, "⏳ Лимит генерации рецептов на час исчерпан. Попробуйте позже.")
        return

    products_raw = get_all_for_cooking(user_id)
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

async def send_admin_notification(text: str):
    admin_id = os.getenv("ADMIN_CHAT_ID")
    if not admin_id:
        logging.warning("ADMIN_CHAT_ID не задан — уведомление не отправлено")
        return
    logging.info("Отправка уведомления админу (chat_id=%s): %s", admin_id, text[:100])
    try:
        await bot.send_message(int(admin_id), text)
        logging.info("Уведомление админу отправлено")
    except Exception:
        logging.exception("Не удалось отправить уведомление админу")


async def main():
    init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="fridge", description="Мой холодильник"),
        BotCommand(command="cook", description="Что приготовить"),
        BotCommand(command="del", description="Удалить товар по ID"),
        BotCommand(command="clear_expired", description="Удалить просроченное"),
        BotCommand(command="clear", description="Очистить всё"),
        BotCommand(command="subscribe", description="Активировать подписку"),
        BotCommand(command="help", description="Справка"),
    ])
    async def alert_worker():
        while True:
            try:
                await send_expiration_alerts()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Ошибка фоновой проверки сроков")
            await asyncio.sleep(ALERT_CHECK_INTERVAL)

    alert_task = asyncio.create_task(alert_worker())
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        logging.info("Polling остановлен")
    finally:
        alert_task.cancel()
        with suppress(asyncio.CancelledError):
            await alert_task

if __name__ == "__main__":
    asyncio.run(main())