import asyncio
import io
import os
import time
import uuid

import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import (
    upsert_user, active_key, activate_key, create_key, disable_key,
    all_users, save_order, get_order, keys_page,
)
from services.zapupi import create_order, order_status, fulfill_success
from services.prediction import get_prediction

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PRICE = {
    1: float(os.getenv("PRICE_1D", "20")),
    7: float(os.getenv("PRICE_7D", "99")),
    30: float(os.getenv("PRICE_30D", "249")),
}
SUPPORT = "@Tiger_Key"
BRAND = "🐯 <b>TIGER MOD</b>"
DIVIDER = "━━━━━━━━━━━━━━━━━━━━"


def msg(body: str) -> str:
    return f"{BRAND}\n\n{body}\n\n{DIVIDER}\n🛟 Support: {SUPPORT}"


def access_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑  ENTER KEY", callback_data="enter_key"),
         InlineKeyboardButton("🛒  BUY ACCESS", callback_data="buy")],
        [InlineKeyboardButton("🛟  SUPPORT", url="https://t.me/Tiger_Key")],
    ])


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯  PREDICTION", callback_data="prediction")],
        [InlineKeyboardButton("🔑  MY ACCESS", callback_data="mykey"),
         InlineKeyboardButton("🛒  BUY ACCESS", callback_data="buy")],
        [InlineKeyboardButton("🛟  SUPPORT", url="https://t.me/Tiger_Key")],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  CREATE KEY", callback_data="acreate"),
         InlineKeyboardButton("📋  KEY LIST", callback_data="akeys")],
        [InlineKeyboardButton("⛔  DISABLE KEY", callback_data="astop")],
        [InlineKeyboardButton("📢  BROADCAST", callback_data="abroadcast")],
    ])


def plans_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"1 DAY  •  ₹{PRICE[1]:g}", callback_data="plan_1")],
        [InlineKeyboardButton(f"7 DAYS  •  ₹{PRICE[7]:g}", callback_data="plan_7")],
        [InlineKeyboardButton(f"30 DAYS  •  ₹{PRICE[30]:g}", callback_data="plan_30")],
        [InlineKeyboardButton("❌  CANCEL", callback_data="cancel")],
    ])


def payment_keyboard(order_id, payment_url):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳  OPEN SECURE PAYMENT", url=payment_url)],
        [InlineKeyboardButton("✅  VERIFY PAYMENT", callback_data=f"verify_{order_id}")],
        [InlineKeyboardButton("❌  CANCEL ORDER", callback_data=f"cancel_{order_id}")],
    ])


def money(amount):
    return f"₹{amount:,.2f}".rstrip("0").rstrip(".")


def qr_bytes(url):
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image()
    out = io.BytesIO()
    image.save(out, format="PNG")
    out.seek(0)
    out.name = "tiger-mod-payment.png"
    return out


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    a = active_key(update.effective_user.id)
    if a:
        await update.message.reply_text(
            msg(
                "👋 <b>Welcome back.</b>\n\n"
                "Your premium access is active and ready.\n\n"
                f"🔐 <b>Status:</b> ACTIVE\n"
                f"📦 <b>Plan:</b> {a['plan_days']} Days\n"
                f"⏳ <b>Expires:</b> {a['expires_at']}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
    else:
        await update.message.reply_text(
            msg(
                "👋 <b>Welcome to Tiger Mod.</b>\n\n"
                "Premium access is required to use the prediction service.\n\n"
                "🔐 <b>Access status:</b> INACTIVE\n\n"
                "Choose an option below to continue."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=access_keyboard(),
        )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(
            msg("🛠 <b>Administrator Panel</b>\n\nManage keys, access and broadcasts from the controls below."),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "enter_key":
        context.user_data["mode"] = "key"
        await q.message.reply_text(
            msg(
                "🔑 <b>Activate Your Key</b>\n\n"
                "Send the access key you received from <b>@Tiger_Key</b>.\n\n"
                "Example:\n<code>TG-XXXXXXXXXXXXXXX</code>\n\n"
                "🔒 A key can be activated on one Telegram account only."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if q.data == "buy":
        await q.message.reply_text(
            msg(
                "🛒 <b>Choose Your Access Plan</b>\n\n"
                "Select the duration that suits you.\n\n"
                "💳 Secure UPI payment\n"
                "⚡ Instant verification\n"
                "🔐 One key per Telegram account"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=plans_keyboard(),
        )
        return

    if q.data.startswith("plan_"):
        days = int(q.data.split("_")[1])
        amount = PRICE[days]
        # Alphanumeric + timestamp/uuid: unique and friendly to gateway rules.
        order_id = f"TGR{uid}{int(time.time())}{uuid.uuid4().hex[:6].upper()}"
        save_order(order_id, uid, days, amount)
        try:
            data = create_order(order_id, amount, f"Tiger Mod | Plan {days} Days | User {uid}")
            payment_url = data["payment_url"]
            from database import update_order
            update_order(order_id, payment_url=payment_url, txn_id=data.get("txn_id", ""))

            caption = msg(
                "💳 <b>Payment Request</b>\n\n"
                f"📦 <b>Plan:</b> {days} Days\n"
                f"💰 <b>Amount:</b> {money(amount)}\n"
                f"🧾 <b>Order ID:</b> <code>{order_id}</code>\n\n"
                "Scan the QR code or open the secure payment page.\n"
                "After payment, press <b>VERIFY PAYMENT</b>."
            )
            await q.message.reply_photo(
                photo=qr_bytes(payment_url),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=payment_keyboard(order_id, payment_url),
            )
        except Exception as exc:
            print("ZapUPI create-order error:", repr(exc))
            await q.message.reply_text(
                msg(
                    "❌ <b>Payment Order Could Not Be Created</b>\n\n"
                    "The payment gateway did not accept the order. Your access has not been charged.\n\n"
                    f"🧾 <b>Order:</b> <code>{order_id}</code>\n"
                    "Please try again. If the issue continues, contact support."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛒  TRY AGAIN", callback_data="buy")],
                    [InlineKeyboardButton("🛟  SUPPORT", url="https://t.me/Tiger_Key")],
                ]),
            )
        return

    if q.data.startswith("verify_"):
        order_id = q.data[7:]
        order = get_order(order_id)
        if not order or order["telegram_id"] != uid:
            await q.message.reply_text(msg("❌ <b>Order Not Found</b>\n\nThis payment order is not linked to your account."), parse_mode=ParseMode.HTML)
            return
        try:
            data = order_status(order_id)
            details = data.get("data", {})
            status = details.get("status")
            if status == "Success":
                key = fulfill_success(order_id, details.get("txn_id", ""), details.get("utr", ""))
                await q.message.reply_text(
                    msg(
                        "✅ <b>Payment Verified Successfully</b>\n\n"
                        f"📦 <b>Plan:</b> {order['plan_days']} Days\n"
                        f"💰 <b>Paid:</b> {money(order['amount'])}\n"
                        f"🔑 <b>Your Access Key:</b>\n<code>{key}</code>\n\n"
                        "Enter this key using <b>ENTER KEY</b> to activate your access."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=access_keyboard(),
                )
            elif status == "Pending":
                await q.message.reply_text(
                    msg("⏳ <b>Payment Still Pending</b>\n\nWe could not confirm the payment yet. Complete the payment and press <b>VERIFY PAYMENT</b> again."),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄  VERIFY AGAIN", callback_data=f"verify_{order_id}")],
                        [InlineKeyboardButton("❌  CANCEL", callback_data=f"cancel_{order_id}")],
                    ]),
                )
            else:
                await q.message.reply_text(
                    msg("❌ <b>Payment Failed</b>\n\nNo access key was issued. You can create a new payment order whenever you are ready."),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒  BUY ACCESS", callback_data="buy")]]),
                )
        except Exception as exc:
            print("ZapUPI verify error:", repr(exc))
            await q.message.reply_text(
                msg("⚠️ <b>Verification Temporarily Unavailable</b>\n\nPlease wait a moment and try again."),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄  TRY AGAIN", callback_data=f"verify_{order_id}")]]),
            )
        return

    if q.data == "cancel":
        await q.message.reply_text(msg("❌ <b>Selection Cancelled</b>\n\nNo payment order was created."), parse_mode=ParseMode.HTML, reply_markup=access_keyboard())
        return

    if q.data.startswith("cancel_"):
        order_id = q.data[7:]
        order = get_order(order_id)
        if order and order["telegram_id"] == uid and order["status"] == "Pending":
            from database import update_order
            update_order(order_id, status="Cancelled")
        await q.message.reply_text(msg("❌ <b>Payment Order Cancelled</b>\n\nNo access key has been issued for this order."), parse_mode=ParseMode.HTML, reply_markup=access_keyboard())
        return

    if q.data == "prediction":
        if not active_key(uid):
            await q.message.reply_text(msg("🔒 <b>Premium Access Required</b>\n\nYour account does not have an active key."), parse_mode=ParseMode.HTML, reply_markup=access_keyboard())
            return
        context.user_data["mode"] = "period"
        await q.message.reply_text(
            msg("🎯 <b>Prediction</b>\n\nEnter the period number you want to check."),
            parse_mode=ParseMode.HTML,
        )
        return

    if q.data == "mykey":
        a = active_key(uid)
        if a:
            body = (
                "🔑 <b>My Access</b>\n\n"
                "🟢 <b>Status:</b> ACTIVE\n"
                f"📦 <b>Plan:</b> {a['plan_days']} Days\n"
                f"⏳ <b>Expires:</b> {a['expires_at']}\n"
                f"🔐 <b>Key:</b> <code>{a['key']}</code>"
            )
        else:
            body = "🔒 <b>My Access</b>\n\nYour account currently has no active key."
        await q.message.reply_text(msg(body), parse_mode=ParseMode.HTML, reply_markup=main_keyboard() if a else access_keyboard())
        return

    if uid in ADMIN_IDS and q.data == "acreate":
        context.user_data["mode"] = "admin_create"
        await q.message.reply_text(msg("➕ <b>Create Access Key</b>\n\nEnter duration: <code>1</code>, <code>7</code>, or <code>30</code> days."), parse_mode=ParseMode.HTML)
        return

    if uid in ADMIN_IDS and q.data == "akeys":
        rows = keys_page()
        if rows:
            lines = ["🔑 <b>Recent Access Keys</b>\n"]
            for r in rows:
                lines.append(f"<code>{r['key']}</code>  •  {r['plan_days']}D  •  <b>{r['status'].upper()}</b>")
            body = "\n".join(lines)
        else:
            body = "🔑 <b>Access Keys</b>\n\nNo keys have been created yet."
        await q.message.reply_text(msg(body), parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return

    if uid in ADMIN_IDS and q.data == "astop":
        context.user_data["mode"] = "admin_stop"
        await q.message.reply_text(msg("⛔ <b>Disable Access Key</b>\n\nSend the key you want to disable."), parse_mode=ParseMode.HTML)
        return

    if uid in ADMIN_IDS and q.data == "abroadcast":
        context.user_data["mode"] = "broadcast"
        await q.message.reply_text(msg("📢 <b>Broadcast</b>\n\nSend the message you want to broadcast to all registered users."), parse_mode=ParseMode.HTML)
        return


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    upsert_user(update.effective_user)
    mode = context.user_data.pop("mode", None)
    text = (update.message.text or "").strip()

    if mode == "key":
        ok, result = activate_key(text, uid)
        body = (
            "✅ <b>Access Activated</b>\n\n" + result
            if ok else
            "❌ <b>Key Activation Failed</b>\n\n" + result
        )
        await update.message.reply_text(msg(body), parse_mode=ParseMode.HTML, reply_markup=main_keyboard() if ok else access_keyboard())
        return

    if mode == "period":
        if not active_key(uid):
            await update.message.reply_text(msg("🔒 <b>Access Expired</b>\n\nYour key is no longer active. Please activate a valid key or purchase a new plan."), parse_mode=ParseMode.HTML, reply_markup=access_keyboard())
            return
        result = get_prediction(text)
        await update.message.reply_text(msg(f"🎯 <b>Prediction Result</b>\n\n{result}"), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())
        return

    if mode == "admin_create" and uid in ADMIN_IDS:
        if text not in ("1", "7", "30"):
            await update.message.reply_text(msg("❌ <b>Invalid Duration</b>\n\nPlease enter <code>1</code>, <code>7</code>, or <code>30</code>."), parse_mode=ParseMode.HTML)
            return
        key = create_key(int(text))
        await update.message.reply_text(msg(f"✅ <b>Key Created</b>\n\n🔑 <code>{key}</code>\n📦 Plan: {text} Days\n🟢 Status: UNUSED"), parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return

    if mode == "admin_stop" and uid in ADMIN_IDS:
        ok = disable_key(text)
        await update.message.reply_text(msg("⛔ <b>Key Disabled</b>\n\nThe selected key can no longer be activated." if ok else "❌ <b>Key Not Found</b>\n\nNo matching key was found."), parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return

    if mode == "broadcast" and uid in ADMIN_IDS:
        sent = failed = 0
        broadcast = msg(f"📢 <b>Announcement</b>\n\n{text}")
        for target in all_users():
            try:
                await context.bot.send_message(target, broadcast, parse_mode=ParseMode.HTML)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        await update.message.reply_text(msg(f"📢 <b>Broadcast Complete</b>\n\n✅ Delivered: {sent}\n❌ Failed: {failed}"), parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return

    await update.message.reply_text(
        msg("ℹ️ <b>Use the buttons below</b>\n\nChoose an option to continue."),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard() if active_key(uid) else access_keyboard(),
    )
