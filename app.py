import os
import threading
import asyncio
from flask import Flask, request, jsonify
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from handlers.bot_handlers import start, callbacks, text_message, admin_command

TOKEN = os.getenv("BOT_TOKEN", "").strip()
app = Flask(__name__)

@app.get("/")
def health():
    return "TigerBot is running", 200

@app.post("/webhook/zapupi")
def zapupi_webhook():
    from services.zapupi import handle_webhook
    data = request.get_json(silent=True) or {}
    handle_webhook(data)
    return jsonify({"status": "ok"}), 200

async def run_bot():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

def start_bot_thread():
    if not TOKEN:
        print("BOT_TOKEN is not set; bot polling will not start.")
        return
    def runner():
        asyncio.run(run_bot())
    threading.Thread(target=runner, daemon=True, name="telegram-bot").start()

start_bot_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
