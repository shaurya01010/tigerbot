import os
import time
import requests
from datetime import datetime, timezone
from database import update_order, get_order, issue_order_key

BASE = "https://pay.zapupi.com/api"


def _zap_key():
    return os.getenv("ZAPUPI_KEY", "").strip()


def _post(endpoint, payload, timeout=25):
    key = _zap_key()
    if not key:
        raise RuntimeError("Payment gateway is not configured. Add ZAPUPI_KEY in Render Environment Variables.")

    safe_payload = dict(payload)
    safe_payload["zap_key"] = "***"
    print(f"[ZapUPI] POST {endpoint} payload={safe_payload}")

    last_error = None
    for attempt in range(1, 3):
        try:
            response = requests.post(
                f"{BASE}/{endpoint}",
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=timeout,
            )
            try:
                data = response.json()
            except ValueError:
                raise RuntimeError(
                    f"ZapUPI returned an invalid response (HTTP {response.status_code})."
                )

            print(f"[ZapUPI] HTTP {response.status_code} response={data}")

            if response.status_code >= 400 or str(data.get("status", "")).lower() != "success":
                message = str(data.get("message") or data.get("error") or "Unknown gateway error")
                raise RuntimeError(f"{message} (HTTP {response.status_code})")
            return data
        except requests.RequestException as exc:
            last_error = exc
            print(f"[ZapUPI] network error attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(1.5)
        except RuntimeError:
            raise

    raise RuntimeError("Payment gateway is temporarily unreachable. Please try again in a moment.") from last_error


def create_order(order_id, amount, remark=""):
    order_id = str(order_id).strip()
    if not order_id or len(order_id) > 60 or not order_id.isalnum():
        raise RuntimeError("Invalid payment order ID. Please try again.")

    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        raise RuntimeError("Invalid payment amount.")
    if amount_value <= 0:
        raise RuntimeError("Payment amount must be greater than zero.")

    payload = {
        "zap_key": _zap_key(),
        "order_id": order_id,
        # ZapUPI documents amount as INR number/float. Keep two decimals only when needed.
        "amount": amount_value,
    }
    if remark:
        payload["remark"] = str(remark)[:500]

    webhook = os.getenv("ZAPUPI_WEBHOOK_URL", "").strip()
    if webhook:
        payload["webhook_url"] = webhook

    data = _post("create-order", payload)
    payment_url = data.get("payment_url")
    if not payment_url:
        raise RuntimeError("ZapUPI created the order but did not return a payment URL.")
    return data


def order_status(order_id):
    data = _post("order-status", {"zap_key": _zap_key(), "order_id": str(order_id)}, timeout=20)
    return data


def fulfill_success(order_id, txn_id="", utr=""):
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
        "🎉 <b>ACCESS KEY READY</b>\n\n"
        "Your payment has been verified successfully.\n\n"
        f"📦 <b>Plan:</b> {order['plan_days']} Days\n"
        f"💰 <b>Amount:</b> ₹{float(order['amount']):g}\n"
        f"🔑 <b>Access Key:</b>\n<code>{key}</code>\n\n"
        "🔐 Activate this key from the bot.\n"
        "⚠️ This key can only be activated on one Telegram account.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛟 <b>Support:</b> @Tiger_Key"
    )
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": order["telegram_id"], "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        response.raise_for_status()
    except Exception as exc:
        print("[Telegram] key delivery error:", type(exc).__name__, exc)


def handle_webhook(data):
    data = data or {}
    order_id = str(data.get("order_id", "")).strip()
    status = str(data.get("status", "")).strip()
    if not order_id:
        return None

    order = get_order(order_id)
    if not order:
        print(f"[ZapUPI] webhook ignored: unknown order {order_id}")
        return None

    if status.lower() == "success":
        try:
            confirmed = order_status(order_id)
            remote = confirmed.get("data") or {}
            if str(remote.get("status", "")).lower() != "success":
                print(f"[ZapUPI] webhook received Success but status check was {remote.get('status')}")
                return None

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
            print("[ZapUPI] webhook confirmation error:", type(exc).__name__, exc)
            return None

    if status.lower() == "failed":
        update_order(order_id, status="Failed", txn_id=data.get("txn_id", ""), utr=data.get("utr", ""))
    return None
