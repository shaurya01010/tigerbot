import os
import html
import base64
import json
import secrets
import string
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, jsonify
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

# Render: put the Base64 value of your Firebase service-account JSON here.
FIREBASE_CREDENTIALS_B64 = os.getenv("FIREBASE_CREDENTIALS_B64", "")

# Local fallback only.
FIREBASE_JSON = os.getenv(
    "FIREBASE_JSON",
    "tiger-da863-firebase-adminsdk-fbsvc-e0938355b9.json",
)

# Telegram username allowed to use /admin.
ADMIN_USERNAME = "tiger_key"

# Optional display label. Set this to your merchant/UPI identifier if you
# want it displayed in the Telegram payment message.
DISPLAY_UPI_ID = os.getenv("DISPLAY_UPI_ID", "Razorpay UPI")

# Render supplies PORT automatically.
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

QR_EXPIRY_MINUTES = 30
ONLINE_TIMEOUT_MINUTES = 5

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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("TIGER_MOD")

# ============================================================
# FIREBASE
# ============================================================

def init_firebase():
    if firebase_admin._apps:
        return

    if FIREBASE_CREDENTIALS_B64:
        try:
            raw = base64.b64decode(FIREBASE_CREDENTIALS_B64)
            info = json.loads(raw.decode("utf-8"))
            firebase_admin.initialize_app(credentials.Certificate(info))
            logger.info("Firebase initialized from FIREBASE_CREDENTIALS_B64.")
            return
        except Exception:
            logger.exception("Could not initialize Firebase from Base64.")

    if os.path.exists(FIREBASE_JSON):
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_JSON))
        logger.info("Firebase initialized from local JSON.")
        return

    raise RuntimeError(
        "Firebase credentials missing. Set FIREBASE_CREDENTIALS_B64 on Render "
        "or provide the Firebase JSON locally."
    )


init_firebase()
db = firestore.client()

# ============================================================
# FLASK / HEALTH SERVER
# ============================================================

flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return jsonify({
        "status": "online",
        "service": "TIGER MOD",
    })

def start_flask():
    flask_app.run(
        host=HOST,
        port=PORT,
        threaded=True,
        use_reloader=False,
    )

# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def timestamp():
    return int(now_utc().timestamp())


def safe_text(value):
    return html.escape(str(value or ""))


def is_admin(user):
    return bool(
        user
        and (user.username or "").lower() == ADMIN_USERNAME.lower()
    )


def user_ref(user_id):
    return db.collection("users").document(str(user_id))


def key_ref(key):
    return db.collection("premium_keys").document(key)


def generate_key(days):
    alphabet = string.ascii_uppercase + string.digits
    random_part = "".join(
        secrets.choice(alphabet)
        for _ in range(16)
    )
    return f"TIGER-{days}D-{random_part}"


def as_utc(value):
    if value is None:
        return None

    if hasattr(value, "to_datetime"):
        value = value.to_datetime()

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value


def save_user(user):
    if not user:
        return

    ref = user_ref(user.id)
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

    ref.set(data, merge=True)


async def track(update):
    if update.effective_user:
        save_user(update.effective_user)

# ============================================================
# KEY MANAGEMENT
# ============================================================

def find_active_key(user_id):
    docs = (
        db.collection("premium_keys")
        .where("telegram_id", "==", user_id)
        .where("status", "==", "active")
        .stream()
    )

    current = now_utc()

    for doc in docs:
        data = doc.to_dict()
        expiry = as_utc(data.get("expires_at"))

        if expiry and expiry > current:
            return doc.id, data

        if expiry and expiry <= current:
            try:
                doc.reference.update({"status": "expired"})
            except Exception:
                pass

    return None, None


def activate_key(key, user_id):
    ref = key_ref(key)
    doc = ref.get()

    if not doc.exists:
        return False, "❌ Invalid key."

    data = doc.to_dict()

    if data.get("status") == "revoked":
        return False, "❌ This key has been revoked."

    current = now_utc()
    expiry = as_utc(data.get("expires_at"))

    if (
        data.get("status") == "active"
        and expiry
        and expiry > current
    ):
        if data.get("telegram_id") == user_id:
            return True, "✅ Your key is already active."

        return False, "❌ This key is already being used."

    days = int(data.get("days", 1))
    new_expiry = current + timedelta(days=days)

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
        f"📅 Expires: "
        f"{new_expiry.strftime('%d-%m-%Y %H:%M UTC')}"
    )


def create_key(
    days,
    source,
    telegram_id=None,
    payment_id=None,
    order_id=None,
    price=0,
):
    key = generate_key(days)

    expiry = (
        now_utc() + timedelta(days=days)
        if source == "razorpay"
        else None
    )

    key_ref(key).set(
        {
            "key": key,
            "days": days,
            "plan": PLANS[str(days)]["name"],
            "price": price,
            "status": (
                "active"
                if source == "razorpay"
                else "unused"
            ),
            "source": source,
            "telegram_id": telegram_id,
            "payment_id": payment_id,
            "order_id": order_id,
            "created_at": firestore.SERVER_TIMESTAMP,
            "activated_at": (
                firestore.SERVER_TIMESTAMP
                if source == "razorpay"
                else None
            ),
            "expires_at": expiry,
        }
    )

    return key, expiry

# ============================================================
# MENUS
# ============================================================

def home_keyboard():
    return InlineKeyboardMarkup(
        [
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
    )


def buy_keyboard():
    return InlineKeyboardMarkup(
        [
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
    )


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
                )
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
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await track(update)

    user = update.effective_user

    key_id, key_data = find_active_key(user.id)

    if key_id:
        expiry = as_utc(key_data.get("expires_at"))

        expiry_text = (
            expiry.strftime("%d-%m-%Y %H:%M UTC")
            if expiry
            else "Unknown"
        )

        await update.message.reply_text(
            "🐯 <b>TIGER MOD</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "✅ <b>Premium Active</b>\n\n"
            f"🔑 Key: <code>{safe_text(key_id)}</code>\n"
            f"📅 Expiry: <code>{expiry_text}</code>\n\n"
            "Choose an option below:",
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
        )

        return

    await update.message.reply_text(
        "🐯 <b>TIGER MOD</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🔐 <b>Premium Access Required</b>\n\n"
        "You don't currently have an active key.\n\n"
        "Choose <b>Enter Key</b> if you already have a key "
        "or <b>Buy Key</b> to purchase access.",
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

# ============================================================
# RAZORPAY API
# ============================================================

def razorpay_api(method, path, **kwargs):
    url = "https://api.razorpay.com/v1" + path

    response = requests.request(
        method,
        url,
        auth=(
            RAZORPAY_KEY_ID,
            RAZORPAY_KEY_SECRET,
        ),
        timeout=20,
        **kwargs,
    )

    if not response.ok:
        raise RuntimeError(
            f"Razorpay HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    return response.json()


def create_razorpay_qr(plan, user):
    close_by = (
        timestamp()
        + QR_EXPIRY_MINUTES * 60
    )

    payload = {
        "type": "upi_qr",
        "name": "TIGER MOD",
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": plan["amount"],
        "description": (
            f"TIGER MOD {plan['name']}"
        ),
        "close_by": close_by,
        "notes": {
            "telegram_id": str(user.id),
            "plan_days": str(plan["days"]),
        },
    }

    return razorpay_api(
        "POST",
        "/payments/qr_codes",
        json=payload,
    )


def fetch_qr_payments(qr_id):
    return razorpay_api(
        "GET",
        f"/payments/qr_codes/{qr_id}/payments",
        params={"count": 100},
    )


def find_matching_captured_payment(
    qr_id,
    expected_amount,
):
    data = fetch_qr_payments(qr_id)

    for payment in data.get("items", []):
        if (
            payment.get("status") == "captured"
            and payment.get("captured") is True
            and int(payment.get("amount", 0))
            == int(expected_amount)
        ):
            return payment

    return None

# ============================================================
# PAYMENT CREATION
# ============================================================

async def create_qr_payment(
    query,
    user,
    plan_id,
):
    if not (
        RAZORPAY_KEY_ID
        and RAZORPAY_KEY_SECRET
    ):
        await query.edit_message_text(
            "❌ Razorpay is not configured."
        )
        return

    plan = PLANS[plan_id]

    try:
        qr = create_razorpay_qr(
            plan,
            user,
        )
    except Exception:
        logger.exception(
            "Razorpay QR creation failed"
        )

        await query.edit_message_text(
            "❌ Could not create the UPI QR.\n"
            "Please try again later.",
            reply_markup=buy_keyboard(),
        )

        return

    qr_id = qr["id"]
    payment_doc_id = f"qr_{qr_id}"

    db.collection("payments").document(
        payment_doc_id
    ).set(
        {
            "id": payment_doc_id,
            "qr_id": qr_id,
            "telegram_id": user.id,
            "plan_days": plan["days"],
            "amount": plan["amount"],
            "price": plan["price"],
            "status": "created",
            "created_at": firestore.SERVER_TIMESTAMP,
            "close_by": qr.get("close_by"),
        }
    )

    expiry = datetime.fromtimestamp(
        qr.get(
            "close_by",
            timestamp() + 1800,
        ),
        tz=timezone.utc,
    )

    caption = (
        "🏦 <b>METHOD:</b> UPI Automatic\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 <b>AMOUNT TO PAY:</b> "
        f"₹{plan['price']}\n"
        f"💱 <b>EXCHANGE RATE:</b> ₹1 = ₹1\n\n"
        f"🆔 <b>UPI ID:</b> "
        f"{safe_text(DISPLAY_UPI_ID)}\n"
        f"🧾 <b>Order ID:</b> "
        f"<code>{safe_text(qr_id)}</code>\n"
        f"⏰ <b>Expires:</b> "
        f"{expiry.strftime('%d-%m-%Y %H:%M:%S UTC')}\n\n"
        "👉 Pay the <b>exact amount</b>, then\n"
        "press ✅ <b>VERIFY PAYMENT</b>.\n"
        "Your premium access will be credited "
        "automatically after Razorpay confirms "
        "the captured payment."
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ VERIFY PAYMENT",
                    callback_data=f"verify:{qr_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ CANCEL",
                    callback_data=f"cancel:{qr_id}",
                )
            ],
        ]
    )

    try:
        await query.message.reply_photo(
            photo=qr["image_url"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

        await query.edit_message_text(
            "💳 <b>Payment QR generated.</b>\n\n"
            "Scan the QR above and pay the exact amount.",
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        logger.exception(
            "Could not send Razorpay QR to Telegram"
        )

        await query.edit_message_text(
            "❌ Could not send the payment QR."
        )

# ============================================================
# VERIFY PAYMENT
# ============================================================

async def verify_qr_payment(
    query,
    qr_id,
):
    doc_ref = db.collection(
        "payments"
    ).document(f"qr_{qr_id}")

    doc = doc_ref.get()

    if not doc.exists:
        await query.answer(
            "Payment record not found.",
            show_alert=True,
        )
        return

    data = doc.to_dict()

    if int(
        data.get("telegram_id", 0)
    ) != query.from_user.id:
        await query.answer(
            "❌ This payment belongs to another user.",
            show_alert=True,
        )
        return

    if data.get("status") == "paid":
        await query.answer(
            "✅ Payment already verified.",
            show_alert=True,
        )
        return

    try:
        payment = find_matching_captured_payment(
            qr_id,
            data["amount"],
        )

    except Exception:
        logger.exception(
            "Razorpay payment verification failed"
        )

        await query.answer(
            "⚠️ Razorpay check failed. Try again.",
            show_alert=True,
        )
        return

    if not payment:
        await query.answer(
            "❌ Payment not found yet.\n"
            "Pay the exact amount and try again.",
            show_alert=True,
        )
        return

    payment_id = payment["id"]
    plan_days = int(data["plan_days"])

    # Re-check before creating a key to avoid duplicate
    # credits if two verification clicks happen together.
    fresh = doc_ref.get().to_dict()

    if fresh.get("status") == "paid":
        await query.answer(
            "✅ Payment already verified.",
            show_alert=True,
        )
        return

    key, expiry = create_key(
        plan_days,
        "razorpay",
        telegram_id=query.from_user.id,
        payment_id=payment_id,
        order_id=qr_id,
        price=int(data["price"]),
    )

    doc_ref.update(
        {
            "status": "paid",
            "payment_id": payment_id,
            "verified_at": firestore.SERVER_TIMESTAMP,
            "key": key,
        }
    )

    await query.answer(
        "✅ Payment verified!",
        show_alert=True,
    )

    await query.message.reply_text(
        "🎉 <b>PAYMENT VERIFIED</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🐯 TIGER MOD\n\n"
        f"📦 Plan: <b>{plan_days} Day(s)</b>\n\n"
        "🔑 Your Premium Key:\n"
        f"<code>{safe_text(key)}</code>\n\n"
        "📅 Expires:\n"
        f"<b>{expiry.strftime('%d-%m-%Y %H:%M UTC')}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=home_keyboard(),
    )

# ============================================================
# CANCEL PAYMENT
# ============================================================

async def cancel_payment(
    query,
    qr_id,
):
    ref = db.collection(
        "payments"
    ).document(f"qr_{qr_id}")

    doc = ref.get()

    if doc.exists:
        data = doc.to_dict()

        if (
            int(data.get("telegram_id", 0))
            == query.from_user.id
            and data.get("status") != "paid"
        ):
            ref.update(
                {
                    "status": "cancelled"
                }
            )

    await query.edit_message_text(
        "❌ <b>Payment cancelled.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=buy_keyboard(),
    )

# ============================================================
# CALLBACK HANDLER
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

    if data == "home":
        key_id, _ = find_active_key(
            user.id
        )

        if key_id:
            await query.edit_message_text(
                "🐯 <b>TIGER MOD</b>\n"
                "━━━━━━━━━━━━━━\n\n"
                "Choose an option:",
                parse_mode=ParseMode.HTML,
                reply_markup=home_keyboard(),
            )
        else:
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

    if data == "enter_key":
        context.user_data[
            "waiting_for_key"
        ] = True

        await query.edit_message_text(
            "🔑 <b>Enter your premium key</b>\n\n"
            "Send the complete key.",
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

    if data == "buy":
        await query.edit_message_text(
            "💳 <b>Choose Premium Plan</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "Select your plan:",
            parse_mode=ParseMode.HTML,
            reply_markup=buy_keyboard(),
        )

        return

    if data == "mykey":
        key_id, key_data = find_active_key(
            user.id
        )

        if not key_id:
            await query.edit_message_text(
                "❌ No active key.",
                reply_markup=buy_keyboard(),
            )
            return

        expiry = as_utc(
            key_data.get("expires_at")
        )

        await query.edit_message_text(
            "🔑 <b>YOUR PREMIUM KEY</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"<code>{safe_text(key_id)}</code>\n\n"
            f"Plan: <b>"
            f"{safe_text(key_data.get('plan'))}"
            f"</b>\n"
            f"Expiry: <b>"
            f"{expiry.strftime('%d-%m-%Y %H:%M UTC') if expiry else 'Unknown'}"
            f"</b>",
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

    if data == "refresh":
        await query.edit_message_text(
            "🐯 <b>TIGER MOD</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            "Status refreshed.",
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
        )
        return

    if data.startswith("plan:"):
        plan_id = data.split(
            ":",
            1,
        )[1]

        if plan_id not in PLANS:
            await query.edit_message_text(
                "❌ Invalid plan."
            )
            return

        await create_qr_payment(
            query,
            user,
            plan_id,
        )
        return

    if data.startswith("verify:"):
        await verify_qr_payment(
            query,
            data.split(":", 1)[1],
        )
        return

    if data.startswith("cancel:"):
        await cancel_payment(
            query,
            data.split(":", 1)[1],
        )
        return

    if data.startswith("admin:"):
        if not is_admin(user):
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

# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await track(update)

    user = update.effective_user
    text = (
        update.message.text or ""
    ).strip()

    if context.user_data.get(
        "waiting_for_key"
    ):
        context.user_data[
            "waiting_for_key"
        ] = False

        success, message = activate_key(
            text.upper(),
            user.id,
        )

        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
        )

        if success:
            await update.message.reply_text(
                "🐯 <b>TIGER MOD</b>\n"
                "━━━━━━━━━━━━━━\n\n"
                "Choose an option:",
                parse_mode=ParseMode.HTML,
                reply_markup=home_keyboard(),
            )

        return

    await update.message.reply_text(
        "🐯 Use /start to open TIGER MOD."
    )

# ============================================================
# ADMIN
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

    current = now_utc()

    active = 0
    expired = 0
    sold = 0
    free = 0
    revenue = 0

    for doc in keys:
        data = doc.to_dict()

        if data.get("source") == "razorpay":
            sold += 1

        elif data.get("source") == "admin":
            free += 1

        if data.get("status") == "active":
            expiry = as_utc(
                data.get("expires_at")
            )

            if expiry and expiry > current:
                active += 1

            elif expiry:
                expired += 1

                try:
                    doc.reference.update(
                        {
                            "status": "expired"
                        }
                    )
                except Exception:
                    pass

    for doc in payments:
        revenue += (
            int(
                doc.to_dict().get(
                    "amount",
                    0,
                )
            )
            // 100
        )

    online_limit = (
        current
        - timedelta(
            minutes=ONLINE_TIMEOUT_MINUTES
        )
    )

    online = 0

    for doc in users:
        last_seen = as_utc(
            doc.to_dict().get(
                "last_seen"
            )
        )

        if (
            last_seen
            and last_seen >= online_limit
        ):
            online += 1

    return {
        "users": len(users),
        "online": online,
        "active": active,
        "expired": expired,
        "generated": len(keys),
        "sold": sold,
        "free": free,
        "revenue": revenue,
    }


async def admin_command(
    update,
    context,
):
    await track(update)

    if not is_admin(
        update.effective_user
    ):
        await update.message.reply_text(
            "❌ Unauthorized."
        )
        return

    s = get_statistics()

    await update.message.reply_text(
        "🐯 <b>TIGER MOD ADMIN</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"👥 Users: <b>{s['users']}</b>\n"
        f"🟢 Online: <b>{s['online']}</b>\n"
        f"🔑 Active Keys: <b>{s['active']}</b>\n"
        f"💳 Keys Sold: <b>{s['sold']}</b>\n"
        f"🎟 Generated Keys: <b>{s['generated']}</b>\n"
        f"💵 Revenue: <b>₹{s['revenue']}</b>\n\n"
        "Choose an option:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


async def admin_callback(
    update,
    context,
    data,
):
    query = update.callback_query

    if not is_admin(
        query.from_user
    ):
        await query.answer(
            "Unauthorized",
            show_alert=True,
        )
        return

    action = data.split(
        ":",
        1,
    )[1]

    if action in (
        "refresh",
        "home",
        "stats",
    ):
        s = get_statistics()

        await query.edit_message_text(
            "📊 <b>TIGER MOD ADMIN</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"👥 Users: <b>{s['users']}</b>\n"
            f"🟢 Online: <b>{s['online']}</b>\n"
            f"🔑 Active Keys: <b>{s['active']}</b>\n"
            f"⏰ Expired Keys: <b>{s['expired']}</b>\n"
            f"🎟 Generated: <b>{s['generated']}</b>\n"
            f"💳 Paid Keys: <b>{s['sold']}</b>\n"
            f"🎁 Free Keys: <b>{s['free']}</b>\n"
            f"💵 Revenue: <b>₹{s['revenue']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )

        return

    if action == "gen":
        await query.edit_message_text(
            "🔑 <b>Generate Free Key</b>\n\n"
            "Choose duration:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
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
            ),
        )

        return

    if action in (
        "gen1",
        "gen7",
        "gen30",
    ):
        days = int(
            action.replace(
                "gen",
                "",
            )
        )

        key, _ = create_key(
            days,
            "admin",
        )

        await query.edit_message_text(
            "✅ <b>KEY GENERATED</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"📦 Plan: <b>{days} Day(s)</b>\n\n"
            f"🔑 <code>{safe_text(key)}</code>\n\n"
            "Generated without payment.",
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
            data = doc.to_dict()

            if data.get("username"):
                name = (
                    "@"
                    + data["username"]
                )
            else:
                name = (
                    data.get(
                        "first_name",
                        "",
                    )
                    or doc.id
                )

            lines.append(
                "• "
                + safe_text(name)
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

        lines = [
            "💳 <b>RECENT SALES</b>",
            "━━━━━━━━━━━━━━",
            "",
        ]

        for doc in payments:
            data = doc.to_dict()

            lines.append(
                f"• ₹{int(data.get('amount', 0)) // 100}"
                f" | {data.get('plan_days', '?')}D"
                f" | {data.get('telegram_id', '?')}"
            )

        if len(lines) == 3:
            lines.append(
                "No sales yet."
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

    if action == "keys":
        keys = list(
            db.collection(
                "premium_keys"
            )
            .limit(100)
            .stream()
        )

        active = sum(
            1
            for doc in keys
            if doc.to_dict().get(
                "status"
            ) == "active"
        )

        unused = sum(
            1
            for doc in keys
            if doc.to_dict().get(
                "status"
            ) == "unused"
        )

        expired = sum(
            1
            for doc in keys
            if doc.to_dict().get(
                "status"
            ) == "expired"
        )

        await query.edit_message_text(
            "🔐 <b>KEY MANAGEMENT</b>\n"
            "━━━━━━━━━━━━━━\n\n"
            f"🟢 Active: <b>{active}</b>\n"
            f"⚪ Unused: <b>{unused}</b>\n"
            f"⏰ Expired: <b>{expired}</b>",
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

# ============================================================
# COMMANDS / MAIN
# ============================================================

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


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    if not (
        FIREBASE_CREDENTIALS_B64
        or os.path.exists(FIREBASE_JSON)
    ):
        raise RuntimeError(
            "Firebase credentials are missing."
        )

    if not (
        RAZORPAY_KEY_ID
        and RAZORPAY_KEY_SECRET
    ):
        logger.warning(
            "Razorpay credentials are missing. "
            "Payments will be unavailable."
        )

    threading.Thread(
        target=start_flask,
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    logger.info(
        "🐯 TIGER MOD BOT STARTING..."
    )

    logger.info(
        "Admin: @%s",
        ADMIN_USERNAME,
    )

    logger.info(
        "Payment/health server: %s:%s",
        HOST,
        PORT,
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
