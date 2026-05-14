#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle WebSocket 即時監控（常駐服務）
══════════════════════════════════════════════════════════════════════
為什麼要這個檔案：
  GitHub Actions cron 是「短命進程」，無法保持 WebSocket 長連線。
  把這個檔案部署到 Railway / Fly.io / Render / VPS 等可持久執行的平台，
  就能做到「秒級」TP/SL 偵測（不再有 cron 延遲）。

部署選項：
  ─ Railway：新增 Service → 連結 GitHub repo → 設定 Start Command:
        python websocket_monitor.py
  ─ Fly.io：fly launch → fly deploy
  ─ Render：Web Service → Start: python websocket_monitor.py
  ─ VPS：nohup python websocket_monitor.py > ws.log 2>&1 &

需要的環境變數：
  TG_TOKEN, CHAT_ID

需要的依賴：
  pip install websockets requests tradingview-ta

注意：
  - 這個服務跟 GitHub Actions 共用同一份 active_signals.json
  - 為了避免衝突，建議只讓「其中一邊」做 TP/SL 觸發（這個檔做即時偵測）
  - 訊號生成（generate_signal）仍交給 GitHub Actions 每 15 分跑一次
══════════════════════════════════════════════════════════════════════
"""
import asyncio
import json
import os
import sys
import time
import logging

try:
    import websockets
except ImportError:
    print("請先安裝：pip install websockets")
    sys.exit(1)

# 重用 main.py 的 SignalTracker / 通知 / 訊號處理邏輯
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    stream=sys.stdout,
)

OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


async def subscribe_tickers(coins: list):
    """訂閱 OKX 即時 ticker，價格變動即觸發訊號檢查"""
    while True:
        try:
            async with websockets.connect(OKX_WS_URL, ping_interval=20) as ws:
                args = [{"channel": "tickers", "instId": c} for c in coins]
                sub_msg = {"op": "subscribe", "args": args}
                await ws.send(json.dumps(sub_msg))
                logging.info(f"✅ 訂閱 {len(coins)} 個幣種的 OKX 即時 ticker")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("event") == "subscribe":
                        continue
                    data = msg.get("data") or []
                    for tick in data:
                        instId = tick.get("instId")
                        last = float(tick.get("last", 0))
                        if instId and last > 0:
                            await on_price_update(instId, last)
        except Exception as e:
            logging.error(f"❌ WebSocket 連線中斷：{e}，10 秒後重連")
            await asyncio.sleep(10)


# 每個 instId 的最後處理時間（避免每 tick 都重打 OKX K 線 API）
_last_process: dict = {}


async def on_price_update(instId: str, price: float):
    """每收到一筆即時價就檢查相關訊號"""
    # 把即時價推進 main 的快取（讓 _check_one 拿到最新價）
    bot._price_cache[instId] = (price, time.time())

    # 限頻：同一個 instId 每 5 秒最多處理一次
    now = time.time()
    if now - _last_process.get(instId, 0) < 5:
        return
    _last_process[instId] = now

    # 開新 tracker（每次重新讀 active_signals 檔）
    tracker = bot.SignalTracker(bot.ACTIVE_SIGNALS_FILE)
    related = [k for k, s in tracker.signals.items() if s.get("instId") == instId]
    if not related:
        return

    # 只處理該 instId 的訊號
    to_remove = []
    for key in related:
        sig = tracker.signals.get(key)
        if not sig:
            continue
        try:
            if tracker._check_one(key, sig):
                to_remove.append(key)
        except Exception as e:
            logging.error(f"❌ check_one 錯誤 [{key}]：{e}")
    for key in to_remove:
        tracker.signals.pop(key, None)
    if to_remove:
        tracker._save()
        logging.info(f"⚡ {instId} 訊號平倉：{len(to_remove)} 筆")


async def main_loop():
    cfg = bot.load_config()
    coins = cfg.get("coins", bot.ALL_COINS)
    logging.info(f"🔔 WebSocket 監控啟動，幣種：{', '.join(coins)}")
    await subscribe_tickers(coins)


if __name__ == "__main__":
    if not bot.TG_TOKEN or not bot.CHAT_ID:
        print("⚠️ 環境變數 TG_TOKEN / CHAT_ID 未設定，通知將無法送出")
    asyncio.run(main_loop())
