# risk_manager.py
import json
import os
from typing import Tuple

class RiskManager:
    def __init__(self):
        self.initial_equity = 10000.0
        self.current_equity = self.initial_equity
        self.peak_equity = self.initial_equity
        self.daily_loss = 0.0
        self.last_date = ""

    def update_equity(self, pnl_percent: float):
        self.current_equity *= (1 + pnl_percent/100)
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        return self.current_drawdown()

    def current_drawdown(self) -> float:
        return (self.peak_equity - self.current_equity) / self.peak_equity * 100

    def calculate_position_size(self, entry: float, stop_loss: float, atr: float) -> float:
        """動態部位規模：根據固定風險金額計算下注數量（USDT）"""
        risk_amount = self.current_equity * 0.01  # 每筆風險 1% 本金，可調整
        # 若提供了 fixed_risk_amount 則優先使用
        fixed_risk = config.get("risk.fixed_risk_amount", None)
        if fixed_risk:
            risk_amount = fixed_risk
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit <= 0:
            return 0
        # 返回部位價值（USDT）
        position_value = risk_amount / (risk_per_unit / entry)
        return min(position_value, self.current_equity * 0.25)  # 最多佔用 25% 本金

    def update_daily_loss(self, pnl_percent: float):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.last_date:
            self.daily_loss = 0.0
            self.last_date = today
        if pnl_percent < 0:
            self.daily_loss += abs(pnl_percent)

    def is_daily_loss_exceeded(self, limit_percent: float) -> bool:
        return self.daily_loss >= limit_percent

risk_mgr = RiskManager()
