import os
import requests
from datetime import datetime, timezone
from database import update_order, get_order, issue_order_key

BASE = "https://pay.zapupi.com/api"
ZAP_KEY = os.getenv("ZAPUPI_KEY", "").strip()


def _json_response(response):
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"ZapUPI returned HTTP {response.status_code} with a non-JSON response")
    return data


def create_order(order_id, amount, remark=""):
    if not ZAP_KEY:
        raise RuntimeError("ZAPUPI_KEY is missing in Render Environment Variables")

    payload = {
        "zap_key": ZAP_KEY,
        "order_id": str(order_id),
        "amount": float(amount),
    }
    if remark:
        payload["remark"] = remark

    # ZapUPI supports a per-order webhook override. If configured, use it;
    # otherwise the dashboard webhook is used.
    webhook = os.getenv("ZAPUPI_WEBHOOK_URL", "").strip()
    if webhook:
        payload["webhook_url"] = webhook

    response = requests.post(
        f"{BASE}/create-order",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    data = _json_response(response)

    if response.status_code >= 400 or str(data.get("status", "")).lower() != "success":
        message = data.get("message") or data.get("error") or f"HTTP {response.status_code}"
        raise RuntimeError(f"ZapUPI: {message}")

    payment_url = data.get("payment_url")
    if not payment_url:
        raise RuntimeError("ZapUPI order was created but no payment URL was returned")

    return data


def order_status(order_id):
    if not ZAP_KEY:
        raise RuntimeError("ZAPUPI_KEY is missing in Render Environment Variables")
    response = requests.post(
        f"{BASE}/order-status",
        json={"zap_key": ZAP_KEY, "order_id": str(order_id)},
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    data = _json_response(response)
    if response.status_code >= 400 or str(data.get("status", "")).lower() != "success":
        message = data.get("message") or f"HTTP {response.status_code}"
        raise RuntimeError(f"ZapUPI: {message}")
    return data


def fulfill_success(order_id, txn_id="", utr=""):
    """Confirm local order and issue one key. Safe to call from verify and webhook."""
    order = get_order(order_id)
    if not order:
        return None

    update_order(
        order_id,
        status="Success",
        txn_id=txn_id or order.get("txn_id", ""),
        utr=utr or order.get("utr", ""),
        paid_at=datetime.now(timezone.utc).isoformat(),
    )
    return issue_order_key(order_id)


def _notify_key(order, key):
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token or not key:
        return
    text = (
        "🐯 <b>TIGER MOD</b>\n\n"
        "✅ <b>Payment Verified Successfully</b>\n\n"
        f"📦 <b>Plan:</b> {order['plan_days']} Days\n"
        f"💰 <b>Amount:</b> ₹{float(order['amount']):g}\n"
        f"🔑 <b>Your Access Key:</b>\n<code>{key}</code>\n\n"
        "Use <b>ENTER KEY</b> in the bot to activate your access.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛟 Support: @Tiger_Key"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": order["telegram_id"],
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=15,
        ).raise_for_status()
    except Exception as exc:
        print("Telegram key delivery error:", exc)


def handle_webhook(data):
    order_id = data.get("order_id", "")
    status = data.get("status", "")
    if not order_id:
        return None

    order = get_order(order_id)
    if not order:
        return None

    if status == "Success":
        try:
            confirmed = order_status(order_id)
            remote = confirmed.get("data", {})
            if remote.get("status") != "Success":
                return None
            # If Verify already issued the key, do not send a duplicate key.
            already_issued = order.get("issued_key", "")
            key = fulfill_success(
                order_id,
                txn_id=remote.get("txn_id", data.get("txn_id", "")),
                utr=remote.get("utr", data.get("utr", "")),
            )
            if key and not already_issued:
                _notify_key(order, key)
            return key
        except Exception as exc:
            print("ZapUPI webhook confirmation error:", exc)
            return None

    if status == "Failed":
        update_order(
            order_id,
            status="Failed",
            txn_id=data.get("txn_id", ""),
            utr=data.get("utr", ""),
        )
    return None
