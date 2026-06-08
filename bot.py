import asyncio
import base64
import calendar
import io
import json
import logging
import os
import re
from datetime import date, datetime

import gspread
import openai
from google.oauth2 import credentials as oauth2_credentials
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
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
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

ASK_NAME, ASK_PHOTO, ASK_NOTE, ASK_CONTINUE = range(4)

CATEGORIES = {"Продукты", "Транспорт", "Офис", "Техника", "Услуги", "Ресторан", "Одежда", "Другое"}

RECEIPT_HEADERS = ["№", "Фото (Google Drive)", "Дата чека", "Магазин", "Товары", "Сумма", "Валюта", "Категория", "Примечание", "Кто внёс", "Время записи"]
RECEIPT_FIELDS = ["date", "store", "items", "amount", "currency", "category"]

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheet():
    logger.info("GOOGLE_CREDS_JSON length: %s", len(GOOGLE_CREDS_JSON) if GOOGLE_CREDS_JSON else "None")
    try:
        creds_data = json.loads(GOOGLE_CREDS_JSON)
    except Exception as e:
        logger.error("JSON ERROR in GOOGLE_CREDS_JSON: %s", e)
        logger.error("RAW repr: %s", repr(GOOGLE_CREDS_JSON))
        raise
    creds = Credentials.from_service_account_info(creds_data, scopes=SHEETS_SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)
    try:
        ws = sheet.worksheet("Чеки")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet("Чеки", rows=2000, cols=11)
        ws.append_row(RECEIPT_HEADERS)
    ws.format("A1:K1", {"textFormat": {"bold": True}})
    return ws


def get_drive_client():
    if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN]):
        raise RuntimeError("Missing OAuth env vars: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET or GOOGLE_REFRESH_TOKEN")
    creds = oauth2_credentials.Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_next_row_num(ws) -> int:
    all_vals = ws.get_all_values()
    data_rows = [row for row in all_vals[1:] if any(cell.strip() for cell in row)]
    return len(data_rows) + 1


def parse_amount(value) -> float:
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = re.sub(r"[^\d,.\-]", "", str(value or ""))
    if not text:
        return 0.0
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(match.group()), 2) if match else 0.0


def receipt_text(value, default="Не указано") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        text = ", ".join(str(i).strip() for i in value if str(i).strip())
    else:
        text = str(value).strip()
    return re.sub(r"\s+", " ", text).strip() or default


def get_or_create_month_folder(drive_client, root_folder_id: str, month_str: str) -> str:
    query = (
        f"name = '{month_str}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{root_folder_id}' in parents "
        "and trashed = false"
    )
    result = drive_client.files().list(q=query, fields="files(id, name)").execute()
    files = result.get("files", [])
    if files:
        folder_id = files[0]["id"]
        logger.info("Month folder '%s' found: %s", month_str, folder_id)
        return folder_id

    metadata = {
        "name": month_str,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [root_folder_id],
    }
    folder = drive_client.files().create(body=metadata, fields="id").execute()
    folder_id = folder["id"]
    logger.info("Month folder '%s' created: %s", month_str, folder_id)
    return folder_id


def upload_receipt_to_drive(drive_client, image_bytes: bytes, filename: str, month_folder_id: str) -> str:
    file_metadata = {
        "name": filename,
        "parents": [month_folder_id],
    }
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg", resumable=False)
    uploaded = drive_client.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
    ).execute()
    file_id = uploaded["id"]

    drive_client.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    public_url = f"https://drive.google.com/file/d/{file_id}/view"
    logger.info("Uploaded to Drive: %s → %s", filename, public_url)
    return public_url


def upload_photo_to_drive(image_bytes: bytes, user_id: int, timestamp: datetime) -> str:
    if not GOOGLE_DRIVE_FOLDER_ID:
        logger.error("GOOGLE_DRIVE_FOLDER_ID is missing")
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is not set in environment variables")
    drive_client = get_drive_client()
    month_str = timestamp.strftime("%Y-%m")
    month_folder_id = get_or_create_month_folder(drive_client, GOOGLE_DRIVE_FOLDER_ID, month_str)
    filename = f"receipt_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}_{user_id}.jpg"
    return upload_receipt_to_drive(drive_client, image_bytes, filename, month_folder_id)


async def recognize(image_bytes: bytes) -> dict:
    if not image_bytes:
        raise ValueError("Фото не найдено. Начни заново: /start")

    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=1500,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты профессионально распознаёшь кассовые чеки. "
                    "Отвечай только в формате KEY=VALUE, по одному полю на строку, без JSON, без markdown."
                )
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Распознай чек и верни строго в таком формате (каждое поле на новой строке):\n"
                            "DATE=03.06.2026\n"
                            "STORE=название магазина\n"
                            "ITEMS=товар 1; товар 2; товар 3\n"
                            "AMOUNT=34.50\n"
                            "CURRENCY=EUR\n"
                            "CATEGORY=Другое\n\n"
                            "Правила: "
                            "DATE в формате DD.MM.YYYY; "
                            "AMOUNT — число через точку без символа валюты; "
                            "CURRENCY — EUR для Европы/Монако; "
                            "ITEMS — товары через точку с запятой, без переносов строк; "
                            "CATEGORY только из: Продукты, Транспорт, Офис, Техника, Услуги, Ресторан, Одежда, Другое. "
                            "Если что-то не видно — пиши Не указано."
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
                    }
                ]
            }
        ]
    )

    raw = response.choices[0].message.content or ""
    raw = raw.strip()
    logger.info("OPENAI RAW: %s", raw)

    data = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip().upper()] = value.strip()

    category = data.get("CATEGORY", "Другое")
    if category not in CATEGORIES:
        category = "Другое"

    currency = data.get("CURRENCY", "EUR").upper()
    if currency in {"EURO", "EUROS", "€"}:
        currency = "EUR"

    return {
        "date": data.get("DATE", "Не указано"),
        "store": data.get("STORE", "Не указано"),
        "items": data.get("ITEMS", "Не указано"),
        "amount": parse_amount(data.get("AMOUNT")),
        "currency": currency,
        "category": category,
    }


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("👋 Привет! Я помогу записать чек в таблицу бухгалтера.\n\nКак тебя зовут?")
    return ASK_NAME


async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Пожалуйста напиши своё имя.")
        return ASK_NAME
    ctx.user_data["name"] = name
    await update.message.reply_text(f"Отлично, {name}! 📸\n\nТеперь пришли фото чека.")
    return ASK_PHOTO


async def got_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста пришли фото чека 📸")
        return ASK_PHOTO
    photo = update.message.photo[-1]
    logger.info("TELEGRAM PHOTO FILE_ID = %s", photo.file_id)
    tg_file = await photo.get_file()
    image_bytes = bytes(await tg_file.download_as_bytearray())
    logger.info("IMAGE_BYTES TYPE = %s", type(image_bytes))
    logger.info("IMAGE_BYTES SIZE = %s", len(image_bytes))
    if len(image_bytes) == 0:
        logger.error("IMAGE_BYTES IS EMPTY")
        await update.message.reply_text("❌ Фото пустое, попробуй отправить ещё раз.")
        return ASK_PHOTO
    ctx.user_data["photo"] = image_bytes
    ctx.user_data["user_id"] = update.effective_user.id
    await update.message.reply_text(
        "✍️ Напиши примечание — для чего куплено?\n\n"
        "Например: офисные расходы, командировка...\n\n"
        "Или напиши /skip"
    )
    return ASK_NOTE


async def save_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, note: str):
    name = ctx.user_data.get("name", "Неизвестно")
    image_bytes = ctx.user_data.get("photo")
    user_id = ctx.user_data.get("user_id", 0)

    msg = await update.message.reply_text("⏳ Распознаю чек через ИИ...")

    try:
        receipt = await recognize(image_bytes)
        now = datetime.now()

        # Upload photo to Google Drive
        logger.info("Starting Drive upload: image_bytes size=%s, user_id=%s", len(image_bytes) if image_bytes else 0, user_id)
        try:
            photo_url = await asyncio.to_thread(upload_photo_to_drive, image_bytes, user_id, now)
            logger.info("Drive upload OK: %s", photo_url)
        except Exception as drive_err:
            logger.error("Drive upload FAILED: %s", type(drive_err).__name__, exc_info=True)
            photo_url = f"Ошибка: {type(drive_err).__name__}: {str(drive_err)[:100]}"

        logger.info("GOOGLE_CREDS_JSON OK")
        logger.info("Connecting to Google Sheets...")
        ws = await asyncio.to_thread(get_sheet)
        row_num = await asyncio.to_thread(get_next_row_num, ws)
        timestamp = now.strftime("%d.%m.%Y %H:%M")

        row_data = [
            row_num, photo_url, receipt["date"], receipt["store"],
            receipt["items"], receipt["amount"], receipt["currency"],
            receipt["category"], note, name, timestamp,
        ]
        logger.info("Writing row: %s", row_data)
        await asyncio.to_thread(ws.append_row, row_data)
        logger.info("GOOGLE SHEETS WRITE SUCCESS")

        await msg.edit_text(
            f"✅ Записано в строку #{row_num}!\n\n"
            f"📅 Дата: {receipt['date']}\n"
            f"🏪 Магазин: {receipt['store']}\n"
            f"🛍 Товары: {receipt['items']}\n"
            f"🏷 Категория: {receipt['category']}\n"
            f"💰 Сумма: {receipt['amount']} {receipt['currency']}\n"
            f"📝 Примечание: {note or '—'}\n"
            f"👤 Внёс: {name}"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Добавить ещё чек", callback_data="continue"),
            InlineKeyboardButton("✅ Готово", callback_data="done"),
        ]])
        await update.message.reply_text("Хочешь добавить ещё один чек?", reply_markup=keyboard)
        ctx.user_data.pop("photo", None)
        ctx.user_data.pop("user_id", None)
        return ASK_CONTINUE

    except Exception as e:
        logger.exception("Ошибка обработки чека")
        await msg.edit_text(f"❌ Ошибка: {str(e)}\n\nНачни снова: /start")
        ctx.user_data.clear()
        return ConversationHandler.END


async def got_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await save_receipt(update, ctx, update.message.text.strip())


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
    await query.message.reply_text("Спасибо! Все чеки записаны 📊\n\nЧтобы добавить новый — напиши /start")
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено. Напиши /start чтобы начать снова.")
    return ConversationHandler.END


async def report_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ws = await asyncio.to_thread(get_sheet)
        all_rows = await asyncio.to_thread(ws.get_all_values)
        today = date.today()
        month_str = today.strftime("%m.%Y")
        totals = {}
        grand_total = 0

        for row in all_rows[1:]:
            if len(row) < 8:
                continue
            try:
                d = datetime.strptime(row[2], "%d.%m.%Y").date()
                if d.year != today.year or d.month != today.month:
                    continue
                amount = parse_amount(row[5])
                currency = row[6] or "EUR"
                category = row[7] or "Другое"
                totals[(category, currency)] = totals.get((category, currency), 0) + amount
                grand_total += amount
            except Exception:
                continue

        if not totals:
            await update.message.reply_text(f"За {month_str} данных пока нет.")
            return

        lines = [f"📊 *Отчёт за {month_str}*\n"]
        for (cat, cur), total in sorted(totals.items(), key=lambda x: -x[1]):
            pct = (total / grand_total * 100) if grand_total else 0
            lines.append(f"• {cat}: *{total:.2f} {cur}* ({pct:.0f}%)")
        lines.append(f"\n💰 *Итого: {grand_total:.2f}*")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)}")


async def send_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    if today.day != calendar.monthrange(today.year, today.month)[1]:
        return
    try:
        ws = await asyncio.to_thread(get_sheet)
        all_rows = await asyncio.to_thread(ws.get_all_values)
        month_str = today.strftime("%m.%Y")
        totals = {}
        grand_total = 0
        for row in all_rows[1:]:
            if len(row) < 8:
                continue
            try:
                d = datetime.strptime(row[2], "%d.%m.%Y").date()
                if d.year != today.year or d.month != today.month:
                    continue
                amount = parse_amount(row[5])
                currency = row[6] or "EUR"
                category = row[7] or "Другое"
                totals[(category, currency)] = totals.get((category, currency), 0) + amount
                grand_total += amount
            except Exception:
                continue
        if not totals:
            report = f"📊 Отчёт за {month_str}\n\nДанных нет."
        else:
            lines = [f"📊 *Отчёт за {month_str}*\n"]
            for (cat, cur), total in sorted(totals.items(), key=lambda x: -x[1]):
                lines.append(f"• {cat}: *{total:.2f} {cur}*")
            lines.append(f"\n💰 *Итого: {grand_total:.2f}*")
            report = "\n".join(lines)
        chat_id = REPORT_CHAT_ID or context.job.chat_id
        await context.bot.send_message(chat_id=chat_id, text=report, parse_mode="Markdown")
    except Exception as e:
        logger.exception("Ошибка отчёта: %s", e)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            ASK_PHOTO: [MessageHandler(filters.PHOTO, got_photo), MessageHandler(filters.TEXT & ~filters.COMMAND, got_photo)],
            ASK_NOTE: [CommandHandler("skip", skip_note), MessageHandler(filters.TEXT & ~filters.COMMAND, got_note)],
            ASK_CONTINUE: [CallbackQueryHandler(continue_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("report", report_command))
    if app.job_queue:
        app.job_queue.run_daily(send_monthly_report, time=datetime.strptime("20:00", "%H:%M").time())
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
