import hashlib
import hmac
import json
import os
import re
import time
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

BASE_URL = "https://api.india.delta.exchange"

# Configure Master + Slave accounts and lot multipliers
ACCOUNTS = [
    {
        "name": "MASTER",
        "key": os.environ["MASTER_KEY"],
        "secret": os.environ["MASTER_SECRET"],
        "multiplier": 1,
    },
    {
        "name": "SLAVE_1",
        "key": os.environ["SLAVE1_KEY"],
        "secret": os.environ["SLAVE1_SECRET"],
        "multiplier": 1,
    },
]

ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Format: BUY BTCUSD 2  or  SELL ETHUSD 5
SIGNAL_REGEX = re.compile(r"^(BUY|SELL)\s+([A-Z0-9_-]+)\s+(\d+)$", re.IGNORECASE)
PRODUCT_CACHE: dict[str, int] = {}


def load_products() -> None:
    """Fetch active futures product IDs from Delta Exchange."""
    resp = requests.get(f"{BASE_URL}/v2/products", timeout=10)
    resp.raise_for_status()
    products = resp.json().get("result", [])
    for p in products:
        if p.get("contract_type") in ("perpetual_futures", "futures") and p.get("state") == "live":
            PRODUCT_CACHE[p["symbol"].upper()] = int(p["id"])


def sign_request(api_secret: str, method: str, path: str, query_string: str = "", payload: str = "") -> tuple[str, str]:
    timestamp = str(int(time.time()))
    prehash = f"{method.upper()}{timestamp}{path}{query_string}{payload}"
    signature = hmac.new(
        api_secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return timestamp, signature


def place_delta_order(api_key: str, api_secret: str, product_id: int, side: str, size: int) -> dict:
    method = "POST"
    path = "/v2/orders"
    payload_dict = {
        "product_id": product_id,
        "size": size,
        "side": side,
        "order_type": "market_order",
    }
    body = json.dumps(payload_dict, separators=(",", ":"))
    timestamp, signature = sign_request(api_secret, method, path, payload=body)

    headers = {
        "api-key": api_key,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
    }
    resp = requests.post(f"{BASE_URL}{path}", headers=headers, data=body, timeout=10)
    data = resp.json()
    if not resp.ok or not data.get("success"):
        raise RuntimeError(f"HTTP {resp.status_code}: {data}")
    return data["result"]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await update.effective_message.reply_text(
        "Delta Copier Online.\nSend orders like:\n`BUY BTCUSD 1`\n`SELL ETHUSD 2`",
        parse_mode="Markdown",
    )


async def handle_trade_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != ALLOWED_CHAT_ID:
        return

    text = (update.effective_message.text or "").strip()
    match = SIGNAL_REGEX.match(text)
    if not match:
        await update.effective_message.reply_text("Invalid format. Use: `BUY BTCUSD 1`", parse_mode="Markdown")
        return

    side_raw, symbol_raw, base_qty_raw = match.groups()
    side = "buy" if side_raw.upper() == "BUY" else "sell"
    symbol = symbol_raw.upper()
    base_qty = int(base_qty_raw)

    product_id = PRODUCT_CACHE.get(symbol)
    if not product_id:
        await update.effective_message.reply_text(f"Unknown or inactive futures symbol: `{symbol}`", parse_mode="Markdown")
        return

    results = []
    for acct in ACCOUNTS:
        qty = base_qty * acct["multiplier"]
        if qty <= 0:
            continue
        try:
            order = place_delta_order(acct["key"], acct["secret"], product_id, side, qty)
            results.append(f"✅ {acct['name']}: {side.upper()} {qty} {symbol} (Order ID: {order.get('id')})")
        except Exception as exc:
            results.append(f"❌ {acct['name']}: Failed ({exc})")

    await update.effective_message.reply_text("\n".join(results))


if __name__ == "__main__":
    load_products()
    print(f"Loaded {len(PRODUCT_CACHE)} live futures contracts.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_trade_signal))
    app.run_polling()
