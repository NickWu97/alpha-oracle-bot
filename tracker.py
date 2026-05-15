# tracker.py
import json
import logging
import time
from typing import Dict, List, Optional
from database import db
from notifier import send_tg_async
from indicators import calc_atr
from risk_manager import risk_mgr
from data_fetcher import AsyncDataFetcher

class TrackerV16:
    def __init__(self, fetcher: AsyncDataFetcher):
        self.fetcher = fetcher
        self.active_signals: Dict[str, Dict] = {}  # order_id -> signal dict
        self._load_active()
    
    def _load_active(self):
        rows = db.get_active_signals()
        for row in rows:
            order_id = row["order_id"]
            sig = json.loads(row["signal_json"])
            sig["status"] = row["status"]
            sig["activated_at"] = row.get("activated_at")
            sig["last_checked_ts"] = row.get("last_checked_ts")
            # 額外狀態
            sig.setdefault("hit_tp1", False)
            sig.setdefault("hit_tp2", False)
            sig.setdefault("hit_tp3", False)
            sig.setdefault("highest_price", sig["entry"])
            sig.setdefault("lowest_price", sig["entry"])
            self.active_signals[order_id] = sig
    
    async def check_all(self):
        """檢查所有活躍訊號的止盈止損及移動止損"""
        for order_id, sig in list(self.active_signals.items()):
            current_price = await self.fetcher.fetch_price(sig["instId"], "okx")
            if not current_price:
                continue
            sig["current_price"] = current_price
            
            # 處理 PENDING 狀態
            if sig["status"] == "PENDING":
                await self._check_pending(order_id, sig, current_price)
                continue
            
            if sig["status"] not in ("ACTIVE", "BE", "TRAIL"):
                continue
            
            # 移動止損（若已啟動）
            if sig.get("trailing_active", False):
                atr = calc_atr(await self.fetcher.fetch_candles(sig["instId"]))  # 簡化，實際應緩存
                new_sl, new_high, new_low = risk_mgr.trailing_stop(
                    current_price, sig["entry"], sig["side"],
                    sig.get("highest_price", sig["entry"]),
                    sig.get("lowest_price", sig["entry"]),
                    atr, atr_mult=2.0
                )
                if new_sl != sig["sl"]:
                    sig["sl"] = new_sl
                    sig["highest_price"] = new_high
                    sig["lowest_price"] = new_low
                    db.update_signal_status(order_id, sig["status"], last_checked_ts=int(time.time()))
                    await send_tg_async(f"🔁 移動止損更新 {sig['instId']} 訂單{order_id[-8:]}: SL → {new_sl:.4f}", level="important")
            
            # 檢查觸發（需要 K 線數據，此處簡化為直接比較價格）
            # 完整實作應使用 K 線高/低點，可參考 v15 SignalTracker
            side = sig["side"]
            if not sig.get("hit_tp1") and self._hit_target(current_price, side, sig["tp1"]):
                await self._hit_tp(order_id, sig, 1, current_price)
            elif not sig.get("hit_tp2") and self._hit_target(current_price, side, sig["tp2"]):
                await self._hit_tp(order_id, sig, 2, current_price)
            elif not sig.get("hit_tp3") and self._hit_target(current_price, side, sig["tp3"]):
                await self._hit_tp(order_id, sig, 3, current_price)
            elif self._hit_target(current_price, side, sig["sl"], is_sl=True):
                await self._hit_sl(order_id, sig, current_price)
    
    def _hit_target(self, price: float, side: str, target: float, is_sl: bool = False) -> bool:
        if side == "LONG":
            return price >= target if not is_sl else price <= target
        else:
            return price <= target if not is_sl else price >= target
    
    async def _check_pending(self, order_id: str, sig: Dict, current_price: float):
        entry_low = sig["entry_low"]
        entry_high = sig["entry_high"]
        if (sig["side"]=="LONG" and entry_low <= current_price <= entry_high) or \
           (sig["side"]=="SHORT" and entry_low <= current_price <= entry_high):
            sig["status"] = "ACTIVE"
            sig["activated_at"] = int(time.time())
            db.update_signal_status(order_id, "ACTIVE", activated_at=sig["activated_at"])
            await send_tg_async(f"✅ 進場觸發 {sig['instId']} {sig['side']} 訂單{order_id[-8:]} 價格 {current_price:.4f}", level="important")
    
    async def _hit_tp(self, order_id: str, sig: Dict, level: int, price: float):
        setattr(sig, f"hit_tp{level}", True)
        pnl = (price - sig["entry"]) / sig["entry"] * 100 if sig["side"]=="LONG" else (sig["entry"] - price) / sig["entry"] * 100
        # 更新狀態
        if level == 1:
            sig["status"] = "BE"
            sig["sl"] = sig["entry"]  # 保本
        elif level == 2:
            sig["status"] = "TRAIL"
            sig["trailing_active"] = True
            sig["sl"] = sig["tp1"]
        else:  # TP3
            sig["status"] = "CLOSED"
            db.update_signal_status(order_id, "CLOSED", closed_at=int(time.time()))
            del self.active_signals[order_id]
        db.update_signal_status(order_id, sig["status"], last_checked_ts=int(time.time()))
        await send_tg_async(f"🎯 TP{level} 達標 {sig['instId']} 獲利 {pnl:+.2f}%", level="important")
    
    async def _hit_sl(self, order_id: str, sig: Dict, price: float):
        pnl = (price - sig["entry"]) / sig["entry"] * 100 if sig["side"]=="LONG" else (sig["entry"] - price) / sig["entry"] * 100
        sig["status"] = "CLOSED"
        db.update_signal_status(order_id, "CLOSED", closed_at=int(time.time()))
        del self.active_signals[order_id]
        await send_tg_async(f"🛑 止損觸發 {sig['instId']} 虧損 {pnl:+.2f}%", level="critical")
    
    async def run_periodic(self, interval: int = 10):
        while True:
            await self.check_all()
            await asyncio.sleep(interval)
