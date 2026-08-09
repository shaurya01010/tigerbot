# TIGER MOD - Telegram Premium Access + Razorpay UPI QR

This version uses Razorpay's Dynamic UPI QR API. It does not require a Razorpay webhook for the Verify button flow.

Render environment variables:
- BOT_TOKEN
- RAZORPAY_KEY_ID
- RAZORPAY_KEY_SECRET
- FIREBASE_CREDENTIALS_B64
- DISPLAY_UPI_ID (optional)

Render:
Build command:
pip install -r requirements.txt

Start command:
python bot.py
