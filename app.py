
import os, threading, asyncio
from flask import Flask, request, jsonify
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from handlers.bot_handlers import start, callbacks, text_message, admin_command

TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")

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
    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("admin", admin_command))
    bot.add_handler(CallbackQueryHandler(callbacks))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling()
    await asyncio.Event().wait()

def start_bot_thread():
    if not TOKEN:
        print("BOT_TOKEN is not set; bot polling will not start.")
        return
    def runner():
        asyncio.run(run_bot())
    threading.Thread(target=runner, daemon=True).start()

start_bot_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
