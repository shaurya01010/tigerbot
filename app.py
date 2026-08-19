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
    try:
        handle_webhook(data)
    except Exception as exc:
        print(f"[ZapUPI Webhook Error] {type(exc).__name__}: {exc}")
    return jsonify({"status": "ok"}), 200

def build_application():
    application = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return application

async def run_bot():
    if not TOKEN:
        print("[Telegram] BOT_TOKEN is not set.")
        return

    while True:
        application = build_application()
        try:
            print("[Telegram] Initializing...")
            await application.initialize()
            print("[Telegram] Starting application...")
            await application.start()
            print("[Telegram] Starting polling...")
            await application.updater.start_polling(drop_pending_updates=True)
            print("[Telegram] Bot is running successfully!")
            await asyncio.Event().wait()
        except Exception as exc:
            print(f"[Telegram] Connection error: {type(exc).__name__}: {exc}")
            print("[Telegram] Retrying in 15 seconds...")
            try:
                if application.updater and application.updater.running:
                    await application.updater.stop()
            except Exception:
                pass
            try:
                if application.running:
                    await application.stop()
            except Exception:
                pass
            try:
                await application.shutdown()
            except Exception:
                pass
            await asyncio.sleep(15)

def start_bot_thread():
    if not TOKEN:
        print("[Telegram] BOT_TOKEN is not set; bot polling will not start.")
        return
    def runner():
        asyncio.run(run_bot())
    threading.Thread(target=runner, daemon=True, name="telegram-bot").start()

start_bot_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
