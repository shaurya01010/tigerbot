import os
import html
import secrets
import string
import hashlib
import hmac
import logging
import threading
import base64
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

import razorpay
import firebase_admin

from firebase_admin import credentials, firestore

from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.constants import ParseMode

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

firebase_b64 = os.getenv("FIREBASE_CREDENTIALS_B64")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

ADMIN_USERNAME = "tiger_key"

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))

# Change this to your public HTTPS webhook URL.
RAZORPAY_WEBHOOK_URL = os.getenv(
    "RAZORPAY_WEBHOOK_URL",
    ""
)

ONLINE_TIMEOUT_MINUTES = 5


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("TIGER_MOD")


# ============================================================
# FIREBASE
# ============================================================
firebase_b64 = os.getenv("FIREBASE_CREDENTIALS_B64", "").strip()

if not firebase_admin._apps:
    if firebase_b64:
        import base64
        import json

        firebase_json_data = base64.b64decode(firebase_b64).decode("utf-8")
        firebase_info = json.loads(firebase_json_data)
        cred = credentials.Certificate(firebase_info)
    else:
        firebase_json = os.getenv(
            "FIREBASE_JSON",
            "tiger-da863-firebase-adminsdk-fbsvc-e0938355b9.json"
        )

        if not os.path.exists(firebase_json):
            raise FileNotFoundError(
                "Firebase credentials not configured. "
                "Set FIREBASE_CREDENTIALS_B64 in Render."
            )

        cred = credentials.Certificate(firebase_json)

    firebase_admin.initialize_app(cred)

db = firestore.client()


# ============================================================
# RAZORPAY
# ============================================================

razorpay_client = None

if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:

    razorpay_client = razorpay.Client(
        auth=(
            RAZORPAY_KEY_ID,
            RAZORPAY_KEY_SECRET,
        )
    )


# ============================================================
# PLANS
# ============================================================

PLANS = {
    "1": {
        "name": "1 Day",
        "days": 1,
        "price": 49,
        "amount": 4900,
    },

    "7": {
        "name": "7 Days",
        "days": 7,
        "price": 299,
        "amount": 29900,
    },

    "30": {
        "name": "30 Days",
        "days": 30,
        "price": 599,
        "amount": 59900,
    },
}


# ============================================================
# FLASK PAYMENT SERVER
# ============================================================

flask_app = Flask(__name__)


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def timestamp():
    return int(utc_now().timestamp())


def admin_user(user):
    if not user:
        return False

    username = (user.username or "").lower()

    return username == ADMIN_USERNAME


def safe_text(value):
    return html.escape(str(value or ""))


def generate_key(days):
    prefix = "TIGER"

    alphabet = string.ascii_uppercase + string.digits

    random_part = "".join(
        secrets.choice(alphabet)
        for _ in range(16)
    )

    return f"{prefix}-{days}D-{random_part}"


def get_user_ref(user_id):
    return db.collection("users").document(str(user_id))


def get_key_ref(key):
    return db.collection("premium_keys").document(key)


# ============================================================
# USER TRACKING
# ============================================================

def save_user(user):

    if not user:
        return

    ref = get_user_ref(user.id)

    existing = ref.get()

    data = {
        "telegram_id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "last_seen": firestore.SERVER_TIMESTAMP,
    }

    if not existing.exists:

        data["created_at"] = firestore.SERVER_TIMESTAMP

    ref.set(
        data,
        merge=True,
    )


async def track(update):

    user = None

    if update.effective_user:
        user = update.effective_user

    if user:
        save_user(user)


# ============================================================
# KEY MANAGEMENT
# ============================================================

def find_active_key_for_user(user_id):

    docs = (
        db.collection("premium_keys")
        .where(
            "telegram_id",
            "==",
            user_id,
        )
        .where(
            "status",
            "==",
            "active",
        )
        .stream()
    )

    now = utc_now()

    for doc in docs:

        data = doc.to_dict()

        expiry = data.get("expires_at")

        if not expiry:
            continue

        if hasattr(expiry, "replace"):

            if expiry.tzinfo is None:
                expiry = expiry.replace(
                    tzinfo=timezone.utc
                )

        if expiry > now:
            return doc.id, data

    return None, None


def activate_key(key, user_id):

    ref = get_key_ref(key)

    doc = ref.get()

    if not doc.exists:
        return False, "❌ Invalid key."

    data = doc.to_dict()

    status = data.get("status", "unused")

    if status == "revoked":
        return False, "❌ This key has been revoked."

    expiry = data.get("expires_at")

    now = utc_now()

    if status == "active" and expiry:

        if hasattr(expiry, "replace") and expiry.tzinfo is None:
            expiry = expiry.replace(
                tzinfo=timezone.utc
            )

        if expiry > now:

            if data.get("telegram_id") == user_id:
                return True, "✅ Your key is already active."

            return False, "❌ This key is already being used."

    days = int(data.get("days", 1))

    new_expiry = now + timedelta(days=days)

    ref.set(
        {
            "telegram_id": user_id,
            "status": "active",
            "activated_at": firestore.SERVER_TIMESTAMP,
            "expires_at": new_expiry,
        },
        merge=True,
    )

    return True, (
        "✅ Key activated successfully!\n\n"
        f"⏳ Valid for: {days} day(s)\n"
        f"📅 Expires: {new_expiry.strftime('%d-%m-%Y %H:%M UTC')}"
    )


def create_free_key(days, created_by):

    key = generate_key(days)

    now = utc_now()

    ref = get_key_ref(key)

    ref.set(
        {
            "key": key,
            "days": days,
            "plan": PLANS[str(days)]["name"],
            "price": 0,
            "status": "unused",
            "source": "admin",
            "created_by": created_by,
            "created_at": now,
            "expires_at": None,
            "telegram_id": None,
        }
    )

    return key


# ============================================================
# PAYMENT KEY CREATION
# ============================================================

def create_paid_key(
    telegram_id,
    plan_days,
    payment_id,
    order_id,
):

    key = generate_key(plan_days)

    ref = get_key_ref(key)

    now = utc_now()

    expiry = now + timedelta(days=plan_days)

    ref.set(
        {
            "key": key,
            "days": plan_days,
            "plan": PLANS[str(plan_days)]["name"],
            "price": PLANS[str(plan_days)]["price"],
            "status": "active",
            "source": "razorpay",
            "telegram_id": telegram_id,
            "payment_id": payment_id,
            "order_id": order_id,
            "created_at": now,
            "activated_at": now,
            "expires_at": expiry,
        }
    )

    return key, expiry


# ============================================================
# MAIN MENU
# ============================================================

def main_menu():

    keyboard = [

        [
            InlineKeyboardButton(
                "🎰 WinGo 1 MIN",
                callback_data="game:wingo1",
            )
        ],

        [
            InlineKeyboardButton(
                "🔑 My Key",
                callback_data="mykey",
            ),
            InlineKeyboardButton(
                "💳 Buy Key",
                callback_data="buy",
            ),
        ],

        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="refresh",
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def buy_menu():

    keyboard = [

        [
            InlineKeyboardButton(
                "1 Day • ₹49",
                callback_data="plan:1",
            )
        ],

        [
            InlineKeyboardButton(
                "7 Days • ₹299",
                callback_data="plan:7",
            )
        ],

        [
            InlineKeyboardButton(
                "30 Days • ₹599",
                callback_data="plan:30",
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="home",
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await track(update)

    user = update.effective_user

    key_id, key_data = find_active_key_for_user(
        user.id
    )

    if key_id:

        expiry = key_data.get("expires_at")

        expiry_text = "Unknown"

        if expiry:

            expiry_text = expiry.strftime(
                "%d-%m-%Y %H:%M UTC"
            )

        text = (
            "🐯 <b>TIGER MOD</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "✅ <b>Premium Active</b>\n\n"
            f"🔑 Key: <code>{safe_text(key_id)}</code>\n"
            f"📅 Expiry: <code>{expiry_text}</code>\n\n"
            "Choose an option below:"
        )

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )

        return

    keyboard = InlineKeyboardMarkup(

        [
            [
                InlineKeyboardButton(
                    "🔑 Enter Key",
                    callback_data="enter_key",
                )
            ],

            [
                InlineKeyboardButton(
                    "💳 Buy Key",
                    callback_data="buy",
                )
            ],
        ]
    )

    text = (
        "🐯 <b>TIGER MOD</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🔐 <b>Premium Access Required</b>\n\n"
        "You don't currently have an active key.\n\n"
        "Choose <b>Enter Key</b> if you already have "
        "a key or <b>Buy Key</b> to purchase access.\n\n"
        "Support @Tiger_Key"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await track(update)

    query = update.callback_query

    await query.answer()

    user = query.from_user

    data = query.data

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if data == "home":

        key_id, key_data = find_active_key_for_user(
            user.id
        )

        if not key_id:

            await query.edit_message_text(
                "🔐 <b>No active key.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔑 Enter Key",
                                callback_data="enter_key",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "💳 Buy Key",
                                callback_data="buy",
                            )
                        ],
                    ]
                ),
            )

            return

        await query.edit_message_text(
            "🐯 <b>TIGER MOD</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "Choose your game:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )

        return

    # --------------------------------------------------------
    # ENTER KEY
    # --------------------------------------------------------

    if data == "enter_key":

        context.user_data["waiting_for_key"] = True

        await query.edit_message_text(
            "🔑 <b>Enter your premium key</b>\n\n"
            "Send the complete key in your next message.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="home",
                        )
                    ]
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if data == "buy":

        await query.edit_message_text(
            "💳 <b>Choose Premium Plan</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "🔑 Select your plan:",
            parse_mode=ParseMode.HTML,
            reply_markup=buy_menu(),
        )

        return

    # --------------------------------------------------------
    # MY KEY
    # --------------------------------------------------------

    if data == "mykey":

        key_id, key_data = find_active_key_for_user(
            user.id
        )

        if not key_id:

            await query.edit_message_text(
                "❌ No active key found.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "💳 Buy Key",
                                callback_data="buy",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ Back",
                                callback_data="home",
                            )
                        ],
                    ]
                ),
            )

            return

        expiry = key_data.get("expires_at")

        expiry_text = (
            expiry.strftime("%d-%m-%Y %H:%M UTC")
            if expiry
            else "Unknown"
        )

        text = (
            "🔑 <b>YOUR PREMIUM KEY</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"Key:\n<code>{safe_text(key_id)}</code>\n\n"
            f"Plan: <b>{safe_text(key_data.get('plan'))}</b>\n"
            f"Expiry: <b>{expiry_text}</b>"
        )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="home",
                        )
                    ]
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # REFRESH
    # --------------------------------------------------------

    if data == "refresh":

        key_id, key_data = find_active_key_for_user(
            user.id
        )

        if key_id:

            await query.edit_message_text(
                "🐯 <b>TIGER MOD</b>\n"
                "━━━━━━━━━━━━━━\n\n"
                "✅ Premium access is active.\n\n"
                "Choose your game:",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )

        else:

            await query.edit_message_text(
                "❌ Your premium access is not active.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔑 Enter Key",
                                callback_data="enter_key",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "💳 Buy Key",
                                callback_data="buy",
                            )
                        ],
                    ]
                ),
            )

        return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    if data == "game:wingo1":

        key_id, key_data = find_active_key_for_user(
            user.id
        )

        if not key_id:

            await query.edit_message_text(
                "🔐 Premium key required.",
                reply_markup=buy_menu(),
            )

            return

        context.user_data["game"] = "WinGo 1 MIN"
        context.user_data["waiting_for_period"] = True

        await query.edit_message_text(
            "🐯 <b>TIGER MOD</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "🎰 <b>Prediction For WinGo 1 MIN</b>\n\n"
            "📅 Enter the <b>last 3 digits</b> "
            "of the period number.\n\n"
            "Example:\n"
            "<code>604</code>",
            parse_mode=ParseMode.HTML,
        )

        return

    # --------------------------------------------------------
    # NEXT PERIOD
    # --------------------------------------------------------

    if data == "next_period":

        context.user_data["waiting_for_period"] = True

        await query.message.reply_text(
            "📅 <b>Next Period</b>\n\n"
            "Enter the last 3 digits:\n"
            "Example: <code>605</code>",
            parse_mode=ParseMode.HTML,
        )

        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if data.startswith("admin:"):

        if not admin_user(user):

            await query.answer(
                "❌ Unauthorized",
                show_alert=True,
            )

            return

        await admin_callback(
            update,
            context,
            data,
        )

        return

    # --------------------------------------------------------
    # RAZORPAY QR VERIFY
    # --------------------------------------------------------

    if data.startswith("payment_verify:"):

        qr_id = data.split(":", 1)[1]
        await verify_razorpay_qr(query, user, qr_id)
        return

    # --------------------------------------------------------
    # RAZORPAY QR CANCEL
    # --------------------------------------------------------

    if data.startswith("payment_cancel:"):

        qr_id = data.split(":", 1)[1]
        await cancel_razorpay_qr(query, user, qr_id)
        return

    # --------------------------------------------------------
    # PLAN
    # --------------------------------------------------------

    if data.startswith("plan:"):

        plan_days = data.split(":")[1]

        if plan_days not in PLANS:

            await query.edit_message_text(
                "❌ Invalid plan."
            )

            return

        await create_payment_order(
            query,
            user,
            plan_days,
        )

        return


# ============================================================
# PAYMENT ORDER
# ============================================================

def razorpay_qr_request(method, path, payload=None):

    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay API credentials are missing.")

    credentials_text = f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}"
    auth = base64.b64encode(
        credentials_text.encode("utf-8")
    ).decode("ascii")

    url = f"https://api.razorpay.com/v1{path}"

    body = None

    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    }

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        logger.error(
            "Razorpay API %s %s failed: HTTP %s %s",
            method,
            path,
            e.code,
            raw,
        )
        raise RuntimeError(
            f"Razorpay HTTP {e.code}: {raw}"
        ) from e
    except Exception as e:
        logger.exception("Razorpay API request failed")
        raise RuntimeError(
            f"Razorpay request failed: {e}"
        ) from e


def create_razorpay_qr(plan, user):

    # Razorpay Dynamic UPI QR: one payment, exact amount.
    payload = {
        "type": "upi_qr",
        "name": f"Tiger Mod {plan['name']}",
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": plan["amount"],
        "description": f"Tiger Mod - {plan['name']}",
        "notes": {
            "telegram_id": str(user.id),
            "plan_days": str(plan["days"]),
        },
    }

    return razorpay_qr_request(
        "POST",
        "/payments/qr_codes",
        payload,
    )


def fetch_razorpay_qr(qr_id):
    return razorpay_qr_request(
        "GET",
        f"/payments/qr_codes/{qr_id}",
    )


def fetch_razorpay_qr_payments(qr_id):
    return razorpay_qr_request(
        "GET",
        f"/payments/qr_codes/{qr_id}/payments?count=100",
    )


def close_razorpay_qr(qr_id):
    return razorpay_qr_request(
        "POST",
        f"/payments/qr_codes/{qr_id}/close",
    )


def finalize_qr_payment(qr_id, payment_id):

    payment_ref = (
        db.collection("payments")
        .document(qr_id)
    )

    payment_doc = payment_ref.get()

    if not payment_doc.exists:
        return None, None, "Payment session not found."

    payment_data = payment_doc.to_dict()

    if payment_data.get("status") == "paid":
        return (
            payment_data.get("key"),
            payment_data.get("plan_days"),
            "already_paid",
        )

    telegram_id = int(payment_data["telegram_id"])
    plan_days = int(payment_data["plan_days"])

    key, expiry = create_paid_key(
        telegram_id,
        plan_days,
        payment_id,
        qr_id,
    )

    payment_ref.update(
        {
            "status": "paid",
            "payment_id": payment_id,
            "verified_at": firestore.SERVER_TIMESTAMP,
            "key": key,
        }
    )

    return key, expiry, "paid"


async def create_payment_order(query, user, plan_days):

    if not razorpay_client:
        await query.edit_message_text(
            "❌ Razorpay is not configured.\n\n"
            "Check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET."
        )
        return

    plan = PLANS[plan_days]

    try:
        qr = create_razorpay_qr(plan, user)
    except Exception as e:
        logger.exception("Razorpay QR creation failed")
        await query.edit_message_text(
            "❌ Could not create the Razorpay UPI QR.\n\n"
            f"<code>{safe_text(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    qr_id = qr.get("id")
    image_url = qr.get("image_url")

    if not qr_id or not image_url:
        logger.error("Razorpay QR response missing id/image_url: %r", qr)
        await query.edit_message_text(
            "❌ Razorpay returned an invalid QR response.\n"
            "Please try again."
        )
        return

    db.collection("payments").document(qr_id).set(
        {
            "qr_code_id": qr_id,
            "telegram_id": user.id,
            "plan_days": int(plan_days),
            "amount": plan["amount"],
            "price": plan["price"],
            "status": "created",
            "created_at": firestore.SERVER_TIMESTAMP,
        }
    )

    caption = (
        "💳 <b>RAZORPAY UPI PAYMENT</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🐯 <b>TIGER MOD</b>\n\n"
        f"📦 Plan: <b>{plan['name']}</b>\n"
        f"💰 Amount: <b>₹{plan['price']}</b>\n\n"
        "📱 Scan this QR using PhonePe, Google Pay, "
        "Paytm or another supported UPI app.\n\n"
        "⚠️ Pay the exact amount shown above.\n"
        "After payment, press <b>VERIFY PAYMENT</b>."
    )

    try:
        await query.message.delete()
    except Exception:
        pass

    if not CURRENT_APPLICATION:
        await query.answer("Payment service is not ready.", show_alert=True)
        return

    await CURRENT_APPLICATION.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=image_url,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ VERIFY PAYMENT",
                        callback_data=f"payment_verify:{qr_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ CANCEL",
                        callback_data=f"payment_cancel:{qr_id}",
                    )
                ],
            ]
        ),
    )


async def verify_razorpay_qr(query, user, qr_id):

    payment_ref = db.collection("payments").document(qr_id)
    payment_doc = payment_ref.get()

    if not payment_doc.exists:
        await query.answer("Payment session not found.", show_alert=True)
        return

    payment_data = payment_doc.to_dict()

    if int(payment_data.get("telegram_id", 0)) != user.id:
        await query.answer("This payment belongs to another user.", show_alert=True)
        return

    if payment_data.get("status") == "paid":
        await query.answer("Payment already verified.", show_alert=True)
        return

    try:
        qr = fetch_razorpay_qr(qr_id)
        payments = fetch_razorpay_qr_payments(qr_id)
    except Exception as e:
        logger.exception("Razorpay QR verification failed")
        await query.answer("Could not contact Razorpay. Try again.", show_alert=True)
        return

    expected_amount = int(payment_data["amount"])
    captured_payment = None

    for item in payments.get("items", []):
        if (
            item.get("status") == "captured"
            and int(item.get("amount", 0)) == expected_amount
        ):
            captured_payment = item
            break

    if not captured_payment:
        status = qr.get("status", "unknown")
        if status == "closed":
            await query.answer(
                "Payment not received. This QR is closed.",
                show_alert=True,
            )
        else:
            await query.answer(
                "❌ Payment not received yet.",
                show_alert=True,
            )
        return

    payment_id = captured_payment.get("id")

    key, expiry, result = finalize_qr_payment(
        qr_id,
        payment_id,
    )

    if result == "already_paid":
        await query.answer("Payment already verified.", show_alert=True)
        return

    if result != "paid":
        await query.answer("Could not verify payment.", show_alert=True)
        return

    try:
        await query.message.delete()
    except Exception:
        pass

    if not CURRENT_APPLICATION:
        await query.answer("Bot is restarting. Try again.", show_alert=True)
        return

    await CURRENT_APPLICATION.bot.send_message(
        chat_id=query.message.chat_id,
        text="🎉 <b>PAYMENT VERIFIED</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🐯 <b>TIGER MOD</b>\n\n"
        f"📦 Plan: <b>{payment_data['plan_days']} Day(s)</b>\n\n"
        "🔑 Your Premium Key:\n"
        f"<code>{safe_text(key)}</code>\n\n"
        "📅 Expires:\n"
        f"<b>{expiry.strftime('%d-%m-%Y %H:%M UTC')}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cancel_razorpay_qr(query, user, qr_id):

    payment_ref = db.collection("payments").document(qr_id)
    payment_doc = payment_ref.get()

    if not payment_doc.exists:
        await query.answer("Payment session not found.", show_alert=True)
        return

    payment_data = payment_doc.to_dict()

    if int(payment_data.get("telegram_id", 0)) != user.id:
        await query.answer("This payment belongs to another user.", show_alert=True)
        return

    if payment_data.get("status") == "paid":
        await query.answer("This payment is already completed.", show_alert=True)
        return

    try:
        qr = fetch_razorpay_qr(qr_id)
        if qr.get("status") == "active":
            close_razorpay_qr(qr_id)
    except Exception:
        logger.exception("Razorpay QR cancellation failed")

    payment_ref.update(
        {
            "status": "cancelled",
            "cancelled_at": firestore.SERVER_TIMESTAMP,
        }
    )

    await query.answer("Payment cancelled.", show_alert=True)

    await query.message.edit_caption(
        caption=(
            "❌ <b>PAYMENT CANCELLED</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "This Razorpay QR has been cancelled.\n\n"
            "Choose a plan again from Buy Key."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💳 BUY KEY",
                        callback_data="buy",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ BACK",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await track(update)

    user = update.effective_user

    text = (update.message.text or "").strip()

    # --------------------------------------------------------
    # KEY ENTRY
    # --------------------------------------------------------

    if context.user_data.get("waiting_for_key"):

        context.user_data["waiting_for_key"] = False

        success, message = activate_key(
            text.upper(),
            user.id,
        )

        if success:

            await update.message.reply_text(
                message,
                parse_mode=ParseMode.HTML,
            )

            await update.message.reply_text(
                "🐯 <b>TIGER MOD</b>\n"
                "━━━━━━━━━━━━━━\n\n"
                "Choose your game:",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )

        else:

            await update.message.reply_text(
                message,
                parse_mode=ParseMode.HTML,
            )

        return

    # --------------------------------------------------------
    # PERIOD ENTRY
    # --------------------------------------------------------

    if context.user_data.get("waiting_for_period"):

        if not text.isdigit() or len(text) != 3:

            await update.message.reply_text(
                "⚠️ Please enter exactly "
                "<b>3 digits</b>.\n\n"
                "Example: <code>604</code>",
                parse_mode=ParseMode.HTML,
            )

            return

        context.user_data["waiting_for_period"] = False

        period = text

        result = generate_prediction(period)

        history_ref = db.collection(
            "prediction_history"
        ).document()

        history_ref.set(
            {
                "telegram_id": user.id,
                "username": user.username or "",
                "game": context.user_data.get(
                    "game",
                    "WinGo 1 MIN",
                ),
                "period": period,
                "result": result,
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )

        game = context.user_data.get(
            "game",
            "WinGo 1 MIN",
        )

        message = format_prediction(
            game,
            period,
            result,
        )

        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔮 Next Prediction",
                            callback_data="next_period",
                        )
                    ],

                    [
                        InlineKeyboardButton(
                            "🏠 Main Menu",
                            callback_data="home",
                        )
                    ],
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # UNKNOWN TEXT
    # --------------------------------------------------------

    await update.message.reply_text(
        "🐯 Use /start to open TIGER MOD."
    )


# ============================================================
# PREDICTION LOGIC
# ============================================================
#
# This follows the supplied HTML:
#
# demo = "100000000000" + last 3 digits
# sum all digits
# choose format 1 or 2
# even/odd result determines BIG/SMALL
#
# ============================================================

def sum_digits(number):

    return sum(
        int(d)
        for d in number
        if d.isdigit()
    )


def get_result(sum_value, format_choice):

    result_digit = int(
        str(sum_value)[-1]
    )

    if format_choice == 1:

        if result_digit in [0, 2, 4, 6, 8]:
            return "SMALL 🟢"

        return "BIG 🔴"

    elif format_choice == 2:

        if result_digit in [1, 3, 5, 7, 9]:
            return "BIG 🔴"

        return "SMALL 🟢"

    return "INVALID"


def generate_prediction(period):

    demo = "100000000000" + period

    total = sum_digits(demo)

    # Same random format selection as uploaded code.
    import random

    format_choice = (
        1 if random.random() > 0.5 else 2
    )

    prediction = get_result(
        total,
        format_choice,
    )

    # Same generated range as uploaded code.
    win_rate = (
        random.randint(70, 95)
    )

    if "SMALL" in prediction:

        color = "🟢 GREEN"

    else:

        color = "🔴 RED"

    return {
        "prediction": prediction,
        "color": color,
        "win_rate": win_rate,
        "sum": total,
        "format": format_choice,
    }


def format_prediction(
    game,
    period,
    result,
):

    return (
        "🐯 <b>TIGER MOD</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🎰 <b>Prediction For {safe_text(game)}</b>\n\n"
        f"📅 <b>Period:</b>\n"
        f"<code>{safe_text(period)}</code>\n\n"
        f"💰 <b>Purchase:</b> "
        f"<b>{safe_text(result['prediction'])}</b>\n\n"
        "🔮 <b>Prediction Details</b>\n"
        f"👉 Colour: <b>{safe_text(result['color'])}</b>\n"
        f"👉 Calculation: <b>{result['sum']}</b>\n"
        f"👉 Win Rate: <b>{result['win_rate']}%</b>\n\n"
        "📊 <b>Risk Level:</b> Medium Risk\n\n"
        "💡 <b>Strategy Tip</b>\n"
        "Use proper fund management and avoid over betting.\n\n"
        "━━━━━━━━━━━━━━\n"
        "🔮 <b>Next Prediction</b>\n"
        "Enter the next 3-digit period below."
    )


# ============================================================
# ADMIN DASHBOARD
# ============================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await track(update)

    user = update.effective_user

    if not admin_user(user):

        await update.message.reply_text(
            "❌ Unauthorized."
        )

        return

    stats = get_statistics()

    text = (
        "🐯 <b>TIGER MOD ADMIN</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"👥 Users: <b>{stats['users']}</b>\n"
        f"🔑 Active Keys: <b>{stats['active_keys']}</b>\n"
        f"💰 Keys Sold: <b>{stats['sold_keys']}</b>\n"
        f"🎟 Generated Keys: <b>{stats['generated_keys']}</b>\n"
        f"🟢 Online: <b>{stats['online']}</b>\n"
        f"💵 Revenue: <b>₹{stats['revenue']}</b>\n\n"
        "Choose an option:"
    )

    keyboard = InlineKeyboardMarkup(

        [
            [
                InlineKeyboardButton(
                    "🔑 Generate Key",
                    callback_data="admin:gen",
                ),

                InlineKeyboardButton(
                    "📊 Statistics",
                    callback_data="admin:stats",
                ),
            ],

            [
                InlineKeyboardButton(
                    "👥 Users",
                    callback_data="admin:users",
                ),

                InlineKeyboardButton(
                    "💳 Sales",
                    callback_data="admin:sales",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🔐 Manage Keys",
                    callback_data="admin:keys",
                ),

                InlineKeyboardButton(
                    "📢 Broadcast",
                    callback_data="admin:broadcast",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data="admin:refresh",
                ),
            ],
        ]
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ============================================================
# ADMIN CALLBACKS
# ============================================================

async def admin_callback(
    update,
    context,
    data,
):

    query = update.callback_query

    user = query.from_user

    if not admin_user(user):

        await query.answer(
            "Unauthorized",
            show_alert=True,
        )

        return

    action = data.split(":", 1)[1]

    # --------------------------------------------------------
    # ADMIN HOME
    # --------------------------------------------------------

    if action in ("refresh", "home"):

        stats = get_statistics()

        text = (
            "🐯 <b>TIGER MOD ADMIN</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"👥 Users: <b>{stats['users']}</b>\n"
            f"🔑 Active Keys: <b>{stats['active_keys']}</b>\n"
            f"💰 Keys Sold: <b>{stats['sold_keys']}</b>\n"
            f"🎟 Generated Keys: <b>{stats['generated_keys']}</b>\n"
            f"🟢 Online: <b>{stats['online']}</b>\n"
            f"💵 Revenue: <b>₹{stats['revenue']}</b>"
        )

        keyboard = admin_keyboard()

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

        return

    # --------------------------------------------------------
    # GENERATE KEY
    # --------------------------------------------------------

    if action == "gen":

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "1 Day",
                        callback_data="admin:gen1",
                    )
                ],

                [
                    InlineKeyboardButton(
                        "7 Days",
                        callback_data="admin:gen7",
                    )
                ],

                [
                    InlineKeyboardButton(
                        "30 Days",
                        callback_data="admin:gen30",
                    )
                ],

                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin:home",
                    )
                ],
            ]
        )

        await query.edit_message_text(
            "🔑 <b>Generate Free Key</b>\n\n"
            "Choose duration:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

        return

    if action in ("gen1", "gen7", "gen30"):

        days = int(action.replace("gen", ""))

        key = create_free_key(
            days,
            user.username or str(user.id),
        )

        await query.edit_message_text(

            "✅ <b>KEY GENERATED</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"📦 Plan: <b>{days} Day(s)</b>\n\n"
            f"🔑 Key:\n"
            f"<code>{safe_text(key)}</code>\n\n"
            "This key was generated without payment.",

            parse_mode=ParseMode.HTML,

            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Admin Panel",
                            callback_data="admin:home",
                        )
                    ]
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # STATISTICS
    # --------------------------------------------------------

    if action == "stats":

        stats = get_statistics()

        text = (
            "📊 <b>STATISTICS</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"👥 Total Users: <b>{stats['users']}</b>\n"
            f"🟢 Online: <b>{stats['online']}</b>\n"
            f"🔑 Active Keys: <b>{stats['active_keys']}</b>\n"
            f"⏰ Expired Keys: <b>{stats['expired_keys']}</b>\n"
            f"🎟 Total Generated: <b>{stats['generated_keys']}</b>\n"
            f"💳 Paid Keys: <b>{stats['sold_keys']}</b>\n"
            f"🎁 Free Keys: <b>{stats['free_keys']}</b>\n"
            f"💵 Revenue: <b>₹{stats['revenue']}</b>"
        )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="admin:home",
                        )
                    ]
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    if action == "users":

        users = list(
            db.collection("users")
            .limit(20)
            .stream()
        )

        lines = [
            "👥 <b>RECENT USERS</b>",
            "━━━━━━━━━━━━━━",
            "",
        ]

        for doc in users:

            d = doc.to_dict()

            username = d.get(
                "username",
                "",
            )

            first_name = d.get(
                "first_name",
                "",
            )

            telegram_id = d.get(
                "telegram_id",
                doc.id,
            )

            if username:

                display = f"@{username}"

            else:

                display = first_name or str(
                    telegram_id
                )

            lines.append(
                f"• {safe_text(display)}"
            )

        if not users:

            lines.append(
                "No users found."
            )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="admin:home",
                        )
                    ]
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # SALES
    # --------------------------------------------------------

    if action == "sales":

        payments = list(
            db.collection("payments")
            .where(
                "status",
                "==",
                "paid",
            )
            .limit(20)
            .stream()
        )

        total = 0

        lines = [
            "💳 <b>RECENT SALES</b>",
            "━━━━━━━━━━━━━━",
            "",
        ]

        for doc in payments:

            d = doc.to_dict()

            amount = int(
                d.get("amount", 0)
            ) // 100

            total += amount

            plan_days = d.get(
                "plan_days",
                "?",
            )

            telegram_id = d.get(
                "telegram_id",
                "?",
            )

            lines.append(
                f"• ₹{amount} | "
                f"{plan_days}D | "
                f"{telegram_id}"
            )

        lines.extend(
            [
                "",
                f"💵 Listed Total: ₹{total}",
            ]
        )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="admin:home",
                        )
                    ]
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # KEYS
    # --------------------------------------------------------

    if action == "keys":

        keys = list(
            db.collection("premium_keys")
            .limit(30)
            .stream()
        )

        active = 0
        unused = 0
        expired = 0

        for doc in keys:

            d = doc.to_dict()

            status = d.get(
                "status",
                "unused",
            )

            if status == "active":
                active += 1

            elif status == "unused":
                unused += 1

            elif status == "expired":
                expired += 1

        text = (
            "🔐 <b>KEY MANAGEMENT</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"🟢 Active: <b>{active}</b>\n"
            f"⚪ Unused: <b>{unused}</b>\n"
            f"⏰ Expired: <b>{expired}</b>\n\n"
            "Use Generate Key to create a new key."
        )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔑 Generate Key",
                            callback_data="admin:gen",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="admin:home",
                        )
                    ],
                ]
            ),
        )

        return

    # --------------------------------------------------------
    # BROADCAST
    # --------------------------------------------------------

    if action == "broadcast":

        context.user_data[
            "admin_broadcast"
        ] = True

        await query.edit_message_text(
            "📢 <b>Broadcast</b>\n\n"
            "Send the message you want to broadcast "
            "in your next message.",
            parse_mode=ParseMode.HTML,
        )

        return


def admin_keyboard():

    return InlineKeyboardMarkup(

        [
            [
                InlineKeyboardButton(
                    "🔑 Generate Key",
                    callback_data="admin:gen",
                ),

                InlineKeyboardButton(
                    "📊 Statistics",
                    callback_data="admin:stats",
                ),
            ],

            [
                InlineKeyboardButton(
                    "👥 Users",
                    callback_data="admin:users",
                ),

                InlineKeyboardButton(
                    "💳 Sales",
                    callback_data="admin:sales",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🔐 Manage Keys",
                    callback_data="admin:keys",
                ),

                InlineKeyboardButton(
                    "📢 Broadcast",
                    callback_data="admin:broadcast",
                ),
            ],

            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data="admin:refresh",
                )
            ],
        ]
    )


# ============================================================
# ADMIN BROADCAST
# ============================================================

async def handle_admin_broadcast(
    update,
    context,
):

    user = update.effective_user

    if not admin_user(user):
        return False

    if not context.user_data.get(
        "admin_broadcast"
    ):
        return False

    context.user_data[
        "admin_broadcast"
    ] = False

    message = update.message.text

    users = list(
        db.collection("users").stream()
    )

    sent = 0
    failed = 0

    for doc in users:

        data = doc.to_dict()

        telegram_id = data.get(
            "telegram_id"
        )

        if not telegram_id:
            continue

        try:

            await context.bot.send_message(
                chat_id=telegram_id,
                text=message,
            )

            sent += 1

        except Exception:

            failed += 1

    await update.message.reply_text(
        "📢 <b>Broadcast Complete</b>\n\n"
        f"✅ Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )

    return True


# ============================================================
# STATISTICS
# ============================================================

def get_statistics():

    users = list(
        db.collection("users").stream()
    )

    keys = list(
        db.collection("premium_keys").stream()
    )

    payments = list(
        db.collection("payments")
        .where(
            "status",
            "==",
            "paid",
        )
        .stream()
    )

    now = utc_now()

    active_keys = 0
    expired_keys = 0
    generated_keys = len(keys)
    sold_keys = 0
    free_keys = 0
    revenue = 0

    for doc in keys:

        d = doc.to_dict()

        source = d.get(
            "source",
            "",
        )

        if source == "razorpay":
            sold_keys += 1

        if source == "admin":
            free_keys += 1

        status = d.get(
            "status",
            "",
        )

        expiry = d.get(
            "expires_at"
        )

        if status == "active" and expiry:

            if hasattr(expiry, "replace"):

                if expiry.tzinfo is None:

                    expiry = expiry.replace(
                        tzinfo=timezone.utc
                    )

            if expiry > now:

                active_keys += 1

            else:

                expired_keys += 1

                try:

                    doc.reference.update(
                        {
                            "status": "expired"
                        }
                    )

                except Exception:
                    pass

    for doc in payments:

        d = doc.to_dict()

        revenue += (
            int(
                d.get(
                    "amount",
                    0,
                )
            ) // 100
        )

    online = 0

    online_limit = now - timedelta(
        minutes=ONLINE_TIMEOUT_MINUTES
    )

    for doc in users:

        d = doc.to_dict()

        last_seen = d.get(
            "last_seen"
        )

        if last_seen:

            if hasattr(
                last_seen,
                "replace",
            ):

                if last_seen.tzinfo is None:

                    last_seen = last_seen.replace(
                        tzinfo=timezone.utc
                    )

            if last_seen >= online_limit:
                online += 1

    return {
        "users": len(users),
        "active_keys": active_keys,
        "expired_keys": expired_keys,
        "generated_keys": generated_keys,
        "sold_keys": sold_keys,
        "free_keys": free_keys,
        "online": online,
        "revenue": revenue,
    }


# ============================================================
# RAZORPAY PAYMENT PAGE
# ============================================================

@flask_app.route(
    "/pay/<order_id>",
    methods=["GET"],
)
def payment_page(order_id):

    doc = (
        db.collection("payments")
        .document(order_id)
        .get()
    )

    if not doc.exists:

        return "Invalid payment order", 404

    data = doc.to_dict()

    amount = data.get(
        "amount",
        0,
    )

    plan_days = data.get(
        "plan_days",
        1,
    )

    html_page = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1.0">
<title>Tiger Mod Payment</title>

<script src="https://checkout.razorpay.com/v1/checkout.js"></script>

<style>
body {{
    background:#050505;
    color:#d4af37;
    font-family:Arial,sans-serif;
    text-align:center;
    padding:40px 20px;
}}

.box {{
    max-width:450px;
    margin:auto;
    padding:30px;
    border:1px solid #d4af37;
    border-radius:18px;
    background:#111;
}}

button {{
    width:100%;
    padding:15px;
    margin-top:20px;
    border:0;
    border-radius:10px;
    background:#d4af37;
    color:#000;
    font-weight:bold;
    font-size:17px;
}}
</style>
</head>

<body>

<div class="box">

<h1>🐯 TIGER MOD</h1>

<hr>

<h2>Premium Access</h2>

<p>Plan: {plan_days} Day(s)</p>

<p>Amount: ₹{amount // 100}</p>

<button onclick="payNow()">
💳 Pay Now
</button>

</div>

<script>

function payNow() {{

    var options = {{

        key: "{safe_text(RAZORPAY_KEY_ID)}",

        amount: {amount},

        currency: "INR",

        name: "TIGER MOD",

        description:
            "Premium Access - {plan_days} Day(s)",

        order_id:
            "{safe_text(order_id)}",

        handler: function(response) {{

            fetch("/payment/success", {{

                method: "POST",

                headers: {{
                    "Content-Type":
                        "application/json"
                }},

                body: JSON.stringify({{

                    razorpay_payment_id:
                        response.razorpay_payment_id,

                    razorpay_order_id:
                        response.razorpay_order_id,

                    razorpay_signature:
                        response.razorpay_signature

                }})

            }})

            .then(function(res) {{
                return res.json();
            }})

            .then(function(data) {{

                document.body.innerHTML =
                    "<h1>🐯 TIGER MOD</h1>" +
                    "<h2>" +
                    data.message +
                    "</h2>";

            }});

        }},

        theme: {{
            color: "#d4af37"
        }}

    }};

    var rzp =
        new Razorpay(options);

    rzp.open();
}}

</script>

</body>
</html>
"""

    return html_page


# ============================================================
# PAYMENT SUCCESS
# ============================================================

@flask_app.route(
    "/payment/success",
    methods=["POST"],
)
def payment_success():

    data = request.get_json(
        silent=True
    ) or {}

    payment_id = data.get(
        "razorpay_payment_id"
    )

    order_id = data.get(
        "razorpay_order_id"
    )

    signature = data.get(
        "razorpay_signature"
    )

    if not all(
        [
            payment_id,
            order_id,
            signature,
        ]
    ):

        return jsonify(
            {
                "message":
                    "Invalid payment response."
            }
        ), 400

    try:

        razorpay_client.utility.verify_payment_signature(
            {
                "razorpay_order_id":
                    order_id,

                "razorpay_payment_id":
                    payment_id,

                "razorpay_signature":
                    signature,
            }
        )

    except Exception:

        return jsonify(
            {
                "message":
                    "Payment verification failed."
            }
        ), 400

    payment_ref = (
        db.collection("payments")
        .document(order_id)
    )

    payment_doc = payment_ref.get()

    if not payment_doc.exists:

        return jsonify(
            {
                "message":
                    "Payment order not found."
            }
        ), 404

    payment_data = payment_doc.to_dict()

    if payment_data.get("status") == "paid":

        return jsonify(
            {
                "message":
                    "Payment already processed."
            }
        )

    telegram_id = int(
        payment_data["telegram_id"]
    )

    plan_days = int(
        payment_data["plan_days"]
    )

    key, expiry = create_paid_key(
        telegram_id,
        plan_days,
        payment_id,
        order_id,
    )

    payment_ref.update(
        {
            "status": "paid",
            "payment_id": payment_id,
            "verified_at":
                firestore.SERVER_TIMESTAMP,
            "key": key,
        }
    )

    # Send key from a background-safe bot instance.
    try:

        bot_app = CURRENT_APPLICATION

        if bot_app:

            import asyncio

            async def send_key():

                await bot_app.bot.send_message(

                    chat_id=telegram_id,

                    text=(
                        "🎉 <b>PAYMENT VERIFIED</b>\n"
                        "━━━━━━━━━━━━━━\n\n"
                        "🐯 TIGER MOD\n\n"
                        f"📦 Plan: <b>{plan_days} Day(s)</b>\n\n"
                        "🔑 Your Premium Key:\n"
                        f"<code>{safe_text(key)}</code>\n\n"
                        f"📅 Expires:\n"
                        f"<b>{expiry.strftime('%d-%m-%Y %H:%M UTC')}</b>\n\n"
                        "Use /start to access your premium panel."
                    ),

                    parse_mode=ParseMode.HTML,
                )

            loop = asyncio.new_event_loop()

            loop.run_until_complete(
                send_key()
            )

            loop.close()

    except Exception:

        logger.exception(
            "Could not send payment key to Telegram"
        )

    return jsonify(
        {
            "message":
                "✅ Payment verified. "
                "Your premium key has been sent "
                "to Telegram."
        }
    )


# ============================================================
# RAZORPAY WEBHOOK
# ============================================================

@flask_app.route(
    "/razorpay/webhook",
    methods=["POST"],
)
def razorpay_webhook():

    body = request.get_data()

    signature = request.headers.get(
        "X-Razorpay-Signature",
        "",
    )

    if RAZORPAY_WEBHOOK_SECRET:
        expected = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            return jsonify({"status": "invalid signature"}), 400

    payload = request.get_json(silent=True) or {}
    event = payload.get("event", "")

    # The Telegram VERIFY button performs the authoritative API check.
    # This endpoint is kept available for optional Razorpay QR webhooks.
    if event == "qr_code.credited":
        logger.info("Razorpay QR credited webhook received")

    return jsonify({"status": "ok"})


# ============================================================
# HEALTH CHECK
# ============================================================

@flask_app.route(
    "/",
    methods=["GET"],
)
def health():

    return jsonify(
        {
            "status": "online",
            "service": "TIGER MOD",
        }
    )


# ============================================================
# RUN FLASK IN BACKGROUND
# ============================================================

CURRENT_APPLICATION = None


def start_payment_server():

    flask_app.run(
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        threaded=True,
        use_reloader=False,
    )


# ============================================================
# COMMANDS
# ============================================================

async def admin_command_wrapper(
    update,
    context,
):

    await admin_command(
        update,
        context,
    )


async def help_command(
    update,
    context,
):

    await track(update)

    await update.message.reply_text(
        "🐯 <b>TIGER MOD</b>\n\n"
        "/start - Open bot\n"
        "/admin - Admin panel",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# MESSAGE ROUTER
# ============================================================

async def universal_message_handler(
    update,
    context,
):

    # Admin broadcast has priority.
    if await handle_admin_broadcast(
        update,
        context,
    ):

        return

    await text_handler(
        update,
        context,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    global CURRENT_APPLICATION

    bot_token = os.getenv("BOT_TOKEN", "").strip()

    logger.info(
        "BOT_TOKEN configured: %s | length: %d",
        bool(bot_token),
        len(bot_token),
    )

    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application.builder()
        .token(bot_token)
        .build()
    )
    # --------------------------------------------------------
    # Start Flask
    # --------------------------------------------------------

    payment_thread = threading.Thread(
        target=start_payment_server,
        daemon=True,
    )

    payment_thread.start()

    # --------------------------------------------------------
    # Telegram Application
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(bot_token)
        .build()
    )

    CURRENT_APPLICATION = application

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command_wrapper,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    # Text
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            universal_message_handler,
        )
    )

    logger.info(
        "🐯 TIGER MOD BOT STARTING..."
    )

    logger.info(
        "Admin: @Tiger_Key"
    )

    logger.info(
        "Payment server: %s:%s",
        WEBHOOK_HOST,
        WEBHOOK_PORT,
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()