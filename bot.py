import os, json, base64, logging, calendar
from datetime import datetime, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes, ConversationHandler,
                          CallbackQueryHandler, JobQueue)
import openai, gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
REPORT_CHAT_ID = os.getenv("REPORT_CHAT_ID")  # ID чата куда слать отчёт

ASK_NAME, ASK_PHOTO, ASK_NOTE, ASK_CONTINUE = range(4)

# === GOOGLE SHEETS ===
def get_sheet():
    creds_data = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)
    try:
        ws = sheet.worksheet("Чеки")
    except:
        ws = sheet.add_worksheet("Чеки", rows=2000, cols=10)
        ws.append_row([
            "№", "Фото (ссылка)", "Дата чека", "Магазин",
            "Товары", "Сумма", "Категория", "Примечание",
            "Кто внёс", "Время записи"
        ])
        ws.format("A1:J1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}
        })
    return ws

def get_next_row_num(ws) -> int:
    """Получаем следующий порядковый номер безопасно"""
    all_vals = ws.get_all_values()
    # Считаем только строки с данными (не пустые)
    data_rows = [r for r in all_vals[1:] if any(cell.strip() for cell in r)]
    return len(data_rows) + 1

# === OPENAI ===
async def recognize(image_bytes: bytes) -> dict:
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    b64 = base64.b64encode(image_bytes).decode()
    r = await client.chat.completions.create(
        model="gpt-4o", max_tokens=600,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": """Распознай чек и верни ТОЛЬКО JSON без markdown:
{
  "date": "DD.MM.YYYY",
  "store": "название магазина",
  "items": "товары через запятую",
  "amount": 0.00,
  "category": "одно из: Продукты/Транспорт/Офис/Техника/Услуги/Ресторан/Другое"
}
Если что-то не видно — пиши "Не указано". amount всегда число."""},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
                "detail": "high"
            }}
        ]}]
    )
    raw = r.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# === ДИАЛОГ ===

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
    ctx.user_data["message_id"] = update.message.message_id

    await update.message.reply_text(
        "✍️ Напиши примечание — для чего куплено?\n\n"
        "Например: офисные расходы, командировка, личные нужды...\n\n"
        "Или напиши /skip"
    )
    return ASK_NOTE

async def got_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    note = "" if text.startswith("/skip") else text

    name = ctx.user_data.get("name", "Неизвестно")
    image_bytes = ctx.user_data.get("photo")
    file_id = ctx.user_data.get("file_id", "")

    msg = await update.message.reply_text("⏳ Распознаю чек через ИИ...")

    try:
        receipt = await recognize(image_bytes)

        ws = get_sheet()
        row_num = get_next_row_num(ws)
        timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

        # Ссылка на фото — используем file_id как идентификатор
        bot_username = (await ctx.bot.get_me()).username
        photo_link = f"https://t.me/{bot_username}"

        ws.append_row([
            row_num,
            photo_link,
            receipt.get("date", ""),
            receipt.get("store", ""),
            receipt.get("items", ""),
            receipt.get("amount", 0),
            receipt.get("category", "Другое"),
            note,
            name,
            timestamp,
        ])

        amount = receipt.get('amount', 0)
        store = receipt.get('store', '—')
        category = receipt.get('category', '—')

        await msg.edit_text(
            f"✅ Записано в строку #{row_num}!\n\n"
            f"📅 Дата: {receipt.get('date', '—')}\n"
            f"🏪 Магазин: {store}\n"
            f"🛍 Товары: {receipt.get('items', '—')}\n"
            f"🏷 Категория: {category}\n"
            f"💰 Сумма: {amount} грн\n"
            f"📝 Примечание: {note or '—'}\n"
            f"👤 Внёс: {name}"
        )

        # Спрашиваем продолжить
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Добавить ещё чек", callback_data="continue"),
            InlineKeyboardButton("✅ Готово", callback_data="done"),
        ]])
        await update.message.reply_text(
            "Хочешь добавить ещё один чек?",
            reply_markup=keyboard
        )

    except json.JSONDecodeError:
        await msg.edit_text(
            "❌ Не удалось распознать чек.\n"
            "Попробуй сфотографировать чётче — чек должен быть хорошо виден.\n\n"
            "Начни снова: /start"
        )
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)}\n\nНачни снова: /start")

    ctx.user_data.clear()
    return ASK_CONTINUE

async def continue_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "continue":
        name = ctx.user_data.get("name", "")
        if name:
            await query.message.reply_text(f"📸 Пришли фото следующего чека, {name}!")
            return ASK_PHOTO
        else:
            await query.message.reply_text("Как тебя зовут?")
            return ASK_NAME
    else:
        await query.message.reply_text(
            "Спасибо! Все чеки записаны в таблицу 📊\n\n"
            "Чтобы добавить новый чек — напиши /start"
        )
        return ConversationHandler.END

async def skip_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update.message.text = "/skip"
    return await got_note(update, ctx)

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "Отменено.\nЧтобы начать снова — напиши /start"
    )
    return ConversationHandler.END

# === АВТООТЧЁТ ===
async def send_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет отчёт в последний день месяца"""
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]

    if today.day != last_day:
        return

    try:
        ws = get_sheet()
        all_rows = ws.get_all_values()

        month_str = today.strftime("%m.%Y")
        month_num = today.strftime("%m")

        totals = {}
        grand_total = 0

        for row in all_rows[1:]:
            if len(row) < 7:
                continue
            date_cell = row[2]  # Дата чека
            if not date_cell or month_num not in date_cell:
                continue
            try:
                amount = float(str(row[5]).replace(",", ".")) if row[5] else 0
                category = row[6] or "Другое"
                totals[category] = totals.get(category, 0) + amount
                grand_total += amount
            except:
                continue

        if not totals:
            report = f"📊 Отчёт за {month_str}\n\nДанных за этот месяц нет."
        else:
            lines = [f"📊 *Отчёт за {month_str}*\n"]
            for cat, total in sorted(totals.items(), key=lambda x: -x[1]):
                lines.append(f"• {cat}: *{total:.2f} грн*")
            lines.append(f"\n💰 *Итого: {grand_total:.2f} грн*")
            report = "\n".join(lines)

        chat_id = REPORT_CHAT_ID or context.job.chat_id
        await context.bot.send_message(
            chat_id=chat_id,
            text=report,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Ошибка отчёта: {e}")

async def report_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Команда /отчёт — показывает отчёт за текущий месяц"""
    try:
        ws = get_sheet()
        all_rows = ws.get_all_values()
        today = date.today()
        month_num = today.strftime("%m")
        month_str = today.strftime("%m.%Y")

        totals = {}
        grand_total = 0

        for row in all_rows[1:]:
            if len(row) < 7:
                continue
            date_cell = row[2]
            if not date_cell or month_num not in date_cell:
                continue
            try:
                amount = float(str(row[5]).replace(",", ".")) if row[5] else 0
                category = row[6] or "Другое"
                totals[category] = totals.get(category, 0) + amount
                grand_total += amount
            except:
                continue

        if not totals:
            await update.message.reply_text(f"За {month_str} данных пока нет.")
            return

        lines = [f"📊 *Отчёт за {month_str}*\n"]
        for cat, total in sorted(totals.items(), key=lambda x: -x[1]):
            pct = (total / grand_total * 100) if grand_total else 0
            lines.append(f"• {cat}: *{total:.2f} грн* ({pct:.0f}%)")
        lines.append(f"\n💰 *Итого: {grand_total:.2f} грн*")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {str(e)}")

# === ЗАПУСК ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)
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

    # Автоотчёт каждый день в 20:00
    app.job_queue.run_daily(
        send_monthly_report,
        time=datetime.strptime("20:00", "%H:%M").time(),
    )

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
