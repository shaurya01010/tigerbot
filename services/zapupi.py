import os
import time
import requests
from datetime import datetime, timezone

from database import update_order, get_order, issue_order_key

BASE_URL = "https://pay.zapupi.com/api"


def get_zapupi_key():
    """
    Supports multiple environment-variable names so the integration
    does not silently fail if the key was named differently in Render.
    """
    key = (
        os.getenv("ZAPUPI_KEY")
        or os.getenv("ZAPUPI_API_KEY")
        or os.getenv("ZAP_KEY")
        or ""
    ).strip()

    return key


def _post(endpoint, payload, timeout=30):
    """
    Send request to ZapUPI and return parsed JSON.
    """

    zap_key = get_zapupi_key()

    if not zap_key:
        raise RuntimeError(
            "ZapUPI API key is missing. "
            "Set ZAPUPI_KEY in Render Environment Variables."
        )

    request_payload = dict(payload)
    request_payload["zap_key"] = zap_key

    # Never print the real API key.
    debug_payload = dict(request_payload)
    debug_payload["zap_key"] = "***HIDDEN***"

    print(
        f"[ZapUPI] POST /{endpoint} "
        f"payload={debug_payload}"
    )

    last_error = None

    for attempt in range(1, 3):
        try:
            response = requests.post(
                f"{BASE_URL}/{endpoint}",
                json=request_payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=timeout,
            )

            print(
                f"[ZapUPI] HTTP STATUS: {response.status_code}"
            )

            try:
                data = response.json()
            except ValueError:
                print(
                    "[ZapUPI] Invalid JSON response:",
                    response.text[:1000],
                )

                raise RuntimeError(
                    "ZapUPI returned an invalid response."
                )

            print(
                f"[ZapUPI] RESPONSE: {data}"
            )

            gateway_status = str(
                data.get("status", "")
            ).lower()

            if response.status_code >= 400:
                message = (
                    data.get("message")
                    or data.get("error")
                    or f"HTTP {response.status_code}"
                )

                raise RuntimeError(
                    f"ZapUPI rejected the request: {message}"
                )

            if gateway_status != "success":
                message = (
                    data.get("message")
                    or data.get("error")
                    or "Unknown ZapUPI error"
                )

                raise RuntimeError(
                    f"ZapUPI rejected the order: {message}"
                )

            return data

        except requests.Timeout as exc:
            last_error = exc

            print(
                f"[ZapUPI] TIMEOUT attempt {attempt}: "
                f"{type(exc).__name__}: {exc}"
            )

            if attempt < 2:
                time.sleep(2)

        except requests.RequestException as exc:
            last_error = exc

            print(
                f"[ZapUPI] NETWORK ERROR attempt {attempt}: "
                f"{type(exc).__name__}: {exc}"
            )

            if attempt < 2:
                time.sleep(2)

        except RuntimeError:
            raise

    raise RuntimeError(
        "ZapUPI payment server could not be reached. "
        "Please try again."
    ) from last_error


def create_order(order_id, amount, remark=""):
    """
    Create a ZapUPI payment order.
    """

    order_id = str(order_id).strip()

    if not order_id:
        raise RuntimeError("Payment order ID is empty.")

    if len(order_id) > 60:
        raise RuntimeError("Payment order ID is too long.")

    # ZapUPI order IDs should be simple alphanumeric values.
    if not order_id.isalnum():
        raise RuntimeError(
            "Payment order ID contains invalid characters."
        )

    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        raise RuntimeError("Invalid payment amount.")

    if amount_value <= 0:
        raise RuntimeError(
            "Payment amount must be greater than zero."
        )

    # Keep the amount clean.
    if amount_value.is_integer():
        amount_value = int(amount_value)

    payload = {
        "order_id": order_id,
        "amount": amount_value,
    }

    if remark:
        payload["remark"] = str(remark)[:500]

    # Optional webhook.
    webhook_url = os.getenv(
        "ZAPUPI_WEBHOOK_URL",
        ""
    ).strip()

    if webhook_url:
        payload["webhook_url"] = webhook_url

    print(
        f"[ZapUPI] Creating order: "
        f"{order_id} | ₹{amount_value}"
    )

    data = _post(
        "create-order",
        payload,
        timeout=30,
    )

    payment_url = data.get("payment_url")

    if not payment_url:
        print(
            "[ZapUPI] Missing payment_url:",
            data
        )

        raise RuntimeError(
            "ZapUPI created the order but did not "
            "return a payment URL."
        )

    print(
        f"[ZapUPI] ORDER CREATED SUCCESSFULLY: {order_id}"
    )

    return data


def order_status(order_id):
    """
    Check payment status directly with ZapUPI.
    """

    order_id = str(order_id).strip()

    if not order_id:
        raise RuntimeError("Invalid order ID.")

    return _post(
        "order-status",
        {
            "order_id": order_id,
        },
        timeout=30,
    )


def fulfill_success(order_id, txn_id="", utr=""):
    """
    Mark order as paid and generate the access key.
    """

    order = get_order(order_id)

    if not order:
        print(
            f"[ZapUPI] Cannot fulfill unknown order: {order_id}"
        )
        return None

    update_order(
        order_id,
        status="Success",
        txn_id=txn_id or order.get("txn_id", ""),
        utr=utr or order.get("utr", ""),
        paid_at=datetime.now(
            timezone.utc
        ).isoformat(),
    )

    return issue_order_key(order_id)


def _notify_key(order, key):
    """
    Send generated access key to Telegram.
    """

    bot_token = os.getenv(
        "BOT_TOKEN",
        ""
    ).strip()

    if not bot_token or not key:
        return

    amount = float(order["amount"])

    text = (
        "🐯 <b>TIGER MOD</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎉 <b>PAYMENT VERIFIED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Your premium access is now ready.\n\n"
        f"📦 <b>Plan:</b> {order['plan_days']} Days\n"
        f"💰 <b>Amount:</b> ₹{amount:g}\n"
        f"🧾 <b>Order:</b> "
        f"<code>{order['order_id']}</code>\n\n"
        "🔑 <b>YOUR ACCESS KEY</b>\n"
        f"<code>{key}</code>\n\n"
        "🔐 Activate this key in the bot using "
        "<b>ENTER KEY</b>.\n\n"
        "⚠️ This key can only be activated on "
        "one Telegram account.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛟 <b>Support:</b> @Tiger_Key"
    )

    try:
        response = requests.post(
            f"https://api.telegram.org/bot"
            f"{bot_token}/sendMessage",
            json={
                "chat_id": order["telegram_id"],
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=15,
        )

        response.raise_for_status()

    except Exception as exc:
        print(
            "[Telegram] Key notification failed:",
            type(exc).__name__,
            exc,
        )


def handle_webhook(data):
    """
    ZapUPI webhook receiver.
    """

    data = data or {}

    order_id = str(
        data.get("order_id", "")
    ).strip()

    status = str(
        data.get("status", "")
    ).strip()

    if not order_id:
        print("[ZapUPI] Webhook missing order_id.")
        return None

    order = get_order(order_id)

    if not order:
        print(
            f"[ZapUPI] Unknown webhook order: {order_id}"
        )
        return None

    if status.lower() == "success":

        try:
            # Double-confirm payment using Order Status API.
            confirmed = order_status(order_id)

            remote = (
                confirmed.get("data")
                or {}
            )

            remote_status = str(
                remote.get("status", "")
            ).lower()

            if remote_status != "success":
                print(
                    "[ZapUPI] Webhook said Success "
                    f"but API says {remote_status}"
                )
                return None

            already_issued = order.get(
                "issued_key",
                ""
            )

            key = fulfill_success(
                order_id,
                txn_id=remote.get(
                    "txn_id",
                    data.get("txn_id", "")
                ),
                utr=remote.get(
                    "utr",
                    data.get("utr", "")
                ),
            )

            if key and not already_issued:
                _notify_key(order, key)

            return key

        except Exception as exc:
            print(
                "[ZapUPI] Webhook confirmation error:",
                type(exc).__name__,
                exc,
            )

            return None

    if status.lower() == "failed":

        update_order(
            order_id,
            status="Failed",
            txn_id=data.get("txn_id", ""),
            utr=data.get("utr", ""),
        )

        print(
            f"[ZapUPI] Order failed: {order_id}"
        )

    return None