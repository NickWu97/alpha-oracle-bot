#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro - WebSocket 即時監控
══════════════════════════════════════════════════════════════════════
功能：
  - 即時價格監控
  - 快速觸發止盈止損
  - 減少 API 延遲
  - 補充 GitHub Actions 掃描間隔
"""
import json
import time
import threading
import websocket
import logging
from datetime import datetime
from typing import Dict, List, Optional


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s"
)


class WebSocketMonitor:
    """WebSocket 監控器"""
    
    def __init__(self, config: dict):
        self.config = config
        self.coins = config.get("coins", [])
        self.ws_connections: Dict[str, websocket.WebSocketApp] = {}
        self.active_signals = self._load_active_signals()
        self.running = False
        
    def _load_active_signals(self) -> dict:
        """載入活躍訊號"""
        try:
            with open("active_signals.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    
    def connect_to_okx(self, instId: str):
        """連接到 OKX WebSocket"""
        channel = f"tickers/{instId}"
        ws_url = "wss://ws.okx.com:8443/ws/v5/public"
        
        def on_message(ws, message):
            self._handle_message(instId, message)
        
        def on_error(ws, error):
            logging.error(f"❌ {instId} WebSocket 錯誤：{error}")
        
        def on_close(ws, close_status_code, close_msg):
            logging.info(f"🔌 {instId} WebSocket 已關閉")
            if self.running:
                time.sleep(5)
                self.connect_to_okx(instId)  # 自動重連
        
        def on_open(ws):
            logging.info(f"🔗 {instId} WebSocket 已連接")
            # 訂閱頻道
            subscribe_msg = {
                "op": "subscribe",
                "args": [
                    {
                        "channel": channel,
                        "instId": instId
                    }
                ]
            }
            ws.send(json.dumps(subscribe_msg))
        
        ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        
        self.ws_connections[instId] = ws
        
        # 在後台執行
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()
    
    def _handle_message(self, instId: str, message: str):
        """處理 WebSocket 訊息"""
        try:
            data = json.loads(message)
            
            if data.get("event") == "subscribe":
                logging.info(f"✅ {instId} 訂閱成功")
                return
            
            if data.get("arg", {}).get("channel") == f"tickers/{instId}":
                for ticker in data.get("data", []):
                    last_price = float(ticker.get("last", 0))
                    if last_price > 0:
                        self._check_signals(instId, last_price)
                        
        except Exception as e:
            logging.error(f"⚠️ 處理訊息失敗：{e}")
    
    def _check_signals(self, instId: str, current_price: float):
        """檢查是否觸發訊號"""
        for key, sig in self.active_signals.items():
            if sig.get("instId") != instId:
                continue
            
            if sig.get("status") not in ("PENDING", "ACTIVE", "BE", "TRAIL"):
                continue
            
            # 檢查 TP/SL
            side = sig.get("side")
            entry = sig.get("entry")
            sl = sig.get("sl")
            tp1 = sig.get("tp1")
            tp2 = sig.get("tp2")
            tp3 = sig.get("tp3")
            
            # 檢查是否觸發
            triggered = None
            
            if side == "LONG":
                if current_price >= tp3 and not sig.get("hit_tp3"):
                    triggered = "TP3"
                elif current_price >= tp2 and not sig.get("hit_tp2"):
                    triggered = "TP2"
                elif current_price >= tp1 and not sig.get("hit_tp1"):
                    triggered = "TP1"
                elif current_price <= sl:
                    triggered = "SL"
            else:  # SHORT
                if current_price <= tp3 and not sig.get("hit_tp3"):
                    triggered = "TP3"
                elif current_price <= tp2 and not sig.get("hit_tp2"):
                    triggered = "TP2"
                elif current_price <= tp1 and not sig.get("hit_tp1"):
                    triggered = "TP1"
                elif current_price >= sl:
                    triggered = "SL"
            
            if triggered:
                logging.info(
                    f"🎯 {instId} {triggered} 觸發！"
                    f" 價格：{current_price:.4f}"
                )
                # 這裡可以發送 Telegram 通知或觸發其他動作
    
    def start(self):
        """啟動監控"""
        logging.info("=" * 50)
        logging.info("📡 WebSocket 監控啟動")
        logging.info(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info("=" * 50)
        
        self.running = True
        
        # 連接所有幣種
        for instId in self.coins:
            time.sleep(0.5)  # 避免連接太快
            self.connect_to_okx(instId)
        
        # 保持運行
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("🛑 收到停止訊號，關閉監控...")
            self.stop()
    
    def stop(self):
        """停止監控"""
        self.running = False
        for instId, ws in self.ws_connections.items():
            ws.close()
        logging.info("✅ 所有 WebSocket 連接已關閉")


def load_config() -> dict:
    """載入配置"""
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"coins": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]}


if __name__ == "__main__":
    config = load_config()
    monitor = WebSocketMonitor(config)
    monitor.start()
