
import os, uuid, asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import upsert_user, active_key, activate_key, create_key, disable_key, all_users, get_plan, save_order, get_order, keys_page
from services.zapupi import create_order, order_status
from services.prediction import get_prediction

ADMIN_IDS={int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}
PRICE={1:float(os.getenv("PRICE_1D","20")),7:float(os.getenv("PRICE_7D","99")),30:float(os.getenv("PRICE_30D","249"))}

def access_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔑 Enter Key",callback_data="enter_key"),
                                  InlineKeyboardButton("🛒 Buy Key",callback_data="buy")]])

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Prediction",callback_data="prediction")],
        [InlineKeyboardButton("🔑 My Key",callback_data="mykey")],
        [InlineKeyboardButton("🛒 Buy Key",callback_data="buy")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Key",callback_data="acreate")],
        [InlineKeyboardButton("📋 Keys",callback_data="akeys")],
        [InlineKeyboardButton("⛔ Stop Key",callback_data="astop")],
        [InlineKeyboardButton("📢 Broadcast",callback_data="abroadcast")]
    ])

async def start(update,context):
    upsert_user(update.effective_user)
    a=active_key(update.effective_user.id)
    if a:
        await update.message.reply_text(f"✅ Access granted\n\nPlan: {a['plan_days']} days\nExpires: {a['expires_at']}",reply_markup=main_keyboard())
    else:
        await update.message.reply_text("🔐 You don't have an active key.",reply_markup=access_keyboard())

async def admin_command(update,context):
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("🛠 Admin Panel",reply_markup=admin_keyboard())

async def callbacks(update,context):
    q=update.callback_query; await q.answer()
    uid=q.from_user.id
    if q.data=="enter_key":
        context.user_data["mode"]="key"; await q.message.reply_text("🔑 Send your key:")
    elif q.data=="buy":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("1 Day",callback_data="plan_1"),
                                   InlineKeyboardButton("7 Days",callback_data="plan_7"),
                                   InlineKeyboardButton("30 Days",callback_data="plan_30")],
                                  [InlineKeyboardButton("❌ Cancel",callback_data="cancel")]])
        await q.message.reply_text("🛒 Choose your plan:",reply_markup=kb)
    elif q.data.startswith("plan_"):
        days=int(q.data.split("_")[1]); amount=PRICE[days]
        order_id="TG"+str(uid)+"_"+uuid.uuid4().hex[:14]
        save_order(order_id,uid,days,amount)
        try:
            data=create_order(order_id,amount,f"Plan {days} Days | Telegram {uid}")
            if data.get("status")!="success":
                await q.message.reply_text("❌ Could not create payment order.")
                return
            pay=data.get("payment_url")
            kb=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 PAY / OPEN QR",url=pay)],
                [InlineKeyboardButton("✅ VERIFY",callback_data="verify_"+order_id)],
                [InlineKeyboardButton("❌ CANCEL",callback_data="cancel_"+order_id)]
            ])
            await q.message.reply_text(f"💳 Payment\n\nPlan: {days} Days\nAmount: ₹{amount:g}\nOrder: {order_id}\n\nComplete payment, then press VERIFY.",reply_markup=kb)
        except Exception as e:
            print(e); await q.message.reply_text("❌ Payment service error. Try again.")
    elif q.data.startswith("verify_"):
        order_id=q.data[7:]
        try:
            data=order_status(order_id); d=data.get("data",{})
            if d.get("status")=="Success":
                # Webhook normally delivers the key. If webhook raced/missed, do a safe local fallback.
                await q.message.reply_text("✅ Payment is confirmed. Your key will be delivered automatically.")
            elif d.get("status")=="Pending":
                await q.message.reply_text("⏳ Payment not completed yet.")
            else:
                await q.message.reply_text("❌ Payment failed.")
        except Exception: await q.message.reply_text("⚠️ Could not verify right now. Try again.")
    elif q.data=="cancel":
        await q.message.reply_text("❌ Cancelled.")
    elif q.data.startswith("cancel_"):
        await q.message.reply_text("❌ Payment cancelled on the bot. Unpaid order will not issue a key.")
    elif q.data=="prediction":
        if not active_key(uid): await q.message.reply_text("⛔ No active key.",reply_markup=access_keyboard()); return
        context.user_data["mode"]="period"; await q.message.reply_text("🎯 Enter the period number:")
    elif q.data=="mykey":
        a=active_key(uid)
        await q.message.reply_text(f"🔑 Key: `{a['key']}`\nPlan: {a['plan_days']} days\nExpires: {a['expires_at']}" if a else "⛔ No active key.",parse_mode="Markdown")
    elif uid in ADMIN_IDS and q.data=="acreate":
        context.user_data["mode"]="admin_create"; await q.message.reply_text("Enter key duration: 1, 7 or 30")
    elif uid in ADMIN_IDS and q.data=="akeys":
        rows=keys_page()
        text="\n".join(f"`{r['key']}` — {r['plan_days']}d — {r['status']}" for r in rows) or "No keys."
        await q.message.reply_text("🔑 Recent keys:\n\n"+text,parse_mode="Markdown")
    elif uid in ADMIN_IDS and q.data=="astop":
        context.user_data["mode"]="admin_stop"; await q.message.reply_text("Send the key to disable:")
    elif uid in ADMIN_IDS and q.data=="abroadcast":
        context.user_data["mode"]="broadcast"; await q.message.reply_text("Send the broadcast message:")

async def text_message(update,context):
    uid=update.effective_user.id; mode=context.user_data.pop("mode",None); text=update.message.text.strip()
    if mode=="key":
        ok,msg=activate_key(text,uid); await update.message.reply_text(msg,reply_markup=main_keyboard() if ok else access_keyboard())
    elif mode=="period":
        if not active_key(uid): await update.message.reply_text("⛔ Key expired/disabled."); return
        await update.message.reply_text(get_prediction(text))
    elif mode=="admin_create" and uid in ADMIN_IDS:
        if text not in ("1","7","30"): await update.message.reply_text("Use 1, 7 or 30."); return
        key=create_key(int(text)); await update.message.reply_text(f"✅ Key created\n\n`{key}`\n\nPlan: {text} days\nStatus: UNUSED",parse_mode="Markdown")
    elif mode=="admin_stop" and uid in ADMIN_IDS:
        await update.message.reply_text("⛔ Key disabled." if disable_key(text) else "❌ Key not found.")
    elif mode=="broadcast" and uid in ADMIN_IDS:
        sent=failed=0
        for target in all_users():
            try:
                await context.bot.send_message(target,text); sent+=1
                await asyncio.sleep(0.05)
            except Exception: failed+=1
        await update.message.reply_text(f"📢 Broadcast finished\n\nSent: {sent}\nFailed: {failed}")
