# tracker.py
import json
import time
import uuid
import logging
from typing import Dict
from indicators import calc_atr
from risk_manager import risk_mgr
from notifier import send_tg_sync
from config import config

class SignalTracker:
    def __init__(self):
        self.filepath = "active_signals.json"
        self.signals = self._load()
        self.transitions = 0

    def _load(self):
        try:
            with open(self.filepath, 'r') as f:
                return json.load(f)
        except:
            return {}

    def _save(self):
        with open(self.filepath, 'w') as f:
            json.dump(self.signals, f, indent=2)

    def add_signal(self, signal, current_price=None) -> str:
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}{signal['side']}{order_id}"
        in_zone = False
        if current_price:
            if signal["entry_low"] <= current_price <= signal["entry_high"]:
                in_zone = True
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if in_zone else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "trailing_active": False,
            "highest": signal["entry"], "lowest": signal["entry"],
            "activated_at": time.time() if in_zone else None,
            "entry_message_id": None,
            "last_checked_ts": time.time() if in_zone else None
        }
        self._save()
        return order_id

    def check_all(self):
        to_remove = []
        for key, sig in list(self.signals.items()):
            if self._check_one(key, sig):
                to_remove.append(key)
        for k in to_remove:
            del self.signals[k]
        if to_remove:
            self._save()

    def _check_one(self, key, sig):
        from data_fetcher import fetch_price  # 同步版本，需自行實現或改用同步請求
        price = fetch_price(sig["instId"])
        if price <= 0:
            return False
        sig["current_price"] = price
        status = sig["status"]
        if status == "PENDING":
            return self._check_pending(sig, price, key)
        if status not in ("ACTIVE", "BE", "TRAIL"):
            return False
        # 移動止損
        if sig.get("trailing_active"):
            from indicators import calc_atr
            import requests
            candles = self._fetch_candles_sync(sig["instId"])
            if candles:
                atr = calc_atr(candles)
                new_sl, new_high, new_low = risk_mgr.trailing_stop(
                    price, sig["entry"], sig["side"],
                    sig.get("highest", price), sig.get("lowest", price),
                    atr, atr_mult=config.get("risk.trailing_stop_atr_mult", 2.0)
                )
                if new_sl != sig["sl"]:
                    sig["sl"] = new_sl
                    sig["highest"] = new_high
                    sig["lowest"] = new_low
                    self._save()
                    send_tg_sync(f"🔁 移動止損更新 {sig['instId']} 訂單 {sig['order_id'][-8:]}: SL → {new_sl:.4f}", level="important")
        # 止盈止損檢查（簡化，實際需遍歷K線）
        if not sig.get("hit_tp1") and self._hit_target(price, sig["side"], sig["tp1"]):
            self._hit_tp(sig, 1, price, key)
        elif not sig.get("hit_tp2") and self._hit_target(price, sig["side"], sig["tp2"]):
            self._hit_tp(sig, 2, price, key)
        elif not sig.get("hit_tp3") and self._hit_target(price, sig["side"], sig["tp3"]):
            self._hit_tp(sig, 3, price, key)
        elif self._hit_target(price, sig["side"], sig["sl"], is_sl=True):
            self._hit_sl(sig, price, key)
        return False

    def _check_pending(self, sig, price, key):
        if sig["entry_low"] <= price <= sig["entry_high"]:
            sig["status"] = "ACTIVE"
            sig["activated_at"] = time.time()
            self._save()
            send_tg_sync(f"✅ 進場觸發 {sig['instId']} {sig['side']} 價格 {price:.4f} 部位建議 {sig.get('position_size',0):.2f} USDT", level="important")
        return False

    def _hit_target(self, price, side, target, is_sl=False):
        if side == "LONG":
            return price >= target if not is_sl else price <= target
        else:
            return price <= target if not is_sl else price >= target

    def _hit_tp(self, sig, level, price, key):
        setattr(self, f"hit_tp{level}", True)
        pnl = (price - sig["entry"]) / sig["entry"] * 100 if sig["side"]=="LONG" else (sig["entry"] - price) / sig["entry"] * 100
        if level == 1:
            sig["status"] = "BE"
            sig["sl"] = sig["entry"]
        elif level == 2:
            sig["status"] = "TRAIL"
            sig["trailing_active"] = True
            sig["sl"] = sig["tp1"]
        else:
            sig["status"] = "CLOSED"
            self._save_trade(sig, price, f"TP{level}", pnl)
            del self.signals[key]
        self._save()
        send_tg_sync(f"🎯 TP{level} 達標 {sig['instId']} 獲利 {pnl:+.2f}%", level="important")

    def _hit_sl(self, sig, price, key):
        pnl = (price - sig["entry"]) / sig["entry"] * 100 if sig["side"]=="LONG" else (sig["entry"] - price) / sig["entry"] * 100
        self._save_trade(sig, price, "SL", pnl)
        del self.signals[key]
        self._save()
        # 更新日內虧損
        risk_mgr.update_daily_loss(pnl)
        send_tg_sync(f"🛑 止損觸發 {sig['instId']} 虧損 {pnl:+.2f}%", level="critical")

    def _save_trade(self, sig, close_price, close_type, pnl):
        import os
        trade = {
            "order_id": sig["order_id"], "coin": sig["instId"].split("-")[0], "side": sig["side"],
            "entry": sig["entry"], "close": close_price, "close_type": close_type,
            "pnl": round(pnl,2), "score": sig["score"], "date": time.strftime("%Y-%m-%d"),
            "time": time.strftime("%Y-%m-%d %H:%M"), "features": {}
        }
        history = []
        if os.path.exists("trade_history.json"):
            with open("trade_history.json", "r") as f:
                history = json.load(f)
        history.append(trade)
        with open("trade_history.json", "w") as f:
            json.dump(history, f, indent=2)

    def send_position_updates(self):
        for sig in self.signals.values():
            if sig["status"] in ("ACTIVE","BE","TRAIL"):
                price = sig.get("current_price", 0)
                if price:
                    pnl = (price - sig["entry"]) / sig["entry"] * 100 if sig["side"]=="LONG" else (sig["entry"] - price) / sig["entry"] * 100
                    send_tg_sync(f"📊 {sig['instId']} {sig['side']} 當前 {price:.4f} {pnl:+.2f}% 止損 {sig['sl']:.4f}", level="all")

def enhance_signal_tracker():
    # 此函數保留給 main.py 調用，目前 tracker 已完整
    pass
