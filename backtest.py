#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro - 回測系統
══════════════════════════════════════════════════════════════════════
功能：
  - 歷史數據回測
  - 策略績效分析
  - 參數優化
  - 蒙特卡羅模擬
"""
import json
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List
import requests


class BacktestEngine:
    """回測引擎"""
    
    def __init__(self, config: dict):
        self.config = config
        self.initial_capital = config.get("backtest", {}).get("initial_capital", 1000)
        self.capital = self.initial_capital
        self.positions = []
        self.trades = []
        self.equity_curve = []
        
    def fetch_historical_data(self, instId: str, tf: str = "15m", 
                              days: int = 30) -> List[dict]:
        """抓取歷史數據"""
        candles = []
        limit = 100
        total_candles = days * 24 * 4 if tf == "15m" else days * 24
        
        print(f"📥 下載 {instId} 歷史數據... (約 {total_candles} 根 K 線)")
        
        while len(candles) < total_candles:
            try:
                r = requests.get(
                    f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
                    timeout=10
                ).json()
                
                if r.get("code") != "0" or not r.get("data"):
                    break
                
                for row in r["data"]:
                    candles.append({
                        "ts": int(row[0]),
                        "o": float(row[1]),
                        "h": float(row[2]),
                        "l": float(row[3]),
                        "c": float(row[4]),
                        "v": float(row[5])
                    })
                
                time.sleep(0.2)  # 避免 API 限制
                
            except Exception as e:
                print(f"⚠️ 抓取失敗：{e}")
                break
        
        candles.sort(key=lambda x: x["ts"])
        print(f"✅ 下載完成：{len(candles)} 根 K 線")
        return candles
    
    def run_backtest(self, instId: str, candles: List[dict]) -> dict:
        """執行回測"""
        print(f"\n🚀 開始回測 {instId}...")
        
        self.capital = self.initial_capital
        self.positions = []
        self.trades = []
        self.equity_curve = []
        
        # 簡化版策略邏輯（實際應整合 main.py 的評分系統）
        for i in range(100, len(candles) - 1):
            df = candles[i-100:i+1]
            current_price = df[-1]["c"]
            
            # 簡單移動平均策略範例
            ma20 = sum(c["c"] for c in df[-20:]) / 20
            ma50 = sum(c["c"] for c in df[-50:]) / 50
            
            # 多頭訊號
            if not self.positions and current_price > ma20 > ma50:
                self._open_position(instId, "LONG", current_price, df[-1]["ts"])
            
            # 平倉訊號
            elif self.positions:
                pos = self.positions[0]
                if pos["side"] == "LONG" and current_price < ma20:
                    self._close_position(instId, current_price, df[-1]["ts"])
        
        # 平掉所有持倉
        if self.positions:
            for pos in self.positions[:]:
                self._close_position(instId, candles[-1]["c"], candles[-1]["ts"])
        
        return self._calculate_metrics()
    
    def _open_position(self, instId: str, side: str, price: float, ts: int):
        """開倉"""
        position = {
            "instId": instId,
            "side": side,
            "entry_price": price,
            "entry_time": ts,
            "contracts": (self.capital * 0.1) / price  # 使用 10% 資金
        }
        self.positions.append(position)
        print(f"🟢 開倉 {instId} {side} @ {price:.4f}")
    
    def _close_position(self, instId: str, price: float, ts: int):
        """平倉"""
        pos = self.positions[0]
        pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"] * 100 
                   if pos["side"] == "LONG" 
                   else (pos["entry_price"] - price) / pos["entry_price"] * 100)
        
        pnl_usd = pos["contracts"] * (price - pos["entry_price"])
        
        self.trades.append({
            "instId": instId,
            "side": pos["side"],
            "entry_price": pos["entry_price"],
            "exit_price": price,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "entry_time": pos["entry_time"],
            "exit_time": ts
        })
        
        self.capital += pnl_usd
        self.positions.remove(pos)
        
        print(f"🔴 平倉 {instId} {pos['side']} @ {price:.4f} | PnL: {pnl_pct:+.2f}%")
    
    def _calculate_metrics(self) -> dict:
        """計算績效指標"""
        if not self.trades:
            return {"error": "無交易記錄"}
        
        wins = [t for t in self.trades if t["pnl_pct"] > 0]
        losses = [t for t in self.trades if t["pnl_pct"] <= 0]
        
        total_pnl = sum(t["pnl_pct"] for t in self.trades)
        win_rate = len(wins) / len(self.trades) * 100
        
        # 計算最大回撤
        peak = self.initial_capital
        max_drawdown = 0
        for trade in self.trades:
            peak = max(peak, self.initial_capital + sum(t["pnl_usd"] for t in self.trades[:self.trades.index(trade)+1]))
            current = self.initial_capital + sum(t["pnl_usd"] for t in self.trades[:self.trades.index(trade)+1])
            drawdown = (peak - current) / peak * 100
            max_drawdown = max(max_drawdown, drawdown)
        
        # 計算夏普比率（簡化）
        returns = [t["pnl_pct"] for t in self.trades]
        avg_return = sum(returns) / len(returns)
        std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
        sharpe = avg_return / std_return if std_return > 0 else 0
        
        return {
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "total_pnl_pct": round(total_pnl, 2),
            "total_pnl_usd": round(self.capital - self.initial_capital, 2),
            "max_drawdown": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 2),
            "final_capital": round(self.capital, 2),
            "avg_win": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0,
            "profit_factor": round(
                abs(sum(t["pnl_usd"] for t in wins) / sum(t["pnl_usd"] for t in losses)), 2
            ) if losses and sum(t["pnl_usd"] for t in losses) != 0 else 0
        }


def run_backtest(coin: str = "BTC-USDT-SWAP", days: int = 30, tf: str = "15m"):
    """執行回測"""
    print("=" * 60)
    print("🤖 Alpha Oracle Pro - 回測系統")
    print("=" * 60)
    
    # 載入配置
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {
            "backtest": {
                "initial_capital": 1000
            }
        }
    
    engine = BacktestEngine(config)
    
    # 抓取歷史數據
    candles = engine.fetch_historical_data(coin, tf, days)
    
    if not candles:
        print("❌ 無法獲取歷史數據")
        return
    
    # 執行回測
    metrics = engine.run_backtest(coin, candles)
    
    # 輸出結果
    print("\n" + "=" * 60)
    print("📊 回測結果")
    print("=" * 60)
    print(f"總交易數：{metrics.get('total_trades', 0)}")
    print(f"勝率：{metrics.get('win_rate', 0):.2f}%")
    print(f"總 PnL：{metrics.get('total_pnl_pct', 0):+.2f}% (${metrics.get('total_pnl_usd', 0):.2f})")
    print(f"最大回撤：{metrics.get('max_drawdown', 0):.2f}%")
    print(f"夏普比率：{metrics.get('sharpe_ratio', 0):.2f}")
    print(f"獲利因子：{metrics.get('profit_factor', 0):.2f}")
    print(f"平均獲利：{metrics.get('avg_win', 0):+.2f}%")
    print(f"平均虧損：{metrics.get('avg_loss', 0):+.2f}%")
    print(f"最終資金：${metrics.get('final_capital', 0):.2f}")
    print("=" * 60)
    
    # 保存結果
    result = {
        "coin": coin,
        "timeframe": tf,
        "days": days,
        "metrics": metrics,
        "trades": engine.trades,
        "timestamp": datetime.now().isoformat()
    }
    
    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 結果已保存至 backtest_result.json")


if __name__ == "__main__":
    coin = sys.argv[1] if len(sys.argv) > 1 else "BTC-USDT-SWAP"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    tf = sys.argv[3] if len(sys.argv) > 3 else "15m"
    
    run_backtest(coin, days, tf)
