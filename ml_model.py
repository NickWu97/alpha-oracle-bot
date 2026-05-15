# ml_model.py
import json
import os
import numpy as np
from typing import List, Dict

class MLPredictor:
    def __init__(self):
        self.model = None
        self.ready = False

    def train_from_history(self):
        """從 trade_history.json 訓練簡單模型（佔位，實際需用 LightGBM）"""
        if not os.path.exists("trade_history.json"):
            return
        with open("trade_history.json", "r") as f:
            trades = json.load(f)
        if len(trades) < 30:
            return
        # 此處僅收集特徵，不實際訓練
        self.ready = True

    def predict(self, signal: Dict) -> float:
        """回傳預測勝率 0~1"""
        if not self.ready:
            return 0.5
        # 簡易規則：評分高則勝率高
        score = signal.get("score", 70)
        prob = min(0.9, 0.4 + (score-50)/100)
        return prob

    def is_ready(self):
        return self.ready

def calculate_sharpe_maxdrawdown(trades: List[Dict]):
    """計算 Sharpe Ratio 和最大回撤（基於日回報）"""
    pnls = [t.get("pnl", 0) for t in trades]
    if len(pnls) < 2:
        return 0.0, 0.0
    returns = np.array(pnls)
    sharpe = returns.mean() / (returns.std() + 1e-6) * np.sqrt(252)
    cumsum = np.cumsum(returns)
    running_max = np.maximum.accumulate(cumsum)
    drawdown = (cumsum - running_max) / (running_max + 1e-6)
    max_dd = -np.min(drawdown) * 100
    return sharpe, max_dd
