#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSocket Real-Time Monitor — v15.5
══════════════════════════════════════════════════════════════════════
🚀 真正即時的 SL/TP 監控（< 1 秒延遲）

在本機 Mac / VPS 跑這支腳本，會：
1. 訂閱所有活躍部位的 OKX WebSocket tickers
2. 每收到一個 tick（每秒可能多次），立即檢查 SL/TP
3. 一觸到就立刻發 TG + 寫入 trade_history.json
4. 與 GitHub Actions 上的 main.py 共用同一份 active_signals.json

🆚 跟 GitHub Actions 的差別：
- main.py 走 cron + REST，最快 1 分鐘輪詢一次，但跑得久不會睡
- 這支 ws monitor 走 WebSocket，秒級延遲，但需要一台機器持續開機

⚙️ 用法：
    pip install websockets requests
    export TG_TOKEN=xxx CHAT_ID=xxx
    python3 websocket_monitor.py

🛑 中斷後重啟會自動同步 active_signals.json 最新狀態
🛑 OKX WebSocket 斷線會自動重連（指數 backoff）
══════════════════════════════════════════════════════════════════════
"""
import os
import sys
import json
import time
import asyncio
import logging
from typing import Set

try:
    import websockets
except ImportError:
    sys.stderr.write("❌ 需要安裝 websockets: pip install websockets\n")
    sys.exit(1)

# 重用 main.py 的函式 / 類別
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import (  # noqa: E402
    SignalTracker, SetupTracker,
    ACTIVE_SIGNALS_FILE, SETUPS_FILE,
    fetch_price, load_config,
    _fmt_final_close, _fmt_tp_milestone, _fmt_entry,
    _order_keyboard, send_tg,
    calc_realized_r, calc_realized_usd, record_trade_final,
    DEFAULT_CONFIG,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WS] %(message)s",
    stream=sys.stdout,
)

OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
RECONNECT_DELAY_BASE = 2  # 重連 backoff 起點（秒）
RECONNECT_DELAY_MAX = 60
RESYNC_INTERVAL = 30  # 每 30 秒重讀 active_signals.json


# ═════════════════════════════════════════════════════════
# Tick 處理（核心邏輯）
# ═════════════════════════════════════════════════════════
class TickProcessor:
    """⚡ 每個 tick 都做：純 price vs SL/TP 比較，秒級觸發"""

    def __init__(self):
        self.tracker = SignalTracker(ACTIVE_SIGNALS_FILE)
        self.setup_tracker = SetupTracker(SETUPS_FILE)
        self.last_resync = 0
        self.cfg = load_config()
        # 紀錄已處理 tick 的時間戳（避免同一 tick 重複處理）
        self.last_tick_ts: dict = {}

    def get_subscribed_instids(self) -> Set[str]:
        """所有需要訂閱 WS 的 instId（含 PENDING/ACTIVE 持倉 + setup）"""
        ids = set()
        for s in self.tracker.signals.values():
            if s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL"):
                ids.add(s["instId"])
        for s in self.setup_tracker.setups.values():
            if s.get("stage") in ("setup", "approach"):
                ids.add(s["instId"])
        return ids

    def resync_if_needed(self) -> bool:
        """每 30 秒重讀檔案，回傳是否需要重新訂閱"""
        now = time.time()
        if now - self.last_resync < RESYNC_INTERVAL:
            return False
        old_ids = self.get_subscribed_instids()
        self.tracker.reload()
        self.setup_tracker.setups = self.setup_tracker.setups  # 沒辦法 reload，下次 init 就會抓
        # 重新建立 setup_tracker
        self.setup_tracker = SetupTracker(SETUPS_FILE)
        self.last_resync = now
        new_ids = self.get_subscribed_instids()
        return old_ids != new_ids

    def process_tick(self, instId: str, price: float) -> None:
        """收到 tick 立刻 check 所有相關訊號"""
        if price <= 0:
            return

        # 處理 ACTIVE/BE/TRAIL 持倉的 SL/TP
        to_remove = []
        for key, sig in list(self.tracker.signals.items()):
            if sig.get("instId") != instId:
                continue
            status = sig.get("status")

            if status in ("ACTIVE", "BE", "TRAIL"):
                # 構造 synthetic candle 用 _process_candle
                synth = {
                    "ts": int(time.time() * 1000),
                    "o": price, "h": price, "l": price, "c": price,
                    "v": 0, "confirmed": False,
                }
                try:
                    if self.tracker._process_candle(sig, synth):
                        to_remove.append(key)
                        logging.info(f"⚡ {instId} {sig['side']} 出場觸發 @ {price}")
                except Exception as e:
                    logging.error(f"❌ tick process [{key}]: {e}")

            elif status == "PENDING":
                # 進場區 check
                side = sig["side"]
                entry = sig["entry"]
                ez = self.cfg.get("entry_zone_pct", DEFAULT_CONFIG["entry_zone_pct"])
                if side == "LONG":
                    low = entry * (1 - ez.get("long_favor", 0.006))
                    high = entry * (1 + ez.get("long_against", 0.002))
                else:
                    low = entry * (1 - ez.get("short_against", 0.002))
                    high = entry * (1 + ez.get("short_favor", 0.006))
                if low <= price <= high:
                    now_ts = time.time()
                    sig["status"] = "ACTIVE"
                    sig["activated_at"] = now_ts
                    sig["last_checked_ts"] = now_ts
                    msg_id = send_tg(
                        _fmt_entry(sig, price),
                        reply_markup=_order_keyboard(sig["order_id"]),
                    )
                    if msg_id:
                        sig["entry_message_id"] = msg_id
                    self.tracker._save()
                    logging.info(f"⚡ {instId} {sig['side']} PENDING → ACTIVE @ {price}")

        for key in to_remove:
            del self.tracker.signals[key]
        if to_remove:
            self.tracker._save()


# ═════════════════════════════════════════════════════════
# WebSocket 主迴圈
# ═════════════════════════════════════════════════════════
async def subscribe_tickers(ws, instIds: Set[str]) -> None:
    if not instIds:
        return
    args = [{"channel": "tickers", "instId": iid} for iid in instIds]
    await ws.send(json.dumps({"op": "subscribe", "args": args}))
    logging.info(f"📡 訂閱 {len(instIds)} 個 tickers: {sorted(instIds)}")


async def unsubscribe_tickers(ws, instIds: Set[str]) -> None:
    if not instIds:
        return
    args = [{"channel": "tickers", "instId": iid} for iid in instIds]
    await ws.send(json.dumps({"op": "unsubscribe", "args": args}))


async def monitor_loop():
    processor = TickProcessor()
    backoff = RECONNECT_DELAY_BASE

    while True:
        try:
            current_ids = processor.get_subscribed_instids()
            if not current_ids:
                logging.info("📭 無活躍部位/setup，10s 後重新檢查...")
                await asyncio.sleep(10)
                processor.resync_if_needed()
                continue

            logging.info(f"🔌 連線 OKX WebSocket ({len(current_ids)} 個 ticker)...")
            async with websockets.connect(
                OKX_WS_URL, ping_interval=20, ping_timeout=10, close_timeout=5,
            ) as ws:
                await subscribe_tickers(ws, current_ids)
                backoff = RECONNECT_DELAY_BASE  # 重置 backoff

                async def periodic_resync():
                    """每 30 秒重讀 active_signals.json 看有沒有新 instId 要訂"""
                    nonlocal current_ids
                    while True:
                        await asyncio.sleep(RESYNC_INTERVAL)
                        try:
                            processor.resync_if_needed()
                            new_ids = processor.get_subscribed_instids()
                            added = new_ids - current_ids
                            removed = current_ids - new_ids
                            if added:
                                await subscribe_tickers(ws, added)
                            if removed:
                                await unsubscribe_tickers(ws, removed)
                                logging.info(f"📭 取消訂閱 {sorted(removed)}")
                            current_ids = new_ids
                        except Exception as e:
                            logging.error(f"❌ resync: {e}")

                resync_task = asyncio.create_task(periodic_resync())

                try:
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if data.get("event") == "subscribe":
                            continue
                        if data.get("event") == "error":
                            logging.error(f"❌ WS error: {data}")
                            continue

                        ticks = data.get("data") or []
                        for tick in ticks:
                            instId = tick.get("instId")
                            try:
                                price = float(tick.get("last", 0))
                            except (TypeError, ValueError):
                                continue
                            if not instId or price <= 0:
                                continue
                            processor.process_tick(instId, price)
                finally:
                    resync_task.cancel()
                    try:
                        await resync_task
                    except asyncio.CancelledError:
                        pass

        except (websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException) as e:
            logging.warning(f"🔌 WS 斷線：{e}，{backoff}s 後重連...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_DELAY_MAX)
        except Exception as e:
            logging.error(f"🔥 未預期錯誤：{e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_DELAY_MAX)


def main():
    if not os.getenv("TG_TOKEN") or not os.getenv("CHAT_ID"):
        sys.stderr.write("❌ TG_TOKEN 或 CHAT_ID 環境變數未設定\n")
        sys.exit(1)

    logging.info("=" * 50)
    logging.info("🚀 Alpha Oracle WebSocket Monitor v15.5")
    logging.info("=" * 50)
    logging.info("此腳本持續監控 OKX tick，秒級觸發 SL/TP")
    logging.info("Ctrl+C 中斷")
    logging.info("=" * 50)

    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        logging.info("👋 手動中斷")
    except Exception as e:
        logging.error(f"🔥 致命錯誤：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
