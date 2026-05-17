"""
Alpha Oracle Pro — WebSocket Real-Time Monitor  v1.0
====================================================
True millisecond-level TP / SL detection via OKX public WebSocket.
• Connects to wss://ws.okx.com:8443/ws/v5/public
• Subscribes to tickers for every symbol in active_signals.json
• Checks TP1→TP2→TP3 sequentially (natural ordering via state flags)
• Sends Telegram notifications with real timestamp differences
• Auto-reconnects on disconnect with exponential back-off
• Re-reads active_signals.json every 60 s so new signals are picked up
  without restarting the process

Run:
    python3 websocket_monitor.py

Environment / config (reads monitor_config.json or env vars):
    TG_TOKEN   – Telegram bot token
    CHAT_ID    – Telegram chat / channel ID
    SIGNALS_FILE – path to active_signals.json  (default: ./active_signals.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("websocket_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ws_monitor")

# ─────────────────────────────────────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_FILE = Path("monitor_config.json")
SIGNALS_FILE_DEFAULT = Path("active_signals.json")

def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except Exception as e:
            log.warning("monitor_config.json parse error: %s", e)
    # env vars override file
    for key in ("TG_TOKEN", "CHAT_ID", "SIGNALS_FILE"):
        val = os.environ.get(key)
        if val:
            cfg[key] = val
    return cfg

# ─────────────────────────────────────────────────────────────────────────────
# OKX WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
PING_INTERVAL = 20          # seconds  (OKX requires ping every 30 s)
RECONNECT_BASE = 3          # seconds  base back-off
RECONNECT_MAX = 60          # seconds  cap
SIGNALS_REFRESH = 60        # seconds  re-read signals file
TP_NOTIFY_DELAY = 1.5       # seconds  pause between consecutive TP notifications

# ─────────────────────────────────────────────────────────────────────────────
# Telegram sender (async)
# ─────────────────────────────────────────────────────────────────────────────
async def send_telegram(session: aiohttp.ClientSession, token: str,
                        chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("Telegram error %s: %s", resp.status, body[:200])
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Signal state manager
# ─────────────────────────────────────────────────────────────────────────────
class SignalState:
    """
    Wraps one active signal dict with helpers for price checking.
    `hit_tp1 / hit_tp2 / hit_tp3` mirror the keys in active_signals.json
    so the file stays the source of truth; we refresh every SIGNALS_REFRESH s.
    """

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.id: str = raw.get("id", raw.get("symbol", "UNKNOWN"))
        self.symbol: str = raw["symbol"]          # e.g. "BTC-USDT-SWAP"
        self.direction: str = raw.get("direction", "long").lower()
        self.entry: float = float(raw["entry"])
        self.sl: float = float(raw["sl"])
        self.tp1: float = float(raw["tp1"])
        self.tp2: float = float(raw.get("tp2", raw["tp1"]))
        self.tp3: float = float(raw.get("tp3", raw["tp1"]))
        self.hit_tp1: bool = bool(raw.get("hit_tp1", False))
        self.hit_tp2: bool = bool(raw.get("hit_tp2", False))
        self.hit_tp3: bool = bool(raw.get("hit_tp3", False))
        self.closed: bool = bool(raw.get("closed", False))
        # lock so only one notification fires at a time for this signal
        self.lock = asyncio.Lock()

    def is_long(self) -> bool:
        return self.direction == "long"

    def _favor_hit(self, price: float, level: float) -> bool:
        """Returns True if price has reached the target level in the right direction."""
        if self.is_long():
            return price >= level
        return price <= level

    def _against_hit(self, price: float, level: float) -> bool:
        if self.is_long():
            return price <= level
        return price >= level

    def next_event(self, price: float) -> str | None:
        """
        Returns the *first* pending event triggered by `price`, or None.
        Caller must fire notification, then set the corresponding hit_ flag
        before calling again (sequential guarantee).
        """
        if self.closed:
            return None
        if not self.hit_tp1 and self._favor_hit(price, self.tp1):
            return "tp1"
        if self.hit_tp1 and not self.hit_tp2 and self._favor_hit(price, self.tp2):
            return "tp2"
        if self.hit_tp2 and not self.hit_tp3 and self._favor_hit(price, self.tp3):
            return "tp3"
        # SL only if no TP pending
        if not self.hit_tp1 and self._against_hit(price, self.sl):
            return "sl"
        if self.hit_tp1 and not self.hit_tp3 and self._against_hit(price, self.sl):
            return "sl_partial"      # already hit TP1, now SL
        return None

    def proximity_alert(self, price: float, threshold: float = 0.003) -> str | None:
        """Return label if price is within `threshold` of next key level."""
        def near(level: float) -> bool:
            return abs(price - level) / level <= threshold

        if not self.hit_tp1 and near(self.tp1):
            return "TP1 接近"
        if self.hit_tp1 and not self.hit_tp2 and near(self.tp2):
            return "TP2 接近"
        if self.hit_tp2 and not self.hit_tp3 and near(self.tp3):
            return "TP3 接近"
        if near(self.sl):
            return "SL 接近"
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Notification formatters
# ─────────────────────────────────────────────────────────────────────────────
EMOJI = {
    "tp1": "🎯",
    "tp2": "🎯🎯",
    "tp3": "🏆",
    "sl": "🛑",
    "sl_partial": "🔒",
}

def _pnl_pct(entry: float, exit_: float, is_long: bool) -> float:
    if is_long:
        return (exit_ - entry) / entry * 100
    return (entry - exit_) / entry * 100

def fmt_tp_message(sig: SignalState, event: str, price: float) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    emoji = EMOJI.get(event, "📌")
    direction_zh = "做多 📈" if sig.is_long() else "做空 📉"
    pnl = _pnl_pct(sig.entry, price, sig.is_long())
    pnl_str = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"

    labels = {
        "tp1": "TP1 達成",
        "tp2": "TP2 達成",
        "tp3": "TP3 達成 — 全部出場！",
        "sl": "止損觸發",
        "sl_partial": "部分持倉止損",
    }
    label = labels.get(event, event.upper())

    lines = [
        f"{emoji} <b>{sig.symbol} — {label}</b>",
        f"方向: {direction_zh}",
        f"進場: <code>{sig.entry}</code>",
        f"觸發價: <code>{price}</code>",
        f"損益: <b>{pnl_str}</b>",
        f"時間: {ts}",
    ]
    if event == "tp1":
        lines.append(f"⏳ 等待 TP2 @ <code>{sig.tp2}</code>")
    elif event == "tp2":
        lines.append(f"⏳ 等待 TP3 @ <code>{sig.tp3}</code>")
    elif event in ("sl", "sl_partial"):
        lines.append("❌ 信號已關閉")
    return "\n".join(lines)

def fmt_proximity_message(sig: SignalState, label: str, price: float) -> str:
    ts = time.strftime("%H:%M:%S")
    return (
        f"⚠️ <b>{sig.symbol} — {label}</b>\n"
        f"現價: <code>{price}</code> | 時間: {ts}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Signals file I/O (thread-safe via asyncio lock)
# ─────────────────────────────────────────────────────────────────────────────
_file_lock = asyncio.Lock()

async def load_signals(path: Path) -> list[dict]:
    async with _file_lock:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.warning("Failed to read signals file: %s", e)
            return []

async def save_signals(path: Path, signals: list[dict]) -> None:
    async with _file_lock:
        try:
            path.write_text(json.dumps(signals, indent=2, ensure_ascii=False))
        except Exception as e:
            log.warning("Failed to write signals file: %s", e)

def sync_state_to_raw(sig: SignalState) -> None:
    """Copy hit_ flags back to raw dict so they persist on save."""
    sig.raw["hit_tp1"] = sig.hit_tp1
    sig.raw["hit_tp2"] = sig.hit_tp2
    sig.raw["hit_tp3"] = sig.hit_tp3
    sig.raw["closed"] = sig.closed


# ─────────────────────────────────────────────────────────────────────────────
# Core monitor
# ─────────────────────────────────────────────────────────────────────────────
class WebSocketMonitor:
    def __init__(self, cfg: dict) -> None:
        self.token: str = cfg.get("TG_TOKEN", "")
        self.chat_id: str = str(cfg.get("CHAT_ID", ""))
        self.signals_path = Path(cfg.get("SIGNALS_FILE", SIGNALS_FILE_DEFAULT))

        if not self.token or not self.chat_id:
            log.error("TG_TOKEN / CHAT_ID missing — check monitor_config.json or env vars")
            sys.exit(1)

        # symbol → SignalState (active, non-closed)
        self.states: dict[str, list[SignalState]] = {}
        # raw list from disk
        self.raw_signals: list[dict] = []
        # proximity alert cooldown: (symbol, label) → last-sent epoch
        self._prox_cooldown: dict[tuple, float] = {}
        # aiohttp session (created in run())
        self._http: aiohttp.ClientSession | None = None

    # ── signal management ────────────────────────────────────────────────────

    async def refresh_signals(self) -> set[str]:
        """
        Re-read disk, update self.states.
        Returns set of instId strings that should be subscribed.
        """
        raw_list = await load_signals(self.signals_path)
        self.raw_signals = raw_list

        new_states: dict[str, list[SignalState]] = {}
        for raw in raw_list:
            if raw.get("closed"):
                continue
            sym = raw.get("symbol", "")
            if not sym:
                continue
            st = SignalState(raw)
            new_states.setdefault(sym, []).append(st)

        self.states = new_states
        log.info("Loaded %d active signal(s) across %d symbol(s)",
                 sum(len(v) for v in self.states.values()), len(self.states))
        return set(self.states.keys())

    async def _persist(self) -> None:
        """Write current hit_ flag state back to disk."""
        for sigs in self.states.values():
            for sig in sigs:
                sync_state_to_raw(sig)
        await save_signals(self.signals_path, self.raw_signals)

    # ── notification ─────────────────────────────────────────────────────────

    async def _notify(self, text: str) -> None:
        if self._http:
            await send_telegram(self._http, self.token, self.chat_id, text)

    # ── price event handler ──────────────────────────────────────────────────

    async def handle_price(self, symbol: str, price: float) -> None:
        sigs = self.states.get(symbol, [])
        for sig in sigs:
            if sig.closed:
                continue
            async with sig.lock:
                # ── 每個 price tick 只觸發「下一個尚未命中」的單一事件 ──
                # 原則：TP1 觸發後不繼續檢查 TP2；TP2 必須等下一個真實價格 tick。
                # 時間差由市場決定（真實不同時間），而非人工 sleep。
                event = sig.next_event(price)
                if event is None:
                    pass   # no action this tick
                else:
                    msg = fmt_tp_message(sig, event, price)
                    log.info("[%s] %s @ %.4f — %s", symbol, event.upper(), price, sig.id)
                    await self._notify(msg)

                    # Update state flags
                    if event == "tp1":
                        sig.hit_tp1 = True
                    elif event == "tp2":
                        sig.hit_tp2 = True
                    elif event == "tp3":
                        sig.hit_tp3 = True
                        sig.closed = True
                    elif event in ("sl", "sl_partial"):
                        sig.closed = True

                    await self._persist()

                # Proximity alert (outside the TP loop, debounced)
                if not sig.closed:
                    prox = sig.proximity_alert(price)
                    if prox:
                        key = (symbol, prox)
                        now = time.time()
                        if now - self._prox_cooldown.get(key, 0) > 120:  # 2-min cooldown
                            self._prox_cooldown[key] = now
                            await self._notify(fmt_proximity_message(sig, prox, price))

    # ── WebSocket loop ───────────────────────────────────────────────────────

    def _build_subscribe_msg(self, symbols: set[str]) -> dict:
        args = [{"channel": "tickers", "instId": sym} for sym in sorted(symbols)]
        return {"op": "subscribe", "args": args}

    def _build_unsubscribe_msg(self, symbols: set[str]) -> dict:
        args = [{"channel": "tickers", "instId": sym} for sym in sorted(symbols)]
        return {"op": "unsubscribe", "args": args}

    async def _ws_loop(self) -> None:
        """Single WebSocket session. Raises on disconnect (caller reconnects)."""
        symbols = await self.refresh_signals()
        if not symbols:
            log.info("No active signals — sleeping 30 s before retry")
            await asyncio.sleep(30)
            return

        sub_msg = json.dumps(self._build_subscribe_msg(symbols))
        last_refresh = time.time()
        last_ping = time.time()

        async with websockets.connect(
            OKX_WS_PUBLIC,
            ping_interval=None,     # we handle pings manually
            max_size=2**20,
        ) as ws:
            log.info("Connected to OKX WebSocket. Subscribing to %d symbol(s)…", len(symbols))
            await ws.send(sub_msg)

            async for raw in ws:
                now = time.time()

                # Manual OKX ping
                if now - last_ping >= PING_INTERVAL:
                    try:
                        await ws.send("ping")
                    except Exception:
                        pass
                    last_ping = now

                # Ignore pong / plain text
                if isinstance(raw, str) and not raw.startswith("{"):
                    continue

                try:
                    msg: dict = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Handle subscription ack / errors
                if "event" in msg:
                    if msg.get("event") == "error":
                        log.warning("WS event error: %s", msg)
                    continue

                # Price tick
                if msg.get("arg", {}).get("channel") == "tickers":
                    data_list: list[dict] = msg.get("data", [])
                    for tick in data_list:
                        inst_id: str = tick.get("instId", "")
                        last_price_str: str = tick.get("last", "")
                        if not inst_id or not last_price_str:
                            continue
                        try:
                            price = float(last_price_str)
                        except ValueError:
                            continue
                        await self.handle_price(inst_id, price)

                # Periodic signal refresh
                if now - last_refresh >= SIGNALS_REFRESH:
                    new_symbols = await self.refresh_signals()
                    added = new_symbols - symbols
                    removed = symbols - new_symbols
                    if added:
                        log.info("Subscribing new symbols: %s", added)
                        await ws.send(json.dumps(self._build_subscribe_msg(added)))
                    if removed:
                        log.info("Unsubscribing closed symbols: %s", removed)
                        await ws.send(json.dumps(self._build_unsubscribe_msg(removed)))
                    symbols = new_symbols
                    last_refresh = now

    # ── public entry point ───────────────────────────────────────────────────

    async def run(self) -> None:
        backoff = RECONNECT_BASE
        async with aiohttp.ClientSession() as http:
            self._http = http
            log.info("Alpha Oracle WebSocket Monitor starting…")
            while True:
                try:
                    await self._ws_loop()
                    backoff = RECONNECT_BASE   # reset on clean exit
                except (ConnectionClosedError, ConnectionClosedOK) as e:
                    log.warning("WS disconnected: %s — reconnecting in %ds", e, backoff)
                except Exception as e:
                    log.error("WS error: %s — reconnecting in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    cfg = load_config()
    monitor = WebSocketMonitor(cfg)
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        log.info("Monitor stopped by user.")


if __name__ == "__main__":
    main()
