import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
# httpx logs full request URLs (which include the bot token) at INFO level
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("delta_copier")

# Load .env from the script directory (supports lines with or without `export`)
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip("'\"")
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip().strip("'\"")


# Use https://api.india.delta.exchange for India accounts,
# https://api.delta.exchange for global accounts.
BASE_URL = optional_env("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
DRY_RUN = optional_env("DRY_RUN", "false").lower() in ("1", "true", "yes")
MAX_QTY = int(optional_env("MAX_QTY", "100"))  # safety cap on base quantity per signal

BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = int(require_env("ALLOWED_CHAT_ID"))

ACCOUNTS: List[dict] = [
    {
        "name": "MASTER",
        "key": require_env("MASTER_KEY"),
        "secret": require_env("MASTER_SECRET"),
        "multiplier": int(optional_env("MASTER_MULTIPLIER", "1")),
    }
]
# Optional slave accounts: SLAVE1_KEY / SLAVE1_SECRET / SLAVE1_MULTIPLIER, SLAVE2_...
for i in range(1, 10):
    k, s = optional_env(f"SLAVE{i}_KEY"), optional_env(f"SLAVE{i}_SECRET")
    if k and s:
        ACCOUNTS.append(
            {
                "name": f"SLAVE{i}",
                "key": k,
                "secret": s,
                "multiplier": int(optional_env(f"SLAVE{i}_MULTIPLIER", "1")),
            }
        )

# Format: BUY BTCUSD 1  or  SELL ETHUSD 2
SIGNAL_REGEX = re.compile(r"^(BUY|SELL)\s+([A-Z0-9_-]+)\s+(\d+)$", re.IGNORECASE)
PRODUCT_CACHE: Dict[str, int] = {}

HEADERS_BASE = {"User-Agent": "delta-tg-copier/1.0", "Content-Type": "application/json"}


def load_products() -> None:
    """Fetch live futures product IDs from Delta Exchange (handles pagination)."""
    after = None
    while True:
        params = {"page_size": 500}
        if after:
            params["after"] = after
        resp = requests.get(f"{BASE_URL}/v2/products", params=params, headers=HEADERS_BASE, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("result", []):
            if p.get("contract_type") in ("perpetual_futures", "futures") and p.get("state") == "live":
                PRODUCT_CACHE[p["symbol"].upper()] = int(p["id"])
        after = (data.get("meta") or {}).get("after")
        if not after:
            break


def sign_request(api_secret: str, method: str, path: str, query_string: str = "", payload: str = "") -> Tuple[str, str]:
    timestamp = str(int(time.time()))
    prehash = f"{method.upper()}{timestamp}{path}{query_string}{payload}"
    signature = hmac.new(api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return timestamp, signature


def place_delta_order(api_key: str, api_secret: str, product_id: int, side: str, size: int) -> dict:
    method, path = "POST", "/v2/orders"
    body = json.dumps(
        {"product_id": product_id, "size": size, "side": side, "order_type": "market_order"},
        separators=(",", ":"),
    )
    timestamp, signature = sign_request(api_secret, method, path, payload=body)
    headers = {**HEADERS_BASE, "api-key": api_key, "timestamp": timestamp, "signature": signature}
    resp = requests.post(f"{BASE_URL}{path}", headers=headers, data=body, timeout=10)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if not resp.ok or not data.get("success"):
        err = data.get("error", data)
        raise RuntimeError(f"HTTP {resp.status_code}: {err}")
    return data["result"]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    names = ", ".join(f"{a['name']}(x{a['multiplier']})" for a in ACCOUNTS)
    await update.effective_message.reply_text(
        f"Bot online [{mode}]\nAccounts: {names}\nSend e.g.: BUY BTCUSD 1"
    )


async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await asyncio.to_thread(load_products)
    await update.effective_message.reply_text(f"Reloaded {len(PRODUCT_CACHE)} contracts.")


async def handle_trade_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    msg = update.effective_message
    match = SIGNAL_REGEX.match((msg.text or "").strip())
    if not match:
        await msg.reply_text("Invalid format. Use: BUY BTCUSD 1")
        return

    side_raw, symbol_raw, qty_raw = match.groups()
    side = "buy" if side_raw.upper() == "BUY" else "sell"
    symbol = symbol_raw.upper()
    base_qty = int(qty_raw)

    if base_qty <= 0 or base_qty > MAX_QTY:
        await msg.reply_text(f"Quantity must be between 1 and {MAX_QTY}.")
        return

    product_id = PRODUCT_CACHE.get(symbol)
    if not product_id:
        await msg.reply_text(f"Unknown or inactive futures symbol: {symbol}\n(Try /reload)")
        return

    async def run(acct: dict) -> str:
        qty = base_qty * acct["multiplier"]
        if qty <= 0:
            return f"⏭ {acct['name']}: skipped (qty 0)"
        if DRY_RUN:
            return f"🧪 {acct['name']}: DRY RUN {side.upper()} {qty} {symbol}"
        try:
            # requests is blocking, so run it in a thread to keep the bot responsive
            order = await asyncio.to_thread(
                place_delta_order, acct["key"], acct["secret"], product_id, side, qty
            )
            return f"✅ {acct['name']}: {side.upper()} {qty} {symbol} (Order ID: {order.get('id')})"
        except Exception as exc:
            log.warning("Order failed for %s: %s", acct["name"], exc)
            return f"❌ {acct['name']}: Failed ({exc})"

    results = await asyncio.gather(*(run(a) for a in ACCOUNTS))
    await msg.reply_text("\n".join(results))


if __name__ == "__main__":
    load_products()
    log.info("Loaded %d live futures contracts. Accounts: %d. DRY_RUN=%s",
             len(PRODUCT_CACHE), len(ACCOUNTS), DRY_RUN)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_trade_signal))
    app.run_polling()
