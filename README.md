# Alpha Oracle Pro

Cryptocurrency perpetual swap signal bot with a self-improving learning system.

## Features

- Multi-timeframe technical analysis: 15-minute plus 1-hour and 4-hour confluence
- Smart Money Concepts: Order Blocks, FVG, Liquidity Sweeps
- Volume confirmation and ADX-based market regime detection
- Dynamic stop loss and take profit with break-even and trailing logic
- KNN-based learning from historical trades
- Multi-source price verification across two exchanges
- Automatic post-mortem analysis on every loss
- Daily and monthly performance reports

## Stack

- Python 3.11
- Runs on GitHub Actions on a cron schedule
- Optional persistent WebSocket monitor for real-time TP and SL detection
- Telegram bot for notifications

## Setup

1. Fork or clone this repository
2. Set the required GitHub Secrets:
   - `TG_TOKEN` for the Telegram bot token
   - `CHAT_ID` for the Telegram chat ID
3. Push to enable the GitHub Actions workflows
4. Optional: customise `config.json` for thresholds, coin list, blackout windows

## Usage

Run via the bundled entrypoint:

- `python -m main` runs the main signal scan
- `python -m main monitor` runs the lightweight monitor mode
- `python -m main daily` sends the daily report
- `python -m main monthly` sends the monthly report
- `python -m main /learning` shows what the bot has learned

The standalone backtest tool is invoked as a script in the repo root.

## Workflows

| File | Purpose | Schedule |
|---|---|---|
| `alpha_oracle.yml` | Full scan and signal generation | Every 15 minutes |
| `alpha_oracle_monitor.yml` | Lightweight TP and SL monitor | Every 3 minutes |
| `alpha_oracle_daily.yml` | Daily and monthly reports | Daily at 00:00 Asia/Taipei |

## License

For personal use only. Not financial advice. Use at your own risk.
