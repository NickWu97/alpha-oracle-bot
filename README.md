# Alpha Oracle Pro

Cryptocurrency perpetual swap signal bot with self-improving learning system.

## Features

- Multi-timeframe technical analysis (15m + 1H + 4H confluence)
- Smart Money Concepts (Order Blocks, FVG, Liquidity Sweeps)
- Volume confirmation, EMA alignment
- Fixed R:R (1.5R / 3R / 5R) with break-even and trailing stops
- KNN-based learning from historical trades
- Multi-source price verification (OKX + TradingView)
- Automatic post-mortem analysis on every loss
- Daily / monthly performance reports
- Capital management with auto-leverage calculation

## Stack

- Python 3.11
- Runs on GitHub Actions (1-min cron)
- Telegram bot for notifications

## Setup

1. Fork or clone this repository
2. Set GitHub Secrets:
   - `TG_TOKEN` — Telegram bot token
   - `CHAT_ID` — Telegram chat ID
3. Push to enable GitHub Actions workflows
4. Optional: customize `config.json`

## Commands

- `python main.py` — main scan
- `python main.py monitor` — lightweight monitor
- `python main.py daily [YYYY-MM-DD]` — daily report
- `python main.py monthly [YYYY-MM]` — monthly report
- `python main.py /learning` — learning state
- `python main.py /audit` — indicator effectiveness audit
- `python main.py /stats` — current positions

## License

For personal use only. Not financial advice.
