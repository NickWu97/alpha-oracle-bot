# risk_manager.py
import math
from typing import Dict, Tuple
from indicators import calc_atr

class RiskManager:
    def __init__(self, initial_equity: float = 10000.0):
        self.initial_equity = initial_equity
        self.current_equity = initial_equity
        self.peak_equity = initial_equity
        self.trades = []   # 可擴充
    
    def update_equity(self, pnl_percent: float) -> float:
        self.current_equity *= (1 + pnl_percent/100)
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        drawdown = (self.peak_equity - self.current_equity) / self.peak_equity * 100
        return drawdown
    
    def current_drawdown(self) -> float:
        return (self.peak_equity - self.current_equity) / self.peak_equity * 100
    
    def is_circuit_breaker(self, max_drawdown_pct: float = 5.0) -> bool:
        return self.current_drawdown() >= max_drawdown_pct
    
    def kelly_fraction(self, win_rate: float, avg_win: float, avg_loss: float, kelly_fraction: float = 0.25) -> float:
        if avg_loss == 0:
            return 0
        b = avg_win / avg_loss
        p = win_rate / 100
        q = 1 - p
        f = (p * b - q) / b
        return max(0.0, min(f, 0.25)) * kelly_fraction
    
    def calculate_stop_loss(self, entry: float, side: str, atr: float = None, method: str = "percent", percent: float = 0.5, atr_mult: float = 1.5) -> float:
        if method == "percent":
            dist = entry * percent / 100
        else:  # atr
            if atr is None:
                raise ValueError("ATR required for ATR-based stop")
            dist = atr * atr_mult
        if side == "LONG":
            return entry - dist
        else:
            return entry + dist
    
    def trailing_stop(self, current_price: float, entry: float, side: str, highest: float, lowest: float, atr: float, atr_mult: float = 2.0) -> Tuple[float, float, float]:
        """回傳 (new_sl, new_highest, new_lowest)"""
        new_high = highest
        new_low = lowest
        if side == "LONG":
            if current_price > highest:
                new_high = current_price
            trail_stop = new_high - atr_mult * atr
            # 不低於進場價保本
            if trail_stop < entry:
                trail_stop = entry
            return trail_stop, new_high, new_low
        else:
            if current_price < lowest:
                new_low = current_price
            trail_stop = new_low + atr_mult * atr
            if trail_stop > entry:
                trail_stop = entry
            return trail_stop, new_high, new_low

risk_mgr = RiskManager()
