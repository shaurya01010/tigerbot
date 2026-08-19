
import os, requests, threading
from database import update_order, get_order, create_key
from telegram import Bot

BASE="https://pay.zapupi.com/api"
ZAP_KEY=os.getenv("ZAPUPI_KEY","")
BOT_TOKEN=os.getenv("BOT_TOKEN","")

def create_order(order_id, amount, remark=""):
    payload={"zap_key":ZAP_KEY,"order_id":order_id,"amount":str(amount)}
    if remark: payload["remark"]=remark
    # The global webhook is configured in ZapUPI; webhook_url may also be sent per order.
    webhook=os.getenv("ZAPUPI_WEBHOOK_URL","")
    if webhook: payload["webhook_url"]=webhook
    r=requests.post(BASE+"/create-order",json=payload,timeout=15)
    r.raise_for_status()
    return r.json()

def order_status(order_id):
    r=requests.post(BASE+"/order-status",json={"zap_key":ZAP_KEY,"order_id":order_id},timeout=15)
    r.raise_for_status()
    return r.json()

def handle_webhook(data):
    order_id=data.get("order_id","")
    status=data.get("status","")
    if not order_id: return
    order=get_order(order_id)
    if not order: return
    # Double-confirm successful payments server-side.
    if status=="Success":
        try:
            confirmed=order_status(order_id)
            remote=confirmed.get("data",{})
            if remote.get("status")!="Success":
                return
            status="Success"
            txn_id=remote.get("txn_id",data.get("txn_id",""))
            utr=remote.get("utr",data.get("utr",""))
        except Exception:
            return
        update_order(order_id,status="Success",txn_id=txn_id,utr=utr,
                     paid_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        key=create_key(order["plan_days"])
        update_order(order_id, status="Success")
        if BOT_TOKEN:
            def send():
                try:
                    Bot(BOT_TOKEN).send_message(
                        chat_id=order["telegram_id"],
                        text=f"✅ Payment verified!\n\n🔑 Your {order['plan_days']}-day key:\n\n`{key}`\n\nUse /start to activate it.",
                        parse_mode="Markdown")
                except Exception as e: print("Telegram delivery error:",e)
            threading.Thread(target=send,daemon=True).start()
    elif status=="Failed":
        update_order(order_id,status="Failed",txn_id=data.get("txn_id",""),utr=data.get("utr",""))
