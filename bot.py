import os
import json
import base64
import logging
import calendar
from datetime import datetime, date

import openai
import gspread

from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ======================
# LOGS
# ======================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ======================
# ENV
# ======================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
REPORT_CHAT_ID = os.getenv("REPORT_CHAT_ID")

SHEET_NAME = "Чеки"

ASK_NAME, ASK_PHOTO, ASK_NOTE, ASK_CONTINUE = range(4)


# ======================
# ENV CHECK
# ======================

def check_env():
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if not SHEET_ID:
        missing.append("SHEET_ID")

    if not GOOGLE_CREDS_JSON:
        missing.append("GOOGLE_CREDS_JSON")

    if missing:
        raise RuntimeError(
            "Не хватает переменных окружения: " + ", ".join(missing)
        )


# ======================
# GOOGLE SHEETS
# ======================

def get_sheet():
    creds_data = json.loads(GOOGLE_CREDS_JSON)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    creds = Credentials.from_service_account_info(
        creds_data,
        scopes=scopes,
    )

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)

    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=SHEET_NAME,
            rows=3000,
            cols=12,
        )

        ws.append_row([
            "№",
            "Фото file_id",
            "Дата чека",
            "Магазин",
            "Товары",
            "Сумма",
            "Валюта",
            "Категория",
            "Примечание",
            "Кто внёс",
            "Время записи",
            "Telegram user id",
        ])

        ws.format("A1:L1", {
            "textFormat": {
                "bold": True,
                "foregroundColor": {
                    "red": 1,
                    "green": 1,
                    "blue": 1,
                },
            },
            "backgroundColor": {
                "red": 0.15,
                "green": 0.15,
                "blue": 0.15,
            },
        })

    return ws


def get_next_row_num(ws) -> int:
    values = ws.get_all_values()

    if len(values) <= 1:
        return 1

    data_rows = [
        row for row in values[1:]
        if any(str(cell).strip() for cell in row)
    ]

    return len(data_rows) + 1


# ======================
# OPENAI RECOGNITION
# ======================

async def recognize(image_bytes: bytes) -> dict:
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

    b64 = base64.b64encode(image_bytes).decode("utf-8")

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=800,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты профессионально распознаёшь кассовые чеки. "
                    "Всегда отвечай только валидным JSON без markdown."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": """
Распознай кассовый чек на фото.

Верни строго JSON такого формата:

{
  "date": "DD.MM.YYYY",
  "store": "название магазина",
  "items": "товары через запятую",
  "amount": 0.00,
  "currency": "EUR",
  "category": "Продукты/Транспорт/Офис/Техника/Услуги/Ресторан/Одежда/Другое"
}

Правила:
- amount всегда число, например 81.80
- если чек из магазина одежды, категория "Одежда"
- если валюта не указана, но чек из Франции/Монако/Европы — ставь EUR
- если поле не видно — пиши "Не указано"
- не добавляй markdown
"""
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    )

    raw = response.choices[0].message.content.strip()

    logger.info(f"OpenAI raw response: {raw}")

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.error(f"OpenAI вернул невалидный JSON: {raw}")
        raise e

    amount = data.get("amount", 0)

    try:
        amount = float(str(amount).replace(",", "."))
    except Exception:
        amount = 0

    return {
        "date": data.get("date", "Не указано"),
        "store": data.get("store", "Не указано"),
        "items": data.get("items", "Не указано"),
        "amount": amount,
        "currency": data.get("currency", "EUR"),
        "category": data.get("category", "Другое"),
    }


# ======================
# BOT FLOW
# ======================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()

    await update.message.reply_text(
        "👋 Привет! Я помогу записать чек в таблицу бухгалтера.\n\n"
        "Как тебя зовут?"
    )

    return ASK_NAME


async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()

    if len(name) < 2:
        await update.message.reply_text("Пожалуйста, напиши своё имя.")
        return ASK_NAME

    ctx.user_data["name"] = name

    await update.message.reply_text(
        f"Отлично, {name}! 📸\n\n"
        "Теперь пришли фото чека."
    )

    return ASK_PHOTO


async def got_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text(
            "Пожалуйста, пришли именно фото чека 📸"
        )
        return ASK_PHOTO

    photo = update.message.photo[-1]

    file = await photo.get_file()
    image_bytes = bytes(await file.download_as_bytearray())

    ctx.user_data["photo"] = image_bytes
    ctx.user_data["file_id"] = photo.file_id

    await update.message.reply_text(
        "✍️ Напиши примечание — для чего куплено?\n\n"
        "Например: офисные расходы, командировка, личные нужды...\n\n"
        "Или напиши /skip"
    )

    return ASK_NOTE


async def got_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()

    return await process_receipt(update, ctx, note)


async def skip_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await process_receipt(update, ctx, "")


async def process_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, note: str):
    name = ctx.user_data.get("name", "Неизвестно")
    image_bytes = ctx.user_data.get("photo")
    file_id = ctx.user_data.get("file_id", "")
    telegram_user_id = update.effective_user.id if update.effective_user else ""

    if not image_bytes:
        await update.message.reply_text(
            "❌ Фото чека не найдено. Начни снова: /start"
        )
        return ConversationHandler.END

    msg = await update.message.reply_text("⏳ Распознаю чек через ИИ...")

    try:
        receipt = await recognize(image_bytes)

        ws = get_sheet()
        row_num = get_next_row_num(ws)

        timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

        ws.append_row([
            row_num,
            file_id,
            receipt["date"],
            receipt["store"],
            receipt["items"],
            receipt["amount"],
            receipt["currency"],
            receipt["category"],
            note,
            name,
            timestamp,
            telegram_user_id,
        ])

        await msg.edit_text(
            f"✅ Записано в таблицу!\n\n"
            f"📄 Строка: #{row_num}\n"
            f"📅 Дата: {receipt['date']}\n"
            f"🏪 Магазин: {receipt['store']}\n"
            f"🛍 Товары: {receipt['items']}\n"
            f"🏷 Категория: {receipt['category']}\n"
            f"💰 Сумма: {receipt['amount']:.2f} {receipt['currency']}\n"
            f"📝 Примечание: {note or '—'}\n"
            f"👤 Внёс: {name}"
        )

        saved_name = name
        ctx.user_data.clear()
        ctx.user_data["name"] = saved_name

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "➕ Добавить ещё чек",
                    callback_data="continue",
                ),
                InlineKeyboardButton(
                    "✅ Готово",
                    callback_data="done",
                ),
            ]
        ])

        await update.message.reply_text(
            "Хочешь добавить ещё один чек?",
            reply_markup=keyboard,
        )

        return ASK_CONTINUE

    except json.JSONDecodeError:
        logger.exception("Ошибка JSON при распознавании")

        await msg.edit_text(
            "❌ ИИ увидел чек, но вернул неправильный формат данных.\n\n"
            "Попробуй отправить фото ещё раз или начни снова: /start"
        )

        return ConversationHandler.END

    except Exception as e:
        logger.exception("Ошибка при обработке чека")

        await msg.edit_text(
            f"❌ Ошибка при обработке чека:\n\n"
            f"{str(e)}\n\n"
            f"Начни снова: /start"
        )

        return ConversationHandler.END


async def continue_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "continue":
        name = ctx.user_data.get("name", "")

        if name:
            await query.message.reply_text(
                f"📸 Пришли фото следующего чека, {name}!"
            )
            return ASK_PHOTO

        await query.message.reply_text("Как тебя зовут?")
        return ASK_NAME

    await query.message.reply_text(
        "Спасибо! Все чеки записаны в таблицу 📊\n\n"
        "Чтобы добавить новый чек — напиши /start"
    )

    ctx.user_data.clear()

    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()

    await update.message.reply_text(
        "Отменено.\nЧтобы начать снова — напиши /start"
    )

    return ConversationHandler.END


# ======================
# REPORTS
# ======================

def parse_receipt_date(value: str):
    if not value:
        return None

    value = value.strip()

    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except Exception:
        return None


def get_current_month_totals():
    ws = get_sheet()
    rows = ws.get_all_values()

    today = date.today()

    totals = {}
    grand_total = 0

    for row in rows[1:]:
        if len(row) < 8:
            continue

        receipt_date = parse_receipt_date(row[2])

        if not receipt_date:
            continue

        if receipt_date.month != today.month or receipt_date.year != today.year:
            continue

        try:
            amount = float(str(row[5]).replace(",", "."))
        except Exception:
            amount = 0

        category = row[7] if len(row) > 7 and row[7] else "Другое"

        totals[category] = totals.get(category, 0) + amount
        grand_total += amount

    return totals, grand_total


async def report_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        totals, grand_total = get_current_month_totals()

        month_str = date.today().strftime("%m.%Y")

        if not totals:
            await update.message.reply_text(
                f"📊 За {month_str} данных пока нет."
            )
            return

        lines = [f"📊 Отчёт за {month_str}\n"]

        for category, total in sorted(totals.items(), key=lambda x: -x[1]):
            percent = total / grand_total * 100 if grand_total else 0
            lines.append(
                f"• {category}: {total:.2f} EUR ({percent:.0f}%)"
            )

        lines.append(f"\n💰 Итого: {grand_total:.2f} EUR")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        logger.exception("Ошибка отчёта")
        await update.message.reply_text(f"❌ Ошибка отчёта: {str(e)}")


async def send_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    today = date.today()

    last_day = calendar.monthrange(today.year, today.month)[1]

    if today.day != last_day:
        return

    if not REPORT_CHAT_ID:
        logger.warning("REPORT_CHAT_ID не указан, автоотчёт не отправлен")
        return

    try:
        totals, grand_total = get_current_month_totals()

        month_str = today.strftime("%m.%Y")

        if not totals:
            report = f"📊 Отчёт за {month_str}\n\nДанных за этот месяц нет."
        else:
            lines = [f"📊 Отчёт за {month_str}\n"]

            for category, total in sorted(totals.items(), key=lambda x: -x[1]):
                lines.append(f"• {category}: {total:.2f} EUR")

            lines.append(f"\n💰 Итого: {grand_total:.2f} EUR")

            report = "\n".join(lines)

        await context.bot.send_message(
            chat_id=REPORT_CHAT_ID,
            text=report,
        )

    except Exception:
        logger.exception("Ошибка автоотчёта")


# ======================
# MAIN
# ======================

def main():
    check_env()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
        ],
        states={
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_name),
            ],
            ASK_PHOTO: [
                MessageHandler(filters.PHOTO, got_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_photo),
            ],
            ASK_NOTE: [
                CommandHandler("skip", skip_note),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_note),
            ],
            ASK_CONTINUE: [
                CallbackQueryHandler(continue_handler),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)

    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(MessageHandler(filters.Regex("^отч[её]т$"), report_command))

    app.job_queue.run_daily(
        send_monthly_report,
        time=datetime.strptime("20:00", "%H:%M").time(),
        name="send_monthly_report",
    )

    logger.info("Бот запущен!")

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
