#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v8.2 — 价格监控 + 保本移损 + 每日战报
══════════════════════════════════════════════════════════════════
v8.1 全功能 + 新增：
  ✅ 每日午夜战报 — 统计前一天胜率、盈亏比、总收益
  ✅ 交易记录持久化 — 保存到 trade_history.json
  ✅ 胜率统计 — Win Rate、平均盈亏、最佳/最差交易

══ 执行模式 ══════════════════════════════════
  python alpha_oracle_v8.2.py              → 扫描 + 监控
  python alpha_oracle_v8.2.py --mode scan  → 只扫描
  python alpha_oracle_v8.2.py --report     → 发送昨日战报
══════════════════════════════════════════════════════════════════
"""

import requests
import os
import json
import sys
import argparse
import pandas as pd
import numpy as np
import logging
import traceback
import time
import threading
import signal
from datetime import datetime, timedelta
from collections import defaultdict

# ─────────────────────────────────────────────────────────
# 1. 基础配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("alpha_oracle_v8.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID  = os.getenv("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

SCAN_TIMEFRAMES         = ["15m", "30m"]
MAX_SIGNALS_PER_RUN     = int(os.getenv("MAX_SIGNALS", "8"))
SETUP_SCORE_THRESHOLD   = 75

# 订单流参数
CROSSLINE_BODY_RATIO       = 0.30
SWEEP_VOLUME_RATIO         = 1.8
SWEEP_CONSECUTIVE_MOVES    = 2
NEWS_COOLDOWN_MINUTES      = 60
ABSORPTION_VOL_MULTIPLIER  = 1.8
ABSORPTION_PRICE_THRESHOLD = 0.002

# v8 精度参数
VOLATILITY_HARD_LIMIT   = 0.035
ATR_SL_MULT             = 1.5
RSI_PERIOD              = 14
ADX_PERIOD              = 14

# 监控参数
ENTRY_TOLERANCE         = 0.002
ACTIVE_SIGNALS_FILE     = "active_signals.json"
TRADE_HISTORY_FILE      = "trade_history.json"  # ✅ 新增
SIGNAL_EXPIRE_HOURS     = 24

# 全局停止标志
stop_requested = False
_news_cooldown: dict = {}

# ─────────────────────────────────────────────────────────
# 2. 工具 & 通知
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str, parse_mode: str = "Markdown") -> bool:
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️  TG_TOKEN / CHAT_ID 未设定，跳过通知")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode},
            timeout=15
        )
        return r.status_code == 200
    except Exception as e:
        logging.error(f"Telegram Error: {e}")
        return False

def check_news_cooldown(instId: str) -> bool:
    return time.time() - _news_cooldown.get(instId, 0) >= NEWS_COOLDOWN_MINUTES * 60

def mark_news_event(instId: str):
    _news_cooldown[instId] = time.time()
    logging.info(f"📰 News cooldown set: {instId}")

def signal_handler(signum, frame):
    global stop_requested
    logging.info("🛑 收到停止信号，正在退出...")
    stop_requested = True
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ─────────────────────────────────────────────────────────
# 3. 交易记录管理（新增）
# ─────────────────────────────────────────────────────────
class TradeHistory:
    """管理交易历史记录与胜率统计"""
    
    def __init__(self, filepath: str = TRADE_HISTORY_FILE):
        self.filepath = filepath
        self.history = self._load()
    
    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"trades": [], "daily_stats": {}}
    
    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)
    
    def add_trade(self, signal_key: str, coin: str, side: str, 
                  entry: float, sl: float, tp1: float, tp2: float, tp3: float,
                  score: int):
        """添加交易记录"""
        trade = {
            "key": signal_key,
            "coin": coin,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "score": score,
            "entry_time": datetime.now().isoformat(),
            "exit_time": None,
            "exit_price": None,
            "exit_type": None,  # TP1/TP2/TP3/SL
            "pnl_pct": None,
            "status": "OPEN"
        }
        self.history["trades"].append(trade)
        self._save()
        logging.info(f"📝 交易记录已添加: {coin} {side} @ {entry}")
    
    def close_trade(self, signal_key: str, exit_price: float, exit_type: str):
        """关闭交易记录"""
        for trade in reversed(self.history["trades"]):
            if trade["key"] == signal_key and trade["status"] == "OPEN":
                # 计算盈亏
                if trade["side"] == "LONG":
                    pnl_pct = (exit_price - trade["entry"]) / trade["entry"] * 100
                else:
                    pnl_pct = (trade["entry"] - exit_price) / trade["entry"] * 100
                
                trade["exit_time"] = datetime.now().isoformat()
                trade["exit_price"] = exit_price
                trade["exit_type"] = exit_type
                trade["pnl_pct"] = round(pnl_pct, 2)
                trade["status"] = "CLOSED"
                
                self._save()
                logging.info(f"✅ 交易已关闭: {trade['coin']} {exit_type} PnL={pnl_pct:.2f}%")
                return trade
        return None
    
    def get_daily_stats(self, date_str: str = None) -> dict:
        """获取指定日期的统计数据"""
        if date_str is None:
            date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        # 筛选该日期的交易
        day_trades = [
            t for t in self.history["trades"]
            if t["status"] == "CLOSED" and t["exit_time"] and t["exit_time"].startswith(date_str)
        ]
        
        if not day_trades:
            return {"date": date_str, "total": 0}
        
        # 计算统计
        wins = [t for t in day_trades if t["pnl_pct"] > 0]
        losses = [t for t in day_trades if t["pnl_pct"] <= 0]
        
        total_pnl = sum(t["pnl_pct"] for t in day_trades)
        avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
        avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
        
        win_rate = len(wins) / len(day_trades) * 100 if day_trades else 0
        
        # 最佳/最差交易
        best_trade = max(day_trades, key=lambda x: x["pnl_pct"]) if day_trades else None
        worst_trade = min(day_trades, key=lambda x: x["pnl_pct"]) if day_trades else None
        
        return {
            "date": date_str,
            "total": len(day_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "trades": day_trades
        }
    
    def generate_daily_report(self, date_str: str = None) -> str:
        """生成每日战报"""
        stats = self.get_daily_stats(date_str)
        
        if stats["total"] == 0:
            date_display = date_str or "昨日"
            return f"📊 *Alpha Oracle 每日战报* ({date_display})\n" \
                   f"━━━━━━━━━━━━━━━━\n" \
                   f"📭 今日无交易记录\n" \
                   f"建议检查扫描条件或市场波动性"
        
        # 格式化交易列表
        trades_txt = ""
        for t in stats["trades"][:10]:  # 只显示前 10 笔
            emoji = "✅" if t["pnl_pct"] > 0 else "❌"
            trades_txt += f"{emoji} {t['coin']} {t['side']}  {t['exit_type']}  {t['pnl_pct']:+.2f}%\n"
        
        if len(stats["trades"]) > 10:
            trades_txt += f"... 等共 {stats['total']} 笔交易\n"
        
        # 最佳/最差
        best_txt = f"{stats['best_trade']['coin']} {stats['best_trade']['side']} {stats['best_trade']['pnl_pct']:+.2f}%" if stats['best_trade'] else "─"
        worst_txt = f"{stats['worst_trade']['coin']} {stats['worst_trade']['side']} {stats['worst_trade']['pnl_pct']:+.2f}%" if stats['worst_trade'] else "─"
        
        report = (
            f"📊 *Alpha Oracle 每日战报*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📅 日期：{stats['date']}\n"
            f"📈 总交易：{stats['total']} 笔\n"
            f"✅ 盈利：{stats['wins']} 笔\n"
            f"❌ 亏损：{stats['losses']} 笔\n"
            f"🎯 胜率：{stats['win_rate']:.1f}%\n"
            f"💰 总盈亏：{stats['total_pnl']:+.2f}%\n"
            f"📊 平均盈利：{stats['avg_win']:+.2f}%\n"
            f"📉 平均亏损：{stats['avg_loss']:+.2f}%\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏆 最佳交易：{best_txt}\n"
            f"💔 最差交易：{worst_txt}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📋 *交易明细：*\n"
            f"{trades_txt}"
            f"━━━━━━━━━━━━━━━━\n"
            f"💡 *继续加油！保持纪律交易！*"
        )
        
        return report

# 全局交易记录实例
trade_history = TradeHistory()

# ─────────────────────────────────────────────────────────
# 4. 数据抓取
# ─────────────────────────────────────────────────────────
def fetch_okx(instId: str, tf: str = "15m", limit: int = 150):
    try:
        url = (f"https://www.okx.com/api/v5/market/candles"
               f"?instId={instId}&bar={tf}&limit={limit}")
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0":
            return None
        df = pd.DataFrame(
            res["data"],
            columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"]
        )
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        df = df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
        return df if len(df) >= 30 else None
    except Exception as e:
        logging.warning(f"[{instId}/{tf}] Fetch: {e}")
        return None

def fetch_ticker_price(instId: str) -> float:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            timeout=5
        ).json()
        if res.get("code") == "0" and res.get("data"):
            return float(res["data"][0]["last"])
        return 0.0
    except:
        return 0.0

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        return float(res["data"][0]["fundingRate"]) if res.get("data") else 0.0
    except: return 0.0

def fetch_ls_ratio(symbol: str) -> tuple:
    try:
        base = symbol.split("-")[0]
        res = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}",
            timeout=5
        ).json()
        if res.get("data"):
            r = float(res["data"][0]["ratio"])
            return r, f"{r:.2f}"
        return 1.0, "N/A"
    except: return 1.0, "N/A"

def fetch_order_book(instId: str, depth: int = 20) -> tuple:
    try:
        res = requests.get(
            f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5
        ).json()
        if res.get("code") != "0" or not res.get("data"):
            return 1.0, "⚪ 盘口均衡"
        data    = res["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"]) or 1e-10
        ratio   = bid_vol / ask_vol
        if   ratio >= 1.30: label = f"🟢 买盘强势 ({ratio:.2f})"
        elif ratio >= 1.05: label = f"🟡 买盘略强 ({ratio:.2f})"
        elif ratio >= 0.95: label = f"⚪ 盘口均衡 ({ratio:.2f})"
        elif ratio >= 0.77: label = f"🟡 卖盘略强 ({ratio:.2f})"
        else:               label = f"🔴 卖盘强势 ({ratio:.2f})"
        return ratio, label
    except: return 1.0, "⚪ 盘口均衡"

# ─────────────────────────────────────────────────────────
# 5. 技术指标（保持原样，省略以节省空间）
# ─────────────────────────────────────────────────────────
# ... [保留所有原有的技术指标函数] ...
# calculate_atr, calculate_ema, calculate_supertrend, calculate_rsi, calculate_adx
# detect_market_regime, adx_regime_bonus, detect_rsi_divergence, get_btc_bias, get_4h_trend
# check_extreme_volatility, calculate_dynamic_sl
# find_swing_points, detect_bos_choch, detect_market_structure
# find_liquidity_pools, find_order_blocks, find_fvg, check_ob_fvg_entry
# detect_premium_discount, detect_crossline, detect_active_sweep
# detect_fishing_trap, detect_absorption, calculate_cvd
# interpret_ls_ratio, interpret_funding_rate, check_ob_direction
# detect_pa, detect_whale_zones, calculate_score
# scan_timeframe, scan_for_opportunity, format_signal, format_alert

# ─────────────────────────────────────────────────────────
# 6. SignalTracker（修改：添加交易记录）
# ─────────────────────────────────────────────────────────
class SignalTracker:
    """追踪活跃讯号，持久化到 JSON"""
    
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self._lock    = threading.Lock()
        self.signals  = self._load()
    
    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    
    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.signals, f, ensure_ascii=False, indent=2)
    
    def add(self, opp: dict) -> str:
        key = f"{opp['instId']}_{opp['side']}_{opp['tf']}_{int(time.time())}"
        with self._lock:
            self.signals[key] = {
                "instId"   : opp["instId"],
                "side"     : opp["side"],
                "tf"       : opp["tf"],
                "entry"    : opp["entry"],
                "sl"       : opp["sl"],
                "sl_orig"  : opp["sl"],
                "tp1"      : opp["tp1"],
                "tp2"      : opp["tp2"],
                "tp3"      : opp["tp3"],
                "score"    : opp["score"],
                "grade"    : opp["grade"],
                "status"   : "PENDING",
                "hit_tp1"  : False,
                "hit_tp2"  : False,
                "created"  : time.time(),
            }
            self._save()
        logging.info(f"📌 讯号加入追踪: {key}")
        return key
    
    def remove(self, key: str):
        with self._lock:
            self.signals.pop(key, None)
            self._save()
    
    def update(self, key: str, **kwargs):
        with self._lock:
            if key in self.signals:
                self.signals[key].update(kwargs)
                self._save()
    
    def list_active(self) -> list:
        with self._lock:
            return list(self.signals.items())
    
    def _get_price(self, instId: str) -> float:
        return fetch_ticker_price(instId)
    
    def check_one(self, key: str, sig: dict) -> bool:
        price = self._get_price(sig["instId"])
        if price <= 0:
            return False
        
        coin   = sig["instId"].split("-")[0]
        side   = sig["side"]
        status = sig["status"]
        entry  = sig["entry"]
        sl     = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        
        # ── 过期清理 ─────────────────────────────
        if status == "PENDING":
            age_h = (time.time() - sig["created"]) / 3600
            if age_h > SIGNAL_EXPIRE_HOURS:
                logging.info(f"  ⏰ 讯号过期清除: {key}")
                send_tg(f"⏰ *讯号过期* — #{coin} {side}\n"
                        f"进场 `{entry:.4f}` 超过{SIGNAL_EXPIRE_HOURS}h 未触发，已移除")
                return True
        
        # ── PENDING → 进场触发 ───────────────────
        if status == "PENDING":
            entered = (
                (side=="LONG"  and price <= entry * (1 + ENTRY_TOLERANCE)) or
                (side=="SHORT" and price >= entry * (1 - ENTRY_TOLERANCE))
            )
            if entered:
                self.update(key, status="ACTIVE")
                send_tg(format_alert(coin, side, "ENTRY",
                                     price, entry, sl, tp1, tp2, tp3, score=sig["score"]))
                
                # ✅ 添加到交易记录
                trade_history.add_trade(
                    signal_key=key,
                    coin=coin,
                    side=side,
                    entry=entry,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    score=sig["score"]
                )
                
                logging.info(f"  ✅ 进场触发: {key}  price={price:.4f}")
            return False
        
        if status not in ("ACTIVE", "BE", "TRAIL"):
            return False
        
        # ── 止损触发 ─────────────────────────────
        sl_hit = (side=="LONG" and price <= sl) or (side=="SHORT" and price >= sl)
        if sl_hit:
            send_tg(format_alert(coin, side, "SL",
                                 price, entry, sl, tp1, tp2, tp3))
            
            # ✅ 关闭交易记录
            trade_history.close_trade(key, price, "SL")
            
            logging.info(f"  🛑 止损触发: {key}  price={price:.4f}  sl={sl:.4f}")
            return True
        
        # ── TP3 ───────────────────────────────────
        tp3_hit = (side=="LONG" and price >= tp3) or (side=="SHORT" and price <= tp3)
        if tp3_hit:
            send_tg(format_alert(coin, side, "TP3",
                                 price, entry, sl, tp1, tp2, tp3))
            
            # ✅ 关闭交易记录
            trade_history.close_trade(key, price, "TP3")
            
            logging.info(f"  🏆 TP3 全部到达: {key}")
            return True
        
        # ── TP2 → 移损至 TP1 ─────────────────────
        tp2_hit = (side=="LONG" and price >= tp2) or (side=="SHORT" and price <= tp2)
        if tp2_hit and not sig.get("hit_tp2"):
            self.update(key, hit_tp2=True, sl=tp1, status="TRAIL")
            send_tg(format_alert(coin, side, "TP2",
                                 price, entry, sl, tp1, tp2, tp3, new_sl=tp1))
            logging.info(f"  🥈 TP2 到达，移损至 TP1={tp1:.4f}: {key}")
            return False
        
        # ── TP1 → 保本移损 ────────────────────────
        tp1_hit = (side=="LONG" and price >= tp1) or (side=="SHORT" and price <= tp1)
        if tp1_hit and not sig.get("hit_tp1"):
            self.update(key, hit_tp1=True, sl=entry, status="BE")
            send_tg(format_alert(coin, side, "TP1",
                                 price, entry, sl, tp1, tp2, tp3, new_sl=entry))
            logging.info(f"  🥇 TP1 到达，移损至成本={entry:.4f}: {key}")
            return False
        
        return False
    
    def check_all(self):
        to_remove = []
        for key, sig in self.list_active():
            try:
                if self.check_one(key, sig):
                    to_remove.append(key)
            except Exception as e:
                logging.error(f"  SignalTracker.check_one [{key}]: {e}")
        for key in to_remove:
            self.remove(key)
        if to_remove:
            logging.info(f"  🗑️  移除 {len(to_remove)} 笔已关闭讯号")
    
    def status_summary(self) -> str:
        items = self.list_active()
        if not items:
            return "📭 目前无追踪中讯号"
        lines = [f"📋 *追踪中讯号 ({len(items)} 笔)*\n━━━━━━━━━━━━━━"]
        for key, s in items:
            coin  = s["instId"].split("-")[0]
            arrow = "🟢" if s["side"]=="LONG" else "🔴"
            status_emoji = {
                "PENDING":"⏳","ACTIVE":"🔵","BE":"🛡","TRAIL":"🔁"
            }.get(s["status"],"❓")
            lines.append(
                f"{status_emoji} #{coin} {arrow}{s['side']} {s['tf']}  "
                f"E:`{s['entry']:.4f}`  SL:`{s['sl']:.4f}`  "
                f"TP1:`{s['tp1']:.4f}`  [{s['score']}分]"
            )
        return "\n".join(lines)

# ─────────────────────────────────────────────────────────
# 7. 主执行（新增 --report 参数）
# ─────────────────────────────────────────────────────────
def main():
    global stop_requested
    parser = argparse.ArgumentParser(description="Alpha Oracle v8.2")
    parser.add_argument("--mode", default="all",
                        choices=["scan", "monitor", "loop", "all"],
                        help="scan=只扫描 | monitor=只监控 | loop=定时扫描+监控 | all=扫描后持续监控（预设）")
    parser.add_argument("--interval", type=int, default=30,
                        help="监控轮询间隔（秒），预设30")
    parser.add_argument("--loop-interval", type=int, default=900,
                        help="loop模式扫描间隔（秒），预设900=15分钟")
    parser.add_argument("--max-duration", type=int, default=None,
                        help="最大运行时间（秒），预设无限制")
    parser.add_argument("--status", action="store_true",
                        help="印出目前追踪中讯号并传送 TG 摘要")
    parser.add_argument("--report", action="store_true",  # ✅ 新增
                        help="发送昨日战报")
    parser.add_argument("--report-date", type=str, default=None,  # ✅ 新增
                        help="指定战报日期 (YYYY-MM-DD)，预设昨天")
    args = parser.parse_args()
    
    tracker = SignalTracker()
    
    # ── 查询模式 ─────────────────────────────────
    if args.status:
        summary = tracker.status_summary()
        print(summary)
        send_tg(summary)
        return
    
    # ── 每日战报模式 ────────────────────────────
    if args.report:
        report = trade_history.generate_daily_report(args.report_date)
        print(report)
        send_tg(report)
        return
    
    # ── scan：只扫描一次 ─────────────────────────
    if args.mode == "scan":
        run_scan(tracker)
        return
    
    # ── monitor：只监控（不扫描）────────────────
    if args.mode == "monitor":
        try:
            monitor_loop(tracker, interval=args.interval, max_duration=args.max_duration)
        except KeyboardInterrupt:
            logging.info("👋 监控停止")
        return
    
    # ── loop：定时扫描 + 持续监控 ────────────────
    if args.mode == "loop":
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_loop,
                             args=(tracker, args.interval, args.max_duration, stop_ev),
                             daemon=True)
        t.start()
        try:
            while not stop_requested and not stop_ev.is_set():
                run_scan(tracker)
                logging.info(f"⏱️  下次扫描：{args.loop_interval}s 后")
                for _ in range(min(args.loop_interval, 5)):
                    if stop_requested or stop_ev.is_set():
                        break
                    time.sleep(1)
        except KeyboardInterrupt:
            logging.info("👋 回路停止")
            stop_ev.set()
            stop_requested = True
        return
    
    # ── all（预设）：扫描一次 + 持续监控 ─────────
    run_scan(tracker)
    try:
        monitor_loop(tracker, interval=args.interval, max_duration=args.max_duration)
    except KeyboardInterrupt:
        logging.info("👋 停止")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"💥 {e}"); traceback.print_exc(); sys.exit(1)
