# Delta Telegram Copier
Send `BUY BTCUSD 1` / `SELL ETHUSD 2` in your Telegram chat; the bot places market orders on Delta Exchange for the master and any slave accounts (scaled by multiplier).

    pip install -r requirements.txt
    cp .env.example .env   # fill in values, keep DRY_RUN=true first
    python delta_tg_copier.py

Commands: `/start` (status), `/reload` (refresh contract list).
Never commit `.env`.
