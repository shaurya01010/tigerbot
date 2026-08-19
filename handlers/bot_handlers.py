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
    upsert_user,
    active_key,
    activate_key,
    create_key,
    disable_key,
    all_users,
    save_order,
    get_order,
    keys_page,
)

from services.zapupi import create_order, order_status, fulfill_success
from services.prediction import get_prediction


# ============================================================
# CONFIGURATION
# ============================================================

ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

PRICE = {
    1: float(os.getenv("PRICE_1D", "20")),
    7: float(os.getenv("PRICE_7D", "99")),
    30: float(os.getenv("PRICE_30D", "249")),
}

SUPPORT = "@Tiger_Key"

BRAND = "🐯 <b>TIGER MOD</b>"
DIVIDER = "━━━━━━━━━━━━━━━━━━━━"


# ============================================================
# MESSAGE DESIGN
# ============================================================

def msg(body: str) -> str:
    return (
        f"{BRAND}\n\n"
        f"{body}\n\n"
        f"{DIVIDER}\n"
        f"🛟 Support: {SUPPORT}"
    )


# ============================================================
# KEYBOARDS
# ============================================================

def access_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔑  ENTER KEY",
                callback_data="enter_key"
            ),
            InlineKeyboardButton(
                "🛒  BUY ACCESS",
                callback_data="buy"
            ),
        ],
        [
            InlineKeyboardButton(
                "🛟  SUPPORT",
                url="https://t.me/Tiger_Key"
            )
        ],
    ])


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎯  PREDICTION",
                callback_data="prediction"
            )
        ],
        [
            InlineKeyboardButton(
                "🔑  MY ACCESS",
                callback_data="mykey"
            ),
            InlineKeyboardButton(
                "🛒  BUY ACCESS",
                callback_data="buy"
            ),
        ],
        [
            InlineKeyboardButton(
                "🛟  SUPPORT",
                url="https://t.me/Tiger_Key"
            )
        ],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕  CREATE KEY",
                callback_data="acreate"
            ),
            InlineKeyboardButton(
                "📋  KEY LIST",
                callback_data="akeys"
            ),
        ],
        [
            InlineKeyboardButton(
                "⛔  DISABLE KEY",
                callback_data="astop"
            )
        ],
        [
            InlineKeyboardButton(
                "📢  BROADCAST",
                callback_data="abroadcast"
            )
        ],
    ])


def plans_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"1 DAY  •  ₹{PRICE[1]:g}",
                callback_data="plan_1"
            )
        ],
        [
            InlineKeyboardButton(
                f"7 DAYS  •  ₹{PRICE[7]:g}",
                callback_data="plan_7"
            )
        ],
        [
            InlineKeyboardButton(
                f"30 DAYS  •  ₹{PRICE[30]:g}",
                callback_data="plan_30"
            )
        ],
        [
            InlineKeyboardButton(
                "❌  CANCEL",
                callback_data="cancel"
            )
        ],
    ])


def payment_keyboard(order_id, payment_url):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💳  OPEN SECURE PAYMENT",
                url=payment_url
            )
        ],
        [
            InlineKeyboardButton(
                "✅  VERIFY PAYMENT",
                callback_data=f"verify_{order_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌  CANCEL ORDER",
                callback_data=f"cancel_{order_id}"
            )
        ],
    ])


# ============================================================
# HELPERS
# ============================================================

def money(amount):
    return f"₹{amount:,.2f}".rstrip("0").rstrip(".")


def qr_bytes(url):
    qr = qrcode.QRCode(
        box_size=8,
        border=2
    )

    qr.add_data(url)
    qr.make(fit=True)

    image = qr.make_image()

    out = io.BytesIO()
    image.save(out, format="PNG")
    out.seek(0)
    out.name = "tiger-mod-payment.png"

    return out


# ============================================================
# /START
# ============================================================

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


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id in ADMIN_IDS:

        await update.message.reply_text(
            msg(
                "🛠 <b>Administrator Panel</b>\n\n"
                "Manage keys, access and broadcasts "
                "from the controls below."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query

    await q.answer()

    uid = q.from_user.id


    # ========================================================
    # ENTER KEY
    # ========================================================

    if q.data == "enter_key":

        context.user_data["mode"] = "key"

        await q.message.reply_text(
            msg(
                "🔑 <b>Activate Your Key</b>\n\n"
                "Send the access key you received from "
                "<b>@Tiger_Key</b>.\n\n"
                "Example:\n"
                "<code>TG-XXXXXXXXXXXXXXX</code>\n\n"
                "🔒 A key can be activated on one Telegram "
                "account only."
            ),
            parse_mode=ParseMode.HTML,
        )

        return


    # ========================================================
    # BUY ACCESS
    # ========================================================

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


    # ========================================================
    # CREATE PAYMENT ORDER
    # ========================================================

    if q.data.startswith("plan_"):

        days = int(q.data.split("_")[1])

        if days not in PRICE:
            await q.message.reply_text(
                msg(
                    "❌ <b>Invalid Plan</b>\n\n"
                    "Please select a valid plan."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=plans_keyboard(),
            )
            return

        amount = PRICE[days]

        # Generate a unique order ID.
        order_id = (
            f"TGR"
            f"{int(time.time() * 1000)}"
            f"{uuid.uuid4().hex[:8].upper()}"
        )

        save_order(
            order_id,
            uid,
            days,
            amount
        )

        try:

            # IMPORTANT:
            # order_id is the VARIABLE, not "order_id".
            data = create_order(
                order_id,
                amount,
                f"Tiger Mod | Plan {days} Days | User {uid}"
            )

            # Make sure the gateway returned a payment URL.
            payment_url = data.get("payment_url")

            if not payment_url:
                raise ValueError(
                    f"ZapUPI did not return payment_url: {data}"
                )

            from database import update_order

            update_order(
                order_id,
                payment_url=payment_url,
                txn_id=data.get("txn_id", "")
            )


            # ------------------------------------------------
            # PAYMENT MESSAGE
            # ------------------------------------------------

            caption = msg(
                "💳 <b>PAYMENT REQUEST</b>\n\n"

                f"📦 <b>Plan:</b> {days} Days\n"
                f"💰 <b>Amount:</b> {money(amount)}\n"
                f"🧾 <b>Order ID:</b> "
                f"<code>{order_id}</code>\n\n"

                "━━━━━━━━━━━━━━━━━━━━\n"

                "📱 <b>HOW TO PAY</b>\n\n"
                "1️⃣ Scan the QR code below\n"
                "2️⃣ Complete the UPI payment\n"
                "3️⃣ Return here\n"
                "4️⃣ Tap <b>VERIFY PAYMENT</b>\n\n"

                "⚡ Your key will be generated "
                "after successful verification.\n"
                "🔐 Payment is verified through ZapUPI."
            )

            await q.message.reply_photo(
                photo=qr_bytes(payment_url),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=payment_keyboard(
                    order_id,
                    payment_url
                ),
            )

        except Exception as exc:

            print(
                "ZapUPI create-order error:",
                repr(exc)
            )

            print(
                f"[ZapUPI] create-order failed "
                f"for {order_id}: {exc}"
            )

            await q.message.reply_text(
                msg(
                    "⚠️ <b>PAYMENT COULD NOT BE CREATED</b>\n\n"

                    "We couldn't create the payment order "
                    "at this moment.\n\n"

                    f"📦 <b>Plan:</b> {days} Days\n"
                    f"💰 <b>Amount:</b> {money(amount)}\n"
                    f"🧾 <b>Order:</b> "
                    f"<code>{order_id}</code>\n\n"

                    "🔒 <b>No payment has been charged.</b>\n\n"

                    "🔄 <b>Next step</b>\n"
                    "Tap <b>TRY AGAIN</b> below.\n\n"

                    "If the problem continues, contact "
                    "<b>@Tiger_Key</b>."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔄  TRY AGAIN",
                            callback_data="buy"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠  MAIN MENU",
                            callback_data="cancel"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🛟  CONTACT SUPPORT",
                            url="https://t.me/Tiger_Key"
                        )
                    ],
                ]),
            )

        return


    # ========================================================
    # VERIFY PAYMENT
    # ========================================================

    if q.data.startswith("verify_"):

        order_id = q.data[7:]

        order = get_order(order_id)

        if not order or order["telegram_id"] != uid:

            await q.message.reply_text(
                msg(
                    "❌ <b>ORDER NOT FOUND</b>\n\n"
                    "This payment order is not linked "
                    "to your Telegram account."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=access_keyboard(),
            )

            return

        try:

            data = order_status(order_id)

            details = data.get("data", {})

            status = details.get("status")


            # ------------------------------------------------
            # PAYMENT SUCCESS
            # ------------------------------------------------

            if status == "Success":

                key = fulfill_success(
                    order_id,
                    details.get("txn_id", ""),
                    details.get("utr", "")
                )

                await q.message.reply_text(
                    msg(
                        "✅ <b>PAYMENT VERIFIED</b>\n\n"

                        "🎉 Your payment has been "
                        "successfully verified.\n\n"

                        "━━━━━━━━━━━━━━━━━━━━\n"

                        f"📦 <b>Plan:</b> "
                        f"{order['plan_days']} Days\n"

                        f"💰 <b>Paid:</b> "
                        f"{money(order['amount'])}\n"

                        f"🧾 <b>Order:</b> "
                        f"<code>{order_id}</code>\n\n"

                        "🔑 <b>YOUR ACCESS KEY</b>\n"
                        f"<code>{key}</code>\n\n"

                        "🔐 Your key is now ready.\n"
                        "Tap <b>ENTER KEY</b> and activate it "
                        "on your Telegram account."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=access_keyboard(),
                )


            # ------------------------------------------------
            # PAYMENT PENDING
            # ------------------------------------------------

            elif status == "Pending":

                await q.message.reply_text(
                    msg(
                        "⏳ <b>PAYMENT STILL PENDING</b>\n\n"

                        "We haven't received confirmation "
                        "from the payment gateway yet.\n\n"

                        "💡 If you've already paid, wait a "
                        "few seconds and press "
                        "<b>VERIFY AGAIN</b>.\n\n"

                        "🔒 No access key has been issued yet."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "🔄  VERIFY AGAIN",
                                callback_data=f"verify_{order_id}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "❌  CANCEL",
                                callback_data=f"cancel_{order_id}"
                            )
                        ],
                    ]),
                )


            # ------------------------------------------------
            # PAYMENT FAILED
            # ------------------------------------------------

            else:

                await q.message.reply_text(
                    msg(
                        "❌ <b>PAYMENT NOT COMPLETED</b>\n\n"

                        "The payment was not confirmed "
                        "by the gateway.\n\n"

                        f"🧾 <b>Order:</b> "
                        f"<code>{order_id}</code>\n\n"

                        "🔒 No access key was issued.\n\n"

                        "You can create a new payment order "
                        "whenever you're ready."
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "🛒  BUY ACCESS",
                                callback_data="buy"
                            )
                        ]
                    ]),
                )

        except Exception as exc:

            print(
                "ZapUPI verify error:",
                repr(exc)
            )

            await q.message.reply_text(
                msg(
                    "⚠️ <b>VERIFICATION TEMPORARILY UNAVAILABLE</b>\n\n"

                    "We couldn't contact the payment gateway "
                    "right now.\n\n"

                    "Your order is still safe.\n"
                    "Please wait a moment and try again."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔄  TRY AGAIN",
                            callback_data=f"verify_{order_id}"
                        )
                    ]
                ]),
            )

        return


    # ========================================================
    # CANCEL PLAN SELECTION
    # ========================================================

    if q.data == "cancel":

        await q.message.reply_text(
            msg(
                "❌ <b>SELECTION CANCELLED</b>\n\n"
                "No payment order was created.\n\n"
                "You can purchase access whenever you're ready."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=access_keyboard(),
        )

        return


    # ========================================================
    # CANCEL PAYMENT ORDER
    # ========================================================

    if q.data.startswith("cancel_"):

        order_id = q.data[7:]

        order = get_order(order_id)

        if (
            order
            and order["telegram_id"] == uid
            and order["status"] == "Pending"
        ):

            from database import update_order

            update_order(
                order_id,
                status="Cancelled"
            )

        await q.message.reply_text(
            msg(
                "❌ <b>PAYMENT ORDER CANCELLED</b>\n\n"

                f"🧾 <b>Order:</b> "
                f"<code>{order_id}</code>\n\n"

                "No access key has been issued for "
                "this order."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=access_keyboard(),
        )

        return


    # ========================================================
    # PREDICTION
    # ========================================================

    if q.data == "prediction":

        if not active_key(uid):

            await q.message.reply_text(
                msg(
                    "🔒 <b>PREMIUM ACCESS REQUIRED</b>\n\n"

                    "Your account does not have an active key.\n\n"

                    "Activate an existing key or purchase "
                    "a new access plan to continue."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=access_keyboard(),
            )

            return

        context.user_data["mode"] = "period"

        await q.message.reply_text(
            msg(
                "🎯 <b>PREDICTION CENTER</b>\n\n"

                "Enter the <b>period number</b> you want "
                "to check.\n\n"

                "Example:\n"
                "<code>12345</code>\n\n"

                "🔐 Your premium access is active."
            ),
            parse_mode=ParseMode.HTML,
        )

        return


    # ========================================================
    # MY ACCESS
    # ========================================================

    if q.data == "mykey":

        a = active_key(uid)

        if a:

            body = (
                "🔑 <b>MY ACCESS</b>\n\n"

                "🟢 <b>Status:</b> ACTIVE\n"
                f"📦 <b>Plan:</b> {a['plan_days']} Days\n"
                f"⏳ <b>Expires:</b> {a['expires_at']}\n"
                f"🔐 <b>Key:</b> <code>{a['key']}</code>\n\n"

                "Your premium access is currently active."
            )

        else:

            body = (
                "🔒 <b>MY ACCESS</b>\n\n"

                "Your account currently has "
                "no active key.\n\n"

                "Purchase a plan or enter an existing key "
                "to continue."
            )

        await q.message.reply_text(
            msg(body),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
            if a
            else access_keyboard(),
        )

        return


    # ========================================================
    # ADMIN - CREATE KEY
    # ========================================================

    if uid in ADMIN_IDS and q.data == "acreate":

        context.user_data["mode"] = "admin_create"

        await q.message.reply_text(
            msg(
                "➕ <b>CREATE ACCESS KEY</b>\n\n"

                "Enter the duration:\n\n"

                "• <code>1</code> — 1 Day\n"
                "• <code>7</code> — 7 Days\n"
                "• <code>30</code> — 30 Days"
            ),
            parse_mode=ParseMode.HTML,
        )

        return


    # ========================================================
    # ADMIN - KEY LIST
    # ========================================================

    if uid in ADMIN_IDS and q.data == "akeys":

        rows = keys_page()

        if rows:

            lines = [
                "🔑 <b>ACCESS KEY LIST</b>\n"
            ]

            for r in rows:

                lines.append(
                    f"🔐 <code>{r['key']}</code>\n"
                    f"📦 Plan: {r['plan_days']} Days\n"
                    f"📊 Status: "
                    f"<b>{r['status'].upper()}</b>\n"
                )

            body = "\n".join(lines)

        else:

            body = (
                "🔑 <b>ACCESS KEYS</b>\n\n"
                "No keys have been created yet."
            )

        await q.message.reply_text(
            msg(body),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )

        return


    # ========================================================
    # ADMIN - STOP KEY
    # ========================================================

    if uid in ADMIN_IDS and q.data == "astop":

        context.user_data["mode"] = "admin_stop"

        await q.message.reply_text(
            msg(
                "⛔ <b>DISABLE ACCESS KEY</b>\n\n"

                "Send the key you want to disable.\n\n"

                "⚠️ A disabled key can no longer "
                "be activated."
            ),
            parse_mode=ParseMode.HTML,
        )

        return


    # ========================================================
    # ADMIN - BROADCAST
    # ========================================================

    if uid in ADMIN_IDS and q.data == "abroadcast":

        context.user_data["mode"] = "broadcast"

        await q.message.reply_text(
            msg(
                "📢 <b>BROADCAST CENTER</b>\n\n"

                "Send the message you want to broadcast "
                "to all registered users.\n\n"

                "⚠️ Please review your message before sending."
            ),
            parse_mode=ParseMode.HTML,
        )

        return


# ============================================================
# TEXT MESSAGE HANDLER
# ============================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    uid = update.effective_user.id

    upsert_user(update.effective_user)

    mode = context.user_data.pop("mode", None)

    text = (
        update.message.text or ""
    ).strip()


    # ========================================================
    # USER KEY ACTIVATION
    # ========================================================

    if mode == "key":

        ok, result = activate_key(
            text,
            uid
        )

        body = (
            "✅ <b>ACCESS ACTIVATED</b>\n\n"
            + result
            if ok
            else
            "❌ <b>KEY ACTIVATION FAILED</b>\n\n"
            + result
        )

        await update.message.reply_text(
            msg(body),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
            if ok
            else access_keyboard(),
        )

        return


    # ========================================================
    # PREDICTION PERIOD
    # ========================================================

    if mode == "period":

        if not active_key(uid):

            await update.message.reply_text(
                msg(
                    "🔒 <b>ACCESS EXPIRED</b>\n\n"

                    "Your key is no longer active.\n\n"

                    "Please activate a valid key or "
                    "purchase a new plan."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=access_keyboard(),
            )

            return

        result = get_prediction(text)

        await update.message.reply_text(
            msg(
                "🎯 <b>PREDICTION RESULT</b>\n\n"
                f"{result}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )

        return


    # ========================================================
    # ADMIN CREATE KEY
    # ========================================================

    if mode == "admin_create" and uid in ADMIN_IDS:

        if text not in ("1", "7", "30"):

            await update.message.reply_text(
                msg(
                    "❌ <b>INVALID DURATION</b>\n\n"

                    "Please enter one of:\n"
                    "• <code>1</code> day\n"
                    "• <code>7</code> days\n"
                    "• <code>30</code> days"
                ),
                parse_mode=ParseMode.HTML,
            )

            return

        key = create_key(
            int(text)
        )

        await update.message.reply_text(
            msg(
                "✅ <b>ACCESS KEY CREATED</b>\n\n"

                f"🔑 <b>Key:</b>\n"
                f"<code>{key}</code>\n\n"

                f"📦 <b>Plan:</b> {text} Days\n"
                "🟢 <b>Status:</b> UNUSED\n\n"

                "Send this key to the customer."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )

        return


    # ========================================================
    # ADMIN DISABLE KEY
    # ========================================================

    if mode == "admin_stop" and uid in ADMIN_IDS:

        ok = disable_key(text)

        if ok:

            body = (
                "⛔ <b>KEY DISABLED</b>\n\n"

                f"🔑 <code>{text}</code>\n\n"

                "This key can no longer be activated "
                "or used."
            )

        else:

            body = (
                "❌ <b>KEY NOT FOUND</b>\n\n"

                "No matching key was found.\n\n"

                "Please check the key and try again."
            )

        await update.message.reply_text(
            msg(body),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )

        return


    # ========================================================
    # ADMIN BROADCAST
    # ========================================================

    if mode == "broadcast" and uid in ADMIN_IDS:

        sent = 0
        failed = 0

        broadcast = msg(
            f"📢 <b>ANNOUNCEMENT</b>\n\n"
            f"{text}"
        )

        for target in all_users():

            try:

                await context.bot.send_message(
                    target,
                    broadcast,
                    parse_mode=ParseMode.HTML
                )

                sent += 1

                await asyncio.sleep(0.05)

            except Exception:

                failed += 1

        await update.message.reply_text(
            msg(
                "📢 <b>BROADCAST COMPLETE</b>\n\n"

                f"✅ <b>Delivered:</b> {sent}\n"
                f"❌ <b>Failed:</b> {failed}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )

        return


    # ========================================================
    # DEFAULT RESPONSE
    # ========================================================

    await update.message.reply_text(
        msg(
            "ℹ️ <b>USE THE MENU</b>\n\n"
            "Choose an option below to continue."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
        if active_key(uid)
        else access_keyboard(),
    )