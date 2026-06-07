import base64
import calendar
import json
import logging
import os
import re
from datetime import date, datetime

import gspread
import openai
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
REPORT_CHAT_ID = os.getenv("REPORT_CHAT_ID")

ASK_NAME, ASK_PHOTO, ASK_NOTE, ASK_CONTINUE = range(4)

CATEGORIES = {
    "Продукты",
    "Транспорт",
    "Офис",
    "Техника",
    "Услуги",
    "Ресторан",
    "Одежда",
    "Другое",
}


def get_sheet():
    creds_data = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)

    try:
        ws = sheet.worksheet("Чеки")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet("Чеки", rows=2000, cols=10)
        ws.append_row([
            "№",
            "Фото (file_id)",
            "Дата чека",
            "Магазин",
            "Товары",
            "Сумма",
            "Категория",
            "Примечание",
            "Кто внёс",
            "Время записи",
        ])
        ws.format("A1:J1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
        })

    return ws


def get_next_row_num(ws) -> int:
    all_vals = ws.get_all_values()
    data_rows = [row for row in all_vals[1:] if any(cell.strip() for cell in row)]
    return len(data_rows) + 1


def extract_json_object(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.error("OpenAI returned text without a JSON object: %r", raw)
        raise ValueError(f"OpenAI вернул не JSON: {raw}")

    json_text = cleaned[start:end + 1].strip()
    json_text = json_text.translate(str.maketrans({
        "“": "\"",
        "”": "\"",
        "„": "\"",
        "‟": "\"",
        "«": "\"",
        "»": "\"",
        "‘": "'",
        "’": "'",
        "\u00a0": " ",
    }))

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse OpenAI JSON. Extracted: %r. Raw: %r", json_text, raw)
        raise ValueError(f"OpenAI вернул не JSON: {raw}") from e


def parse_amount(value) -> float:
    if isinstance(value, (int, float)):
        return round(float(value), 2)

    text = str(value or "").replace(",", ".").replace(" ", "").replace("€", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(match.group()), 2) if match else 0.0


def normalize_receipt(receipt: dict) -> dict:
    category = str(receipt.get("category") or "Другое").strip()
    if category not in CATEGORIES:
        category = "Другое"

    return {
        "date": str(receipt.get("date") or "Не указано").strip(),
        "store": str(receipt.get("store") or "Не указано").strip(),
        "items": str(receipt.get("items") or "Не указано").strip(),
        "amount": parse_amount(receipt.get("amount")),
        "currency": str(receipt.get("currency") or "EUR").strip(),
        "category": category,
    }


async def recognize(image_bytes: bytes) -> dict:
    if not image_bytes:
        raise ValueError("Фото не найдено. Начни заново: /start")

    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    response = await client.responses.create(
        model="gpt-4o-mini",
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Ты профессионально распознаёшь кассовые чеки. "
                            "Верни только JSON по заданной схеме. "
                            "Никакого markdown, пояснений или текста вне JSON."
                        ),
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Распознай чек на фото. "
                            "Дата должна быть в формате DD.MM.YYYY. "
                            "amount верни строкой через точку, например \"34.50\". "
                            "В amount нельзя использовать запятую, знак валюты или текст. "
                            "currency верни EUR, если чек из Франции/Монако/Европы. "
                            "category выбери только из: Продукты, Транспорт, Офис, "
                            "Техника, Услуги, Ресторан, Одежда, Другое."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{b64}",
                    },
                ],
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "receipt_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "date": {"type": "string"},
                        "store": {"type": "string"},
                        "items": {"type": "string"},
                        "amount": {"type": "string"},
                        "currency": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "Продукты",
                                "Транспорт",
                                "Офис",
                                "Техника",
                                "Услуги",
                                "Ресторан",
                                "Одежда",
                                "Другое",
                            ],
                        },
                    },
                    "required": [
                        "date",
                        "store",
                        "items",
                        "amount",
                        "currency",
                        "category",
                    ],
                },
            },
        },
    )

    raw = response.output_text or ""
    raw = raw.strip()

    logger.info("OPENAI RAW RESPONSE: %s", raw)

    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"OPENAI RAW RESPONSE IS NOT JSON: {raw}") from e

    return normalize_receipt(data)


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
        await update.message.reply_text("Пожалуйста напиши своё имя.")
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
            "Пожалуйста пришли именно фото чека 📸\n"
            "Сфотографируй чек и отправь в этот чат."
        )
        return ASK_PHOTO

    file = await update.message.photo[-1].get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    ctx.user_data["photo"] = image_bytes
    ctx.user_data["file_id"] = update.message.photo[-1].file_id

    await update.message.reply_text(
        "✍️ Напиши примечание — для чего куплено?\n\n"
        "Например: офисные расходы, командировка, личные нужды...\n\n"
        "Или напиши /skip"
    )
    return ASK_NOTE


async def save_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, note: str):
    name = ctx.user_data.get("name", "Неизвестно")
    image_bytes = ctx.user_data.get("photo")
    file_id = ctx.user_data.get("file_id", "")

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
            receipt["category"],
            note,
            name,
            timestamp,
        ])

        await msg.edit_text(
            f"✅ Записано в строку #{row_num}!\n\n"
            f"📅 Дата: {receipt['date']}\n"
            f"🏪 Магазин: {receipt['store']}\n"
            f"🛍 Товары: {receipt['items']}\n"
            f"🏷 Категория: {receipt['category']}\n"
            f"💰 Сумма: {receipt['amount']} грн\n"
            f"📝 Примечание: {note or '—'}\n"
            f"👤 Внёс: {name}"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Добавить ещё чек", callback_data="continue"),
            InlineKeyboardButton("✅ Готово", callback_data="done"),
        ]])
        await update.message.reply_text(
            "Хочешь добавить ещё один чек?",
            reply_markup=keyboard,
        )

        ctx.user_data.pop("photo", None)
        ctx.user_data.pop("file_id", None)
        return ASK_CONTINUE

    except Exception as e:
        logger.exception("Receipt processing failed")
        await msg.edit_text(f"❌ Ошибка обработки:\n{str(e)}\n\nНачни снова: /start")

    ctx.user_data.clear()
    return ConversationHandler.END


async def got_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    return await save_receipt(update, ctx, note)


async def skip_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await save_receipt(update, ctx, "")


async def continue_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "continue":
        name = ctx.user_data.get("name", "")
        if name:
            await query.message.reply_text(f"📸 Пришли фото следующего чека, {name}!")
            return ASK_PHOTO

        await query.message.reply_text("Как тебя зовут?")
        return ASK_NAME

    ctx.user_data.clear()
    await query.message.reply_text(
        "Спасибо! Все чеки записаны в таблицу 📊\n\n"
        "Чтобы добавить новый чек — напиши /start"
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "Отменено.\nЧтобы начать снова — напиши /start"
    )
    return ConversationHandler.END


def row_belongs_to_month(date_cell: str, today: date) -> bool:
    try:
        receipt_date = datetime.strptime(date_cell, "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return False

    return receipt_date.year == today.year and receipt_date.month == today.month


async def send_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]

    if today.day != last_day:
        return

    try:
        ws = get_sheet()
        all_rows = ws.get_all_values()

        month_str = today.strftime("%m.%Y")
        totals = {}
        grand_total = 0

        for row in all_rows[1:]:
            if len(row) < 7 or not row_belongs_to_month(row[2], today):
                continue

            try:
                amount = float(str(row[5]).replace(",", ".")) if row[5] else 0
                category = row[6] or "Другое"
                totals[category] = totals.get(category, 0) + amount
                grand_total += amount
            except ValueError:
                continue

        if not totals:
            report = f"📊 Отчёт за {month_str}\n\nДанных за этот месяц нет."
        else:
            lines = [f"📊 *Отчёт за {month_str}*\n"]
            for category, total in sorted(totals.items(), key=lambda item: -item[1]):
                lines.append(f"• {category}: *{total:.2f} грн*")
            lines.append(f"\n💰 *Итого: {grand_total:.2f} грн*")
            report = "\n".join(lines)

        chat_id = REPORT_CHAT_ID or context.job.chat_id
        await context.bot.send_message(
            chat_id=chat_id,
            text=report,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.exception("Report sending failed: %s", e)


async def report_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ws = get_sheet()
        all_rows = ws.get_all_values()
        today = date.today()
        month_str = today.strftime("%m.%Y")

        totals = {}
        grand_total = 0

        for row in all_rows[1:]:
            if len(row) < 7 or not row_belongs_to_month(row[2], today):
                continue

            try:
                amount = float(str(row[5]).replace(",", ".")) if row[5] else 0
                category = row[6] or "Другое"
                totals[category] = totals.get(category, 0) + amount
                grand_total += amount
            except ValueError:
                continue

        if not totals:
            await update.message.reply_text(f"За {month_str} данных пока нет.")
            return

        lines = [f"📊 *Отчёт за {month_str}*\n"]
        for category, total in sorted(totals.items(), key=lambda item: -item[1]):
            pct = (total / grand_total * 100) if grand_total else 0
            lines.append(f"• {category}: *{total:.2f} грн* ({pct:.0f}%)")
        lines.append(f"\n💰 *Итого: {grand_total:.2f} грн*")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.exception("Report command failed")
        await update.message.reply_text(f"Ошибка: {str(e)}")


def main():
    missing = [
        name for name, value in {
            "BOT_TOKEN": BOT_TOKEN,
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "SHEET_ID": SHEET_ID,
            "GOOGLE_CREDS_JSON": GOOGLE_CREDS_JSON,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
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
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("report", report_command))

    if app.job_queue:
        app.job_queue.run_daily(
            send_monthly_report,
            time=datetime.strptime("20:00", "%H:%M").time(),
        )
    else:
        logger.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
