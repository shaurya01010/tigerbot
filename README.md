# Tiger Mod Bot

Telegram prediction bot with premium key access, ZapUPI payments, QR payment flow, payment verification, automatic key delivery, admin key controls, and broadcast support.

## Preserved prediction logic
The existing prediction pattern/period logic in `services/prediction.py` is kept unchanged. The Telegram layer only controls access and presentation.

## User flow
1. `/start`
2. Enter Key or Buy Access
3. Select 1 / 7 / 30 days
4. ZapUPI order is created with a unique order ID
5. Bot shows a QR code and secure payment button
6. User verifies payment
7. On confirmed payment, exactly one key is issued
8. Key is bound to the user's Telegram account when activated
9. User can request predictions only while access is active

## Environment
```text
BOT_TOKEN=
ADMIN_IDS=
ZAPUPI_KEY=
ZAPUPI_WEBHOOK_URL=https://YOUR-RENDER-DOMAIN/webhook/zapupi
PRICE_1D=20
PRICE_7D=99
PRICE_30D=249
DATABASE_FILE=tigerbot.db
PORT=10000
```

## Render
Build command:
```bash
pip install -r requirements.txt
```

Start command:
```bash
gunicorn --workers 1 --threads 2 --timeout 120 app:app
```

## ZapUPI
Configure the same webhook URL in ZapUPI if using automatic webhook delivery. The bot also verifies the order directly with the order-status API before issuing a key.

Never commit `.env` or real API keys/tokens to GitHub.
