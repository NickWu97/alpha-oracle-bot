#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v8.2 — 全自动交易信号 + 每日战报系统
══════════════════════════════════════════════════════════════════
功能：
  ✅ 多时间框架扫描（15m/30m）
  ✅ 实时价格监控 + 自动通知
  ✅ 保本移损（TP1→成本，TP2→TP1）
  ✅ 每日午夜战报（胜率/盈亏统计）
  ✅ 交易记录持久化（JSON）
  ✅ 75分高精度评分系统

执行模式：
  python alpha_oracle_v8.2.py --mode scan        # 只扫描
  python alpha_oracle_v8.2.py --mode loop        # 持续扫描+监控
  python alpha_oracle_v8.2.py --report           # 发送昨日战报
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
from typing import Optional, Dict, List, Tuple

# ─────────────────────────────────────────────────────────
# 1. 基础配置
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("alpha_oracle.log", encoding="utf-8", mode="a"),
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

# 精度参数
VOLATILITY_HARD_LIMIT   = 0.035
ATR_SL_MULT             = 1.5
RSI_PERIOD              = 14
ADX_PERIOD              = 14

# 监控参数
ENTRY_TOLERANCE         = 0.002
ACTIVE_SIGNALS_FILE     = "active_signals.json"
TRADE_HISTORY_FILE      = "trade_history.json"
SIGNAL_EXPIRE_HOURS     = 24

stop_requested = False
_news_cooldown: dict = {}

# ─────────────────────────────────────────────────────────
# 2. 工具函数
# ─────────────────────────────────────────────────────────
def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

def send_tg(msg: str, parse_mode: str = "Markdown") -> bool:
    if not TG_TOKEN or not CHAT_ID:
        logging.warning("⚠️  Telegram 未设定")
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

def signal_handler(signum, frame):
    global stop_requested
    logging.info("🛑 收到停止信号")
    stop_requested = True
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ─────────────────────────────────────────────────────────
# 3. 交易记录管理
# ─────────────────────────────────────────────────────────
class TradeHistory:
    def __init__(self, filepath: str = TRADE_HISTORY_FILE):
        self.filepath = filepath
        self.history = self._load()
    
    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"trades": []}
    
    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)
    
    def add_trade(self, key: str, coin: str, side: str, entry: float, 
                  sl: float, tp1: float, tp2: float, tp3: float, score: int):
        trade = {
            "key": key, "coin": coin, "side": side,
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "score": score, "entry_time": datetime.now().isoformat(),
            "exit_time": None, "exit_price": None, "exit_type": None,
            "pnl_pct": None, "status": "OPEN"
        }
        self.history["trades"].append(trade)
        self._save()
        logging.info(f"📝 交易记录: {coin} {side} @ {entry}")
    
    def close_trade(self, key: str, exit_price: float, exit_type: str):
        for trade in reversed(self.history["trades"]):
            if trade["key"] == key and trade["status"] == "OPEN":
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
                logging.info(f"✅ 交易关闭: {trade['coin']} {exit_type} {pnl_pct:+.2f}%")
                return trade
        return None
    
    def get_daily_stats(self, date_str: str = None) -> dict:
        if date_str is None:
            date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        day_trades = [
            t for t in self.history["trades"]
            if t["status"] == "CLOSED" and t["exit_time"] and t["exit_time"].startswith(date_str)
        ]
        
        if not day_trades:
            return {"date": date_str, "total": 0}
        
        wins = [t for t in day_trades if t["pnl_pct"] > 0]
        losses = [t for t in day_trades if t["pnl_pct"] <= 0]
        
        return {
            "date": date_str,
            "total": len(day_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(day_trades) * 100, 2) if day_trades else 0,
            "total_pnl": round(sum(t["pnl_pct"] for t in day_trades), 2),
            "avg_win": round(np.mean([t["pnl_pct"] for t in wins]), 2) if wins else 0,
            "avg_loss": round(np.mean([t["pnl_pct"] for t in losses]), 2) if losses else 0,
            "best_trade": max(day_trades, key=lambda x: x["pnl_pct"]) if day_trades else None,
            "worst_trade": min(day_trades, key=lambda x: x["pnl_pct"]) if day_trades else None,
            "trades": day_trades
        }
    
    def generate_daily_report(self, date_str: str = None) -> str:
        stats = self.get_daily_stats(date_str)
        
        if stats["total"] == 0:
            date_display = date_str or "昨日"
            return f"📊 *Alpha Oracle 每日战报* ({date_display})\n" \
                   f"━━━━━━━━━━━━━━━━\n" \
                   f"📭 今日无交易记录"
        
        trades_txt = ""
        for t in stats["trades"][:10]:
            emoji = "✅" if t["pnl_pct"] > 0 else "❌"
            trades_txt += f"{emoji} {t['coin']} {t['side']}  {t['exit_type']}  {t['pnl_pct']:+.2f}%\n"
        
        if len(stats["trades"]) > 10:
            trades_txt += f"... 等共 {stats['total']} 笔\n"
        
        best = stats['best_trade']
        worst = stats['worst_trade']
        
        return (
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
            f"🏆 最佳：{best['coin']} {best['side']} {best['pnl_pct']:+.2f}%" if best else ""
            f"\n💔 最差：{worst['coin']} {worst['side']} {worst['pnl_pct']:+.2f}%" if worst else ""
            f"\n━━━━━━━━━━━━━━━━\n"
            f"📋 *交易明细：*\n"
            f"{trades_txt}"
            f"━━━━━━━━━━━━━━━━\n"
            f"💡 *保持纪律，继续加油！*"
        )

trade_history = TradeHistory()

# ─────────────────────────────────────────────────────────
# 4. 数据抓取
# ─────────────────────────────────────────────────────────
def fetch_okx(instId: str, tf: str = "15m", limit: int = 150):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0":
            return None
        df = pd.DataFrame(res["data"], columns=["ts","o","h","l","c","v","volCcy","volCcyQuote","confirm"])
        df[["o","h","l","c","v"]] = df[["o","h","l","c","v"]].astype(float)
        df = df[df["confirm"] == "1"].iloc[::-1].reset_index(drop=True)
        return df if len(df) >= 30 else None
    except Exception as e:
        logging.warning(f"[{instId}/{tf}] Fetch: {e}")
        return None

def fetch_ticker_price(instId: str) -> float:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={instId}", timeout=5).json()
        if res.get("code") == "0" and res.get("data"):
            return float(res["data"][0]["last"])
        return 0.0
    except:
        return 0.0

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        return float(res["data"][0]["fundingRate"]) if res.get("data") else 0.0
    except:
        return 0.0

def fetch_ls_ratio(symbol: str) -> tuple:
    try:
        base = symbol.split("-")[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base}", timeout=5).json()
        if res.get("data"):
            r = float(res["data"][0]["ratio"])
            return r, f"{r:.2f}"
        return 1.0, "N/A"
    except:
        return 1.0, "N/A"

def fetch_order_book(instId: str, depth: int = 20) -> tuple:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5).json()
        if res.get("code") != "0" or not res.get("data"):
            return 1.0, "⚪ 盘口均衡"
        data = res["data"][0]
        bid_vol = sum(float(b[1]) for b in data["bids"])
        ask_vol = sum(float(a[1]) for a in data["asks"]) or 1e-10
        ratio = bid_vol / ask_vol
        if ratio >= 1.30: label = f"🟢 买盘强势 ({ratio:.2f})"
        elif ratio >= 1.05: label = f"🟡 买盘略强 ({ratio:.2f})"
        elif ratio >= 0.95: label = f"⚪ 盘口均衡 ({ratio:.2f})"
        elif ratio >= 0.77: label = f"🟡 卖盘略强 ({ratio:.2f})"
        else: label = f"🔴 卖盘强势 ({ratio:.2f})"
        return ratio, label
    except:
        return 1.0, "⚪ 盘口均衡"

# ─────────────────────────────────────────────────────────
# 5. 技术指标
# ─────────────────────────────────────────────────────────
def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df["h"] - df["l"]
    hc = np.abs(df["h"] - df["c"].shift())
    lc = np.abs(df["l"] - df["c"].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calculate_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> tuple:
    if len(df) < period + 2:
        return 0, "⚪ 未知"
    h = df["h"].values.astype(float)
    l = df["l"].values.astype(float)
    c = df["c"].values.astype(float)
    n = len(df)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1]*(period-1)+tr[i]) / period
    hl2 = (h+l)/2.0
    bu = hl2 - mult*atr
    bd = hl2 + mult*atr
    fu = np.zeros(n); fd = np.zeros(n)
    trend = np.ones(n, dtype=int)
    fu[period]=bu[period]; fd[period]=bd[period]
    for i in range(period+1, n):
        fu[i]=bu[i] if bu[i]>fu[i-1] or c[i-1]<fu[i-1] else fu[i-1]
        fd[i]=bd[i] if bd[i]<fd[i-1] or c[i-1]>fd[i-1] else fd[i-1]
        if trend[i-1]==-1 and c[i]>fd[i-1]: trend[i]=1
        elif trend[i-1]==1 and c[i]<fu[i-1]: trend[i]=-1
        else: trend[i]=trend[i-1]
    if trend[-1]==1: return 1, "🟢 多头"
    if trend[-1]==-1: return -1, "🔴 空头"
    return 0, "⚪ 未知"

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["c"].diff()
    gain = delta.where(delta>0, 0).rolling(period).mean()
    loss = (-delta.where(delta<0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100/(1+rs))

def calculate_adx(df: pd.DataFrame, period: int = 14) -> tuple:
    if len(df) < period*2+2:
        return 0.0, 0.0, 0.0
    h = df["h"].values.astype(float)
    l = df["l"].values.astype(float)
    c = df["c"].values.astype(float)
    n = len(df)
    tr = np.zeros(n); pdm = np.zeros(n); mdm = np.zeros(n)
    for i in range(1, n):
        h_diff = h[i]-h[i-1]
        l_diff = l[i-1]-l[i]
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        pdm[i] = h_diff if h_diff>l_diff and h_diff>0 else 0
        mdm[i] = l_diff if l_diff>h_diff and l_diff>0 else 0
    atr_w = np.zeros(n); p_w = np.zeros(n); m_w = np.zeros(n)
    atr_w[period]=tr[1:period+1].sum()
    p_w[period]=pdm[1:period+1].sum()
    m_w[period]=mdm[1:period+1].sum()
    for i in range(period+1, n):
        atr_w[i] = atr_w[i-1]-atr_w[i-1]/period+tr[i]
        p_w[i] = p_w[i-1]-p_w[i-1]/period+pdm[i]
        m_w[i] = m_w[i-1]-m_w[i-1]/period+mdm[i]
    plus_di = 100*p_w/(atr_w+1e-10)
    minus_di = 100*m_w/(atr_w+1e-10)
    dx = 100*np.abs(plus_di-minus_di)/(plus_di+minus_di+1e-10)
    adx = np.zeros(n)
    s = 2*period
    if s < n:
        adx[s]=dx[period+1:s+1].mean()
        for i in range(s+1, n):
            adx[i]=(adx[i-1]*(period-1)+dx[i])/period
    return float(adx[-1]), float(plus_di[-1]), float(minus_di[-1])

def detect_market_regime(df: pd.DataFrame) -> dict:
    adx, pdi, mdi = calculate_adx(df, ADX_PERIOD)
    if adx < 20: regime = "📊 震荡市"; sc = 0.4
    elif adx < 25: regime = "📈 弱趋势"; sc = 0.6
    elif adx < 40: regime = "🚀 强趋势"; sc = 0.9
    else: regime = "🔥 极强趋势"; sc = 1.0
    trend_dir = "🟢 上升趋势" if pdi > mdi else "🔴 下降趋势"
    return {"regime": regime, "adx": adx, "trend_dir": trend_dir, "score": sc, "plus_di": pdi, "minus_di": mdi}

def check_extreme_volatility(df: pd.DataFrame) -> tuple:
    atr = calculate_atr(df)
    price = df["c"].iloc[-1]
    ratio = atr / (price + 1e-10)
    if ratio > VOLATILITY_HARD_LIMIT:
        return False, f"🚫 极端波动 ATR={ratio*100:.2f}%"
    return True, f"✅ 波动正常 ATR={ratio*100:.2f}%"

def calculate_dynamic_sl(entry: float, side: str, atr: float, support: float = None, resistance: float = None) -> float:
    base = entry - atr*1.5 if side=="LONG" else entry + atr*1.5
    if side=="LONG" and support:
        if abs(entry - support) < atr*2.5:
            base = min(base, support - atr*0.5)
    if side=="SHORT" and resistance:
        if abs(resistance - entry) < atr*2.5:
            base = max(base, resistance + atr*0.5)
    min_dist = atr * 1.5
    if abs(entry - base) < min_dist:
        base = entry - min_dist if side=="LONG" else entry + min_dist
    return base

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple:
    data = df.tail(lookback).reset_index(drop=True)
    sh_p, sl_p = [], []
    for i in range(n, len(data)-n):
        wh = data["h"].iloc[i-n:i+n+1]
        wl = data["l"].iloc[i-n:i+n+1]
        if data["h"].iloc[i] == wh.max():
            sh_p.append(data["h"].iloc[i])
        if data["l"].iloc[i] == wl.min():
            sl_p.append(data["l"].iloc[i])
    return sorted(set(sh_p)), sorted(set(sl_p))

def detect_bos_choch(df: pd.DataFrame, side: str) -> tuple:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=80)
    price = df["c"].iloc[-1]
    atr = calculate_atr(df)
    result, score = "⚪ 无明显结构", 0.0
    if side=="LONG":
        if sl and df["l"].iloc[-4:-1].min() < sl[-1]-atr*0.1 and price>sl[-1]:
            result, score = f"✅ CHoCH 扫低反弹 @ {sl[-1]:.4f}", 0.90
        elif sh and not score:
            if price>sh[-1]:
                result, score = f"✅ BOS 向上突破 {sh[-1]:.4f}", 0.80
            elif len(sh)>=2 and price>sh[-2]:
                result, score = f"🟡 CHoCH 潜在转折 {sh[-2]:.4f}", 0.55
    else:
        if sh and df["h"].iloc[-4:-1].max() > sh[-1]+atr*0.1 and price<sh[-1]:
            result, score = f"✅ CHoCH 扫高回落 @ {sh[-1]:.4f}", 0.90
        elif sl and not score:
            if price<sl[-1]:
                result, score = f"✅ BOS 向下跌破 {sl[-1]:.4f}", 0.80
            elif len(sl)>=2 and price<sl[-2]:
                result, score = f"🟡 CHoCH 潜在转折 {sl[-2]:.4f}", 0.55
    return result, score

def detect_market_structure(df: pd.DataFrame, side: str) -> str:
    sh, sl, _, _ = find_swing_points(df, n=3, lookback=60)
    has_w = len(sl)>=2 and sl[-2]>0 and abs(sl[-2]-sl[-1])/sl[-2]<0.015
    has_m = len(sh)>=2 and sh[-2]>0 and abs(sh[-2]-sh[-1])/sh[-2]<0.015
    if side=="LONG":
        if has_w: return "W 底反转 📐"
        if has_m: return "M 头压制 ⚠️"
    else:
        if has_m: return "M 头反转 📐"
        if has_w: return "W 底支撑 ⚠️"
    recent = df.tail(20)
    slope = (recent["c"].iloc[-1]-recent["c"].iloc[0])/(recent["c"].iloc[0]+1e-10)
    if slope>0.025: return "上升趋势延续 📈"
    if slope<-0.025: return "下降趋势延续 📉"
    return "区间盘整 ↔️"

def find_liquidity_pools(df: pd.DataFrame, side: str, lookback: int = 60) -> dict:
    sh, sl, _, _ = find_swing_points(df, n=2, lookback=lookback)
    price = df["c"].iloc[-1]
    atr = calculate_atr(df)
    res = dict(pools=[], sweep_detected=False, sweep_desc="", sweep_score=0.0, eqh=None, eql=None, nearest_bsl=None, nearest_ssl=None)
    for i in range(len(sh)-1, 0, -1):
        if abs(sh[i]-sh[i-1])/(sh[i-1]+1e-10)<0.003:
            res["eqh"]=(sh[i-1]+sh[i])/2
            res["pools"].append(f"🔴 EQH等高 {res['eqh']:.4f}")
            break
    for i in range(len(sl)-1, 0, -1):
        if abs(sl[i]-sl[i-1])/(sl[i-1]+1e-10)<0.003:
            res["eql"]=(sl[i-1]+sl[i])/2
            res["pools"].append(f"🟢 EQL等低 {res['eql']:.4f}")
            break
    bsl_c=[h for h in sh if h>price]
    ssl_c=[l for l in sl if l<price]
    if bsl_c: res["nearest_bsl"]=min(bsl_c)
    if ssl_c: res["nearest_ssl"]=max(ssl_c)
    recent=df.tail(5)
    if side=="LONG":
        for lvl,is_eq in ([(res["eql"],True)] if res["eql"] else []) + ([(res["nearest_ssl"],False)] if res["nearest_ssl"] else []):
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k=recent.iloc[i]
                if k["l"]<lvl-atr*0.05 and k["c"]>lvl:
                    wick=(lvl-k["l"])/(atr+1e-10)
                    res["sweep_detected"]=True
                    res["sweep_desc"]=f"{'🔥 EQL' if is_eq else '✅ SSL'}扫除反弹！低扫{k['l']:.4f}→收{k['c']:.4f}"
                    res["sweep_score"]=0.95 if is_eq else min(0.55+wick*0.08,0.90)
                    break
            if res["sweep_detected"]: break
    else:
        for lvl,is_eq in ([(res["eqh"],True)] if res["eqh"] else []) + ([(res["nearest_bsl"],False)] if res["nearest_bsl"] else []):
            for i in range(len(recent)-1, max(len(recent)-4,0), -1):
                k=recent.iloc[i]
                if k["h"]>lvl+atr*0.05 and k["c"]<lvl:
                    wick=(k["h"]-lvl)/(atr+1e-10)
                    res["sweep_detected"]=True
                    res["sweep_desc"]=f"{'🔥 EQH' if is_eq else '✅ BSL'}扫除回落！高扫{k['h']:.4f}→收{k['c']:.4f}"
                    res["sweep_score"]=0.95 if is_eq else min(0.55+wick*0.08,0.90)
                    break
            if res["sweep_detected"]: break
    return res

def find_order_blocks(df: pd.DataFrame, side: str, lookback: int = 60) -> list:
    data=df.tail(lookback).reset_index(drop=True)
    obs=[]
    price=data["c"].iloc[-1]
    atr=calculate_atr(data)
    for i in range(2, len(data)-3):
        c=data.iloc[i]
        if side=="LONG":
            if c["c"]<c["o"]:
                mv=data["h"].iloc[i+1:i+4].max()-c["h"]
                if mv>atr*1.5:
                    ob=dict(high=c["h"],low=c["l"],mid=(c["h"]+c["l"])/2,strength=mv/(atr+1e-10))
                    if ob["high"]<price*1.005: obs.append(ob)
        else:
            if c["c"]>c["o"]:
                mv=c["l"]-data["l"].iloc[i+1:i+4].min()
                if mv>atr*1.5:
                    ob=dict(high=c["h"],low=c["l"],mid=(c["h"]+c["l"])/2,strength=mv/(atr+1e-10))
                    if ob["low"]>price*0.995: obs.append(ob)
    obs.sort(key=lambda x:x["strength"],reverse=True)
    return obs[:2]

def find_fvg(df: pd.DataFrame, side: str, lookback: int = 40) -> list:
    data=df.tail(lookback).reset_index(drop=True)
    fvgs=[]
    price=data["c"].iloc[-1]
    for i in range(2, len(data)):
        if side=="LONG":
            bot,top=data["h"].iloc[i-2],data["l"].iloc[i]
            if top>bot and bot<price:
                fvgs.append(dict(top=top,bottom=bot,mid=(top+bot)/2,size=top-bot))
        else:
            top,bot=data["l"].iloc[i-2],data["h"].iloc[i]
            if bot<top and top>price:
                fvgs.append(dict(top=top,bottom=bot,mid=(top+bot)/2,size=top-bot))
    return fvgs[-2:] if fvgs else []

def check_ob_fvg_entry(df: pd.DataFrame, side: str, atr: float) -> tuple:
    price=df["c"].iloc[-1]
    obs=find_order_blocks(df,side)
    fvgs=find_fvg(df,side)
    at_ob=at_fvg=False
    ob_d="📍 无OB"
    fvg_d="📍 无FVG"
    ez=price
    for ob in obs:
        if ob["low"]-atr*0.5<=price<=ob["high"]+atr*0.5:
            at_ob=True
            ob_d=f"✅ 在OB [{ob['low']:.4f}~{ob['high']:.4f}] 强{ob['strength']:.1f}x"
            ez=ob["mid"]
            break
        else:
            ob_d=f"📍 OB [{ob['low']:.4f}~{ob['high']:.4f}]"
    for fvg in reversed(fvgs):
        if fvg["bottom"]-atr*0.3<=price<=fvg["top"]+atr*0.3:
            at_fvg=True
            fvg_d=f"✅ 在FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
            if not at_ob:
                ez=fvg["mid"]
            break
        else:
            fvg_d=f"📍 FVG [{fvg['bottom']:.4f}~{fvg['top']:.4f}]"
    return at_ob, at_fvg, ob_d, fvg_d, ez

def detect_premium_discount(df: pd.DataFrame, side: str) -> tuple:
    sh,sl,_,_=find_swing_points(df,n=3,lookback=50)
    price=df["c"].iloc[-1]
    if not sh or not sl:
        return "⚪ 无法判断",0.5
    hi=max(sh[-2:]) if len(sh)>=2 else sh[-1]
    lo=min(sl[-2:]) if len(sl)>=2 else sl[-1]
    rng=hi-lo
    if rng<=0:
        return "⚪ 无法判断",0.5
    fib=(price-lo)/rng
    if side=="LONG":
        if fib<=0.35: return f"✅ Discount {fib*100:.0f}%",1.0
        elif fib<=0.5: return f"🟡 均衡偏低 {fib*100:.0f}%",0.6
        elif fib<=0.65: return f"🟡 均衡偏高 {fib*100:.0f}%",0.3
        else: return f"❌ Premium {fib*100:.0f}%",0.0
    else:
        if fib>=0.65: return f"✅ Premium {fib*100:.0f}%",1.0
        elif fib>=0.5: return f"🟡 均衡偏高 {fib*100:.0f}%",0.6
        elif fib>=0.35: return f"🟡 均衡偏低 {fib*100:.0f}%",0.3
        else: return f"❌ Discount {fib*100:.0f}%",0.0

def detect_crossline(df: pd.DataFrame, lookback: int = 15) -> dict | None:
    for i in range(len(df)-1, max(len(df)-lookback-1,0), -1):
        k=df.iloc[i]
        body=abs(k["c"]-k["o"])
        rng=k["h"]-k["l"]+1e-10
        if body<CROSSLINE_BODY_RATIO*rng:
            uw=k["h"]-max(k["c"],k["o"])
            dw=min(k["c"],k["o"])-k["l"]
            pot="SHORT" if uw>dw*1.5 else ("LONG" if dw>uw*1.5 else "NEUTRAL")
            dist=len(df)-1-i
            return dict(price=k["c"],high=k["h"],low=k["l"],body_ratio=body/rng,potential_side=pot,distance=dist,desc=f"🎯 十字线@{k['c']:.4f}（潜在:{pot}，{dist}根前）")
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> tuple:
    if len(df)<8:
        return False,0.0,"⚪ 数据不足"
    recent=df.tail(8)
    vol_ma=df["v"].tail(20).mean()
    vol_sc=recent.iloc[-1]["v"]/(vol_ma+1e-10)
    if vol_sc<SWEEP_VOLUME_RATIO:
        return False,0.0,f"⚪ 量能不足({vol_sc:.1f}x)"
    moves=0
    for i in range(len(recent)-1,0,-1):
        if side=="LONG" and recent["c"].iloc[i]>recent["c"].iloc[i-1]:
            moves+=1
        elif side=="SHORT" and recent["c"].iloc[i]<recent["c"].iloc[i-1]:
            moves+=1
        else:
            break
    if moves>=SWEEP_CONSECUTIVE_MOVES:
        return True,min(vol_sc/3.0,1.0),f"⚡ 主动扫单！连续{moves}根+{vol_sc:.1f}x"
    return False,0.0,f"⚪ 无连续扫单({moves}根)"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    if len(df)<6:
        return False
    recent=df.tail(6)
    vol_ma=df["v"].tail(20).mean()
    mv=abs(recent["c"].iloc[-1]-recent["c"].iloc[0])/(recent["c"].iloc[0]+1e-10)
    return mv>=0.005 and recent["v"].iloc[-1]<0.75*vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> tuple:
    if len(df)<15:
        return False,"⚪ 无吸收"
    recent=df.tail(5)
    vol_ma=df["v"].tail(20).mean()
    avg3=recent["v"].iloc[-3:].mean()
    chg=abs(recent["c"].iloc[-1]-recent["c"].iloc[-4])/(recent["c"].iloc[-4]+1e-10)
    if avg3>ABSORPTION_VOL_MULTIPLIER*vol_ma and chg<ABSORPTION_PRICE_THRESHOLD:
        return True,f"🔄 吸收！量{avg3/vol_ma:.1f}x 价动{chg*100:.2f}%"
    return False,"⚪ 无吸收"

def calculate_cvd(df: pd.DataFrame, periods: int = 50) -> tuple:
    data = df.tail(periods).copy()
    delta = np.where(data["c"]>data["o"], data["v"], np.where(data["c"]<data["o"], -data["v"], 0))
    cvd = np.cumsum(delta)
    cur = cvd[-1]
    slope = cur - (cvd[-10] if len(cvd)>=10 else cvd[0])
    if slope>0 and cur>0: lb,sc = f"🟢 买盘累积 CVD+{cur:,.0f}", 1.0
    elif slope>0 and cur<0: lb,sc = f"🟡 CVD底部翻正", 0.65
    elif slope<0 and cur<0: lb,sc = f"🔴 卖盘累积 CVD{cur:,.0f}", 1.0
    elif slope<0 and cur>0: lb,sc = f"🟡 CVD顶部翻负", 0.65
    else: lb,sc = f"⚪ CVD持平", 0.3
    return cur, slope, lb, sc

def interpret_ls_ratio(ratio: float, side: str) -> tuple:
    if ratio>=2.5: senti=f"🔴 极度多头拥挤({ratio:.2f})"
    elif ratio>=1.8: senti=f"🟠 多头拥挤({ratio:.2f})"
    elif ratio>=1.2: senti=f"⚪ 略偏多头({ratio:.2f})"
    elif ratio>=0.8: senti=f"⚪ 均衡({ratio:.2f})"
    elif ratio>=0.5: senti=f"🟠 空头拥挤({ratio:.2f})"
    else: senti=f"🟢 极度空头拥挤({ratio:.2f})"
    if side=="LONG": sc=1.0 if ratio<0.8 else(0.7 if ratio<1.2 else(0.4 if ratio<1.8 else 0.1))
    else: sc=1.0 if ratio>2.0 else(0.7 if ratio>1.5 else(0.4 if ratio>1.0 else 0.1))
    return sc, senti

def interpret_funding_rate(fr: float, side: str) -> tuple:
    p=fr*100
    if side=="LONG":
        if fr<-0.0003: return 1.0,f"✅ 费率极佳{p:.4f}%"
        elif fr<0.0001: return 0.8,f"✅ 费率友善{p:.4f}%"
        elif fr<0.0003: return 0.5,f"⚠️ 费率尚可{p:.4f}%"
        elif fr<0.0008: return 0.2,f"❌ 费率不佳{p:.4f}%"
        else: return 0.0,f"🚫 费率禁入{p:.4f}%"
    else:
        if fr>0.0008: return 1.0,f"✅ 费率极佳{p:.4f}%"
        elif fr>0.0003: return 0.8,f"✅ 费率友善{p:.4f}%"
        elif fr>0.0001: return 0.5,f"⚠️ 费率尚可{p:.4f}%"
        elif fr>-0.0003: return 0.2,f"❌ 费率不佳{p:.4f}%"
        else: return 0.0,f"🚫 费率禁入{p:.4f}%"

def check_ob_direction(side: str, ob_r: float) -> tuple:
    if side=="LONG":
        if ob_r>=1.30: return 1.0,f"✅ 买盘强势({ob_r:.2f})"
        elif ob_r>=1.05: return 0.7,f"✅ 买盘略强({ob_r:.2f})"
        elif ob_r>=0.95: return 0.3,f"⚪ 盘口均衡({ob_r:.2f})"
        else: return 0.0,f"❌ 卖盘主导({ob_r:.2f})"
    else:
        if ob_r<=0.77: return 1.0,f"✅ 卖盘强势({ob_r:.2f})"
        elif ob_r<=0.95: return 0.7,f"✅ 卖盘略强({ob_r:.2f})"
        elif ob_r<=1.05: return 0.3,f"⚪ 盘口均衡({ob_r:.2f})"
        else: return 0.0,f"❌ 买盘主导({ob_r:.2f})"

def detect_pa(df: pd.DataFrame, side: str) -> tuple:
    sigs=[]
    for i in range(len(df)-1, max(len(df)-6,0), -1):
        k=df.iloc[i]
        body=abs(k["c"]-k["o"])
        rng=k["h"]-k["l"]+1e-10
        uw=k["h"]-max(k["c"],k["o"])
        dw=min(k["c"],k["o"])-k["l"]
        bp=body/rng
        if side=="SHORT" and uw>=body*2.0 and dw<=body*0.5:
            sigs.append(f"空头流星线({min(uw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="LONG" and dw>=body*2.0 and uw<=body*0.5:
            sigs.append(f"多头锤子线({min(dw/(body+1e-10),5):.1f}x)@{k['c']:.4f}")
        if side=="SHORT" and uw/rng>0.40 and k["c"]<k["o"]:
            sigs.append(f"压力拒绝(上影{uw/rng*100:.0f}%)@{k['c']:.4f}")
        if side=="LONG" and dw/rng>0.40 and k["c"]>k["o"]:
            sigs.append(f"支撑拒绝(下影{dw/rng*100:.0f}%)@{k['c']:.4f}")
        if bp>=0.70 and ((side=="LONG" and k["c"]>k["o"]) or (side=="SHORT" and k["c"]<k["o"])):
            sigs.append(f"{'多' if side=='LONG' else '空'}头动量棒({bp*100:.0f}%)@{k['c']:.4f}")
    sigs=sigs[:3]
    sc=0.6 if len(sigs)>=3 else(0.4 if len(sigs)>=2 else(0.2 if sigs else 0.0))
    last=df.iloc[-1]
    body=abs(last["c"]-last["o"])
    rng=last["h"]-last["l"]+1e-10
    if body/rng>0.70: sc+=0.20
    if (side=="LONG" and last["c"]>last["o"]) or (side=="SHORT" and last["c"]<last["o"]): sc+=0.20
    sc=min(sc,1.0)
    lb="✅ 强PA" if sc>=0.65 else("⚠️ 弱PA" if sc>=0.40 else "⛔ 弱PA")
    return sc*100, lb, sigs

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    zones=[]
    vm=df["v"].rolling(20).mean()
    vs=df["v"].rolling(20).std()
    for i in range(max(len(df)-10,0), len(df)):
        if df["v"].iloc[i]>vm.iloc[i]+2*vs.iloc[i]:
            if df["c"].iloc[i]>df["o"].iloc[i] and side=="LONG":
                zones.append(f"🔵 主力吸筹 {df['c'].iloc[i]:.4f}")
            elif df["c"].iloc[i]<df["o"].iloc[i] and side=="SHORT":
                zones.append(f"🔴 主力派发 {df['c'].iloc[i]:.4f}")
    hi=df["h"].iloc[-20:].max()
    lo=df["l"].iloc[-20:].min()
    zones.append(f"{'🔴 多头清算' if side=='SHORT' else '🔵 空头清算'} {hi if side=='SHORT' else lo:.4f}")
    return zones[:2]

def calculate_score(p: dict) -> tuple:
    sc=0.0
    bd=[]
    side=p["side"]
    htf=p.get("htf_trend","UNKNOWN")
    if htf==side: sc+=20; bd.append("HTF+20")
    elif htf in("NEUTRAL","UNKNOWN"): sc+=8; bd.append("HTF+8")
    else: sc+=0; bd.append("HTF+0")
    at_ob=p.get("at_ob",False)
    at_fvg=p.get("at_fvg",False)
    if at_ob and at_fvg: sc+=18; bd.append("OB+FVG+18")
    elif at_ob: sc+=15; bd.append("OB+15")
    elif at_fvg: sc+=12; bd.append("FVG+12")
    pts=round(p.get("sweep_score",0)*18)
    sc+=pts
    if pts: bd.append(f"扫除+{pts}")
    pts=round(p.get("active_sweep_score",0)*13)
    sc+=pts
    if pts: bd.append(f"主动扫+{pts}")
    pts=round(p.get("crossline_score",0)*8)
    sc+=pts
    if pts: bd.append(f"十字+{pts}")
    pts=round(p.get("absorption_score",0)*7)
    sc+=pts
    if pts: bd.append(f"吸收+{pts}")
    pts=round(p.get("cvd_score",0)*12)
    sc+=pts
    bd.append(f"CVD+{pts}")
    pts=round(p.get("ls_score",0)*8)
    sc+=pts
    bd.append(f"LS+{pts}")
    pts=round(p.get("fr_score",0)*5)
    sc+=pts
    bd.append(f"FR+{pts}")
    pts=round(p.get("ob_dir_score",0)*5)
    sc+=pts
    bd.append(f"盘口+{pts}")
    if p.get("bos_score",0)>=0.75: sc+=5; bd.append("BOS+5")
    pts=round(p.get("trend_4h_score",0)*5)
    if pts: sc+=pts; bd.append(f"4H+{pts}")
    if p.get("has_rsi_divergence",False): sc+=5; bd.append("RSI+5")
    pts=round(p.get("btc_score",0)*3)
    if pts: sc+=pts; bd.append(f"BTC+{pts}")
    adx_b=p.get("adx_bonus",0)
    if adx_b: sc+=adx_b; bd.append(f"ADX+{adx_b}")
    if p.get("pd_score",0)>=0.7: sc+=3; bd.append("PD+3")
    if htf not in(side,"NEUTRAL","UNKNOWN"): sc-=15; bd.append("HTF逆-15")
    if p.get("fr_score",1)==0.0: sc-=10; bd.append("FR禁-10")
    if p.get("ob_dir_score",1)==0.0: sc-=10; bd.append("盘口反-10")
    sc=max(0,min(round(sc),100))
    if sc>=88: grade="🏆 A+ 极强"
    elif sc>=75: grade="✅ A  强力"
    elif sc>=65: grade="⚠️ B+ 观望"
    elif sc>=55: grade="⚠️ B  偏弱"
    else: grade="❌ C  跳过"
    return sc, grade, bd

# ─────────────────────────────────────────────────────────
# 6. 主扫描逻辑
# ─────────────────────────────────────────────────────────
def scan_timeframe(instId: str, tf: str, htf_trend: str, fr: float, ls_f: float, ls_str: str, ob_r: float, _cache: dict) -> list:
    df = fetch_okx(instId, tf=tf, limit=150)
    if df is None or len(df) < 50:
        return []
    vol_ok, vol_msg = check_extreme_volatility(df)
    if not vol_ok:
        logging.info(f"  [{instId}/{tf}] {vol_msg}")
        return []
    atr = calculate_atr(df)
    _, st_lb = calculate_supertrend(df)
    regime = detect_market_regime(df)
    cl = detect_crossline(df)
    abs_b, abs_d = detect_absorption(df, "LONG")
    opportunities = []
    for side in ["LONG", "SHORT"]:
        if htf_trend not in("UNKNOWN","NEUTRAL") and htf_trend!=side:
            continue
        ob_dir_sc, ob_dir_lb = check_ob_direction(side, ob_r)
        if ob_dir_sc == 0.0:
            continue
        fr_sc, fr_lb = interpret_funding_rate(fr, side)
        if fr_sc == 0.0:
            continue
        if detect_fishing_trap(df, side):
            continue
        cvd_cur, cvd_sl, cvd_lb, cvd_sc_raw = calculate_cvd(df)
        cvd_aligned = (side=="LONG" and cvd_sl>0) or (side=="SHORT" and cvd_sl<0)
        eff_cvd_sc = cvd_sc_raw if cvd_aligned else cvd_sc_raw*0.25
        liq = find_liquidity_pools(df, side)
        bos_desc, bos_sc = detect_bos_choch(df, side)
        at_ob,at_fvg,ob_d,fvg_d,ez = check_ob_fvg_entry(df, side, atr)
        pd_lb, pd_sc = detect_premium_discount(df, side)
        pa_sc,pa_lb,pa_sigs = detect_pa(df, side)
        structure = detect_market_structure(df, side)
        whale_zones = detect_whale_zones(df, side)
        ls_sc, ls_lb = interpret_ls_ratio(ls_f, side)
        as_bool,as_sc,as_d = detect_active_sweep(df, side)
        cl_sc = 0.0
        if cl:
            pot=cl["potential_side"]
            if pot==side or pot=="NEUTRAL":
                cl_sc = max(0.0, 1.0 - cl["distance"]/10) * 0.6 + 0.4
        t4h_sc, t4h_lb = 0.5, "⚪ 4H数据不足"
        btc_sc, btc_lb = 0.5, "⚪ BTC数据不足"
        adx_bonus, adx_lb = 0, "⚪ ADX无加分"
        ab_sc = 0.8 if abs_b else 0.0
        params = dict(
            side=side, htf_trend=htf_trend, at_ob=at_ob, at_fvg=at_fvg,
            sweep_score=liq["sweep_score"], active_sweep_score=as_sc,
            crossline_score=cl_sc, absorption_score=ab_sc,
            cvd_score=eff_cvd_sc, ls_score=ls_sc, fr_score=fr_sc,
            ob_dir_score=ob_dir_sc, bos_score=bos_sc,
            trend_4h_score=t4h_sc, has_rsi_divergence=False,
            btc_score=btc_sc, adx_bonus=adx_bonus, pd_score=pd_sc,
        )
        score, grade, bd = calculate_score(params)
        if score < SETUP_SCORE_THRESHOLD:
            logging.info(f"  [{instId}/{tf}/{side}] {score}分 < {SETUP_SCORE_THRESHOLD}，跳过")
            continue
        price = df["c"].iloc[-1]
        sh,sl,_,_ = find_swing_points(df, n=2, lookback=30)
        support = max([s for s in sl if s<price], default=None)
        resistance = min([h for h in sh if h>price], default=None)
        if liq["sweep_detected"]:
            entry = price
        elif at_ob or at_fvg:
            entry = ez
        elif cl:
            entry = cl["low"] if side=="LONG" else cl["high"]
        elif side=="LONG" and liq["nearest_ssl"]:
            entry = liq["nearest_ssl"]*1.001
        elif side=="SHORT" and liq["nearest_bsl"]:
            entry = liq["nearest_bsl"]*0.999
        else:
            entry = price
        sl_price = calculate_dynamic_sl(entry, side, atr, support, resistance)
        risk = abs(entry - sl_price)
        tp1 = entry+risk if side=="LONG" else entry-risk
        tp2 = entry+risk*2.5 if side=="LONG" else entry-risk*2.5
        tp3 = entry+risk*4.0 if side=="LONG" else entry-risk*4.0
        opp = dict(
            instId=instId, side=side, tf=tf, entry=entry, sl=sl_price,
            tp1=tp1, tp2=tp2, tp3=tp3, price=price, atr=atr,
            structure=structure, bos_desc=bos_desc, at_ob=at_ob, at_fvg=at_fvg,
            ob_d=ob_d, fvg_d=fvg_d, pd_lb=pd_lb, liq=liq, crossline=cl,
            as_bool=as_bool, as_d=as_d, abs_bool=abs_b, abs_desc=abs_d,
            cvd_lb=cvd_lb, ls_str=ls_str, ls_lb=ls_lb, fr_lb=fr_lb,
            ob_dir_lb=ob_dir_lb, pa_sc=pa_sc, pa_lb=pa_lb, pa_sigs=pa_sigs,
            whale_zones=whale_zones, htf_trend=htf_trend, st_lb=st_lb,
            regime=regime, t4h_lb=t4h_lb, btc_lb=btc_lb, adx_lb=adx_lb,
            vol_msg=vol_msg, score=score, grade=grade, breakdown=bd,
            lev="10x~20x" if atr/price<0.015 else "3x~5x",
        )
        opportunities.append(opp)
    return opportunities

def scan_for_opportunity(instId: str) -> list:
    _cache = {}
    htf_df = fetch_okx(instId, tf="1H", limit=60)
    htf_trend_str = "UNKNOWN"
    if htf_df is not None:
        v,_ = calculate_supertrend(htf_df)
        htf_trend_str = "LONG" if v==1 else ("SHORT" if v==-1 else "NEUTRAL")
        _cache[f"{instId}_1H"] = htf_df
    fr = fetch_funding_rate(instId)
    ls_f, ls_str = fetch_ls_ratio(instId)
    ob_r, _ = fetch_order_book(instId)
    all_opps = []
    for tf in SCAN_TIMEFRAMES:
        try:
            opps = scan_timeframe(instId, tf, htf_trend_str, fr, ls_f, ls_str, ob_r, _cache)
            all_opps.extend(opps)
        except Exception as e:
            logging.error(f"  [{instId}/{tf}] {e}")
    seen={}
    for opp in all_opps:
        k=f"{opp['side']}_{opp['tf']}"
        if k not in seen or opp["score"]>seen[k]["score"]:
            seen[k]=opp
    return list(seen.values())

# ─────────────────────────────────────────────────────────
# 7. 信号格式化
# ─────────────────────────────────────────────────────────
def format_signal(opp: dict) -> str:
    coin = opp["instId"].split("-")[0]
    arrow = "🟢" if opp["side"]=="LONG" else "🔴"
    st = "多单 (LONG)" if opp["side"]=="LONG" else "空单 (SHORT)"
    htf_e = {"LONG":"🟢","SHORT":"🔴","NEUTRAL":"⚪","UNKNOWN":"⚪"}.get(opp["htf_trend"],"⚪")
    liq = opp["liq"]
    entry = opp["entry"]
    sl_pct = abs(entry - opp["sl"]) / entry * 100
    tp1_pct = abs(opp["tp1"] - entry) / entry * 100
    tp2_pct = abs(opp["tp2"] - entry) / entry * 100
    tp3_pct = abs(opp["tp3"] - entry) / entry * 100
    sign = "+" if opp["side"]=="LONG" else "-"
    sl_sign = "-" if opp["side"]=="LONG" else "+"
    top_bd = [x for x in opp["breakdown"] if not x.endswith("+0") and "0分" not in x][:6]
    bd_line = "  ".join(top_bd)
    triggers = []
    if liq["sweep_detected"]:
        triggers.append(f"💧 {liq['sweep_desc']}")
    if opp["at_ob"]:
        triggers.append(f"🟦 {opp['ob_d']}")
    if opp["at_fvg"]:
        triggers.append(f"🟩 {opp['fvg_d']}")
    if opp["bos_desc"] != "⚪ 无明显结构":
        triggers.append(f"🏗 {opp['bos_desc']}")
    if opp["as_bool"]:
        triggers.append(f"⚡ {opp['as_d']}")
    if not triggers:
        triggers.append("⚪ 等待进场区确认")
    trigger_txt = "\n".join(f"  • {t}" for t in triggers[:4])
    bsl = f"{liq['nearest_bsl']:.4f}" if liq["nearest_bsl"] else "─"
    ssl = f"{liq['nearest_ssl']:.4f}" if liq["nearest_ssl"] else "─"
    eqh = f"EQH {liq['eqh']:.4f}" if liq["eqh"] else "─"
    eql = f"EQL {liq['eql']:.4f}" if liq["eql"] else "─"
    pa_top = opp["pa_sigs"][0] if opp["pa_sigs"] else "─"
    whale = "  │  ".join(opp["whale_zones"]) if opp["whale_zones"] else "─"
    return (
        f"🔥 *Alpha Oracle v8.2* 🔥\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 #{coin}  {arrow} {st}  [{opp['lev']}]\n"
        f"⏰ {opp['tf']}  │  1H: {htf_e} {opp['htf_trend']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *{opp['score']}分*  {opp['grade']}\n"
        f"   {bd_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 进场    `{opp['entry']:.4f}`\n"
        f"🛑 止损    `{opp['sl']:.4f}`  ({sl_sign}{sl_pct:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🥇 TP1 (1R)    `{opp['tp1']:.4f}`  ({sign}{tp1_pct:.2f}%)\n"
        f"🥈 TP2 (2.5R)  `{opp['tp2']:.4f}`  ({sign}{tp2_pct:.2f}%)\n"
        f"🏆 TP3 (4R)    `{opp['tp3']:.4f}`  ({sign}{tp3_pct:.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 *信号根据*\n"
        f"{trigger_txt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 结构: {opp['structure']}  │  P/D: {opp['pd_lb']}\n"
        f"💧 BSL {bsl}  │  SSL {ssl}\n"
        f"   {eqh}  │  {eql}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 {opp['regime']['regime']} (ADX={opp['regime']['adx']:.1f})\n"
        f"🧬 {opp['cvd_lb']}  │  多空比 {opp['ls_str']}\n"
        f"💸 {opp['fr_lb']}  │  {opp['ob_dir_lb']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕯 PA: {opp['pa_lb']} {opp['pa_sc']:.0f}分  │  {pa_top}\n"
        f"🐋 {whale}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *{'流动性扫除后进场' if liq['sweep_detected'] else ('主动扫单确认' if opp['as_bool'] else '等待进场区回踩')}*"
    )

def format_alert(coin: str, side: str, alert_type: str, price: float, entry: float, sl: float, tp1: float, tp2: float, tp3: float, new_sl: float = None, score: int = 0) -> str:
    arrow = "🟢" if side=="LONG" else "🔴"
    st = "多" if side=="LONG" else "空"
    if alert_type == "ENTRY":
        sl_pct = abs(entry - sl) / entry * 100
        tp1_pct = abs(tp1 - entry) / entry * 100
        sign = "+" if side=="LONG" else "-"
        sl_sign = "-" if side=="LONG" else "+"
        return (
            f"✅ *进场提醒* — #{coin} {arrow}{st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 进场价  `{entry:.4f}`\n"
            f"🛑 止损    `{sl:.4f}`  ({sl_sign}{sl_pct:.2f}%)\n"
            f"🥇 TP1     `{tp1:.4f}`  ({sign}{tp1_pct:.2f}%)\n"
            f"🥈 TP2     `{tp2:.4f}`\n"
            f"🏆 TP3     `{tp3:.4f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 评分 {score}分  │  当前 `{price:.4f}`\n"
            f"💡 价格已到达进场区，请确认进场！"
        )
    elif alert_type == "TP1":
        pnl = (price - entry) / entry * 100 if side=="LONG" else (entry - price) / entry * 100
        return (
            f"🎯 *TP1 到达！保本移损* — #{coin} {arrow}{st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"当前价  `{price:.4f}`  (+{pnl:.2f}%)\n"
            f"🎯 TP1  `{tp1:.4f}`  ✅ 已到\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡 止损已移至成本 `{new_sl:.4f}`\n"
            f"🎯 继续等 TP2  `{tp2:.4f}`\n"
            f"🏆 最终 TP3    `{tp3:.4f}`"
        )
    elif alert_type == "TP2":
        pnl = (price - entry) / entry * 100 if side=="LONG" else (entry - price) / entry * 100
        return (
            f"🎯 *TP2 到达！移损至TP1* — #{coin} {arrow}{st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"当前价  `{price:.4f}`  (+{pnl:.2f}%)\n"
            f"🥈 TP2  `{tp2:.4f}`  ✅ 已到\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡 止损已移至 TP1 `{new_sl:.4f}`（锁利）\n"
            f"🏆 继续持有等 TP3  `{tp3:.4f}` 🎉"
        )
    elif alert_type == "TP3":
        pnl = (price - entry) / entry * 100 if side=="LONG" else (entry - price) / entry * 100
        return (
            f"🏆 *TP3 全部到达！* — #{coin} {arrow}{st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"当前价  `{price:.4f}`  (+{pnl:.2f}%)\n"
            f"🏆 TP3  `{tp3:.4f}`  ✅ 完美收割！\n"
            f"建议全部平仓，恭喜获利 🎉🎉🎉"
        )
    elif alert_type == "SL":
        pnl = (price - entry) / entry * 100 if side=="LONG" else (entry - price) / entry * 100
        return (
            f"🛑 *止损触发* — #{coin} {arrow}{st}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"当前价  `{price:.4f}`  ({pnl:.2f}%)\n"
            f"🛑 止损  `{sl:.4f}`  已触发\n"
            f"仓位已平，请确认出场！"
        )
    return ""

# ─────────────────────────────────────────────────────────
# 8. SignalTracker
# ─────────────────────────────────────────────────────────
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.signals = self._load()
    
    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    
    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.signals, f, ensure_ascii=False, indent=2)
    
    def add(self, opp: dict) -> str:
        key = f"{opp['instId']}_{opp['side']}_{opp['tf']}_{int(time.time())}"
        with self._lock:
            self.signals[key] = {
                "instId": opp["instId"], "side": opp["side"], "tf": opp["tf"],
                "entry": opp["entry"], "sl": opp["sl"], "sl_orig": opp["sl"],
                "tp1": opp["tp1"], "tp2": opp["tp2"], "tp3": opp["tp3"],
                "score": opp["score"], "grade": opp["grade"],
                "status": "PENDING", "hit_tp1": False, "hit_tp2": False,
                "created": time.time(),
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
        coin = sig["instId"].split("-")[0]
        side = sig["side"]
        status = sig["status"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        if status == "PENDING":
            age_h = (time.time() - sig["created"]) / 3600
            if age_h > SIGNAL_EXPIRE_HOURS:
                logging.info(f"  ⏰ 讯号过期清除: {key}")
                send_tg(f"⏰ *讯号过期* — #{coin} {side}\n进场 `{entry:.4f}` 超过{SIGNAL_EXPIRE_HOURS}h 未触发")
                return True
        if status == "PENDING":
            entered = (side=="LONG" and price <= entry * (1 + ENTRY_TOLERANCE)) or (side=="SHORT" and price >= entry * (1 - ENTRY_TOLERANCE))
            if entered:
                self.update(key, status="ACTIVE")
                send_tg(format_alert(coin, side, "ENTRY", price, entry, sl, tp1, tp2, tp3, score=sig["score"]))
                trade_history.add_trade(key, coin, side, entry, sl, tp1, tp2, tp3, sig["score"])
                logging.info(f"  ✅ 进场触发: {key}  price={price:.4f}")
            return False
        if status not in ("ACTIVE", "BE", "TRAIL"):
            return False
        sl_hit = (side=="LONG" and price <= sl) or (side=="SHORT" and price >= sl)
        if sl_hit:
            send_tg(format_alert(coin, side, "SL", price, entry, sl, tp1, tp2, tp3))
            trade_history.close_trade(key, price, "SL")
            logging.info(f"  🛑 止损触发: {key}")
            return True
        tp3_hit = (side=="LONG" and price >= tp3) or (side=="SHORT" and price <= tp3)
        if tp3_hit:
            send_tg(format_alert(coin, side, "TP3", price, entry, sl, tp1, tp2, tp3))
            trade_history.close_trade(key, price, "TP3")
            logging.info(f"  🏆 TP3 到达: {key}")
            return True
        tp2_hit = (side=="LONG" and price >= tp2) or (side=="SHORT" and price <= tp2)
        if tp2_hit and not sig.get("hit_tp2"):
            self.update(key, hit_tp2=True, sl=tp1, status="TRAIL")
            send_tg(format_alert(coin, side, "TP2", price, entry, sl, tp1, tp2, tp3, new_sl=tp1))
            logging.info(f"  🥈 TP2 到达: {key}")
            return False
        tp1_hit = (side=="LONG" and price >= tp1) or (side=="SHORT" and price <= tp1)
        if tp1_hit and not sig.get("hit_tp1"):
            self.update(key, hit_tp1=True, sl=entry, status="BE")
            send_tg(format_alert(coin, side, "TP1", price, entry, sl, tp1, tp2, tp3, new_sl=entry))
            logging.info(f"  🥇 TP1 到达: {key}")
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
            coin = s["instId"].split("-")[0]
            arrow = "🟢" if s["side"]=="LONG" else "🔴"
            status_emoji = {"PENDING":"⏳","ACTIVE":"🔵","BE":"🛡","TRAIL":"🔁"}.get(s["status"],"❓")
            lines.append(f"{status_emoji} #{coin} {arrow}{s['side']} {s['tf']}  E:`{s['entry']:.4f}`  SL:`{s['sl']:.4f}`  [{s['score']}分]")
        return "\n".join(lines)

# ─────────────────────────────────────────────────────────
# 9. 监控循环
# ─────────────────────────────────────────────────────────
def monitor_loop(tracker: SignalTracker, interval: int = 30, max_duration: int = None, stop_event=None):
    global stop_requested
    start_time = time.time()
    logging.info(f"👀 监控循环启动，间隔 {interval}s")
    while True:
        if stop_requested or (stop_event and stop_event.is_set()):
            logging.info("🛑 监控收到停止信号")
            break
        if max_duration and (time.time() - start_time) > max_duration:
            logging.info(f"⏰ 监控达到最大运行时间 {max_duration}s")
            break
        try:
            active = tracker.list_active()
            if active:
                logging.info(f"🔍 监控中... {len(active)} 笔讯号")
                tracker.check_all()
            else:
                logging.info("📭 无追踪讯号")
        except Exception as e:
            logging.error(f"monitor_loop error: {e}")
        for _ in range(min(interval, 5)):
            if stop_requested or (stop_event and stop_event.is_set()):
                break
            time.sleep(1)

# ─────────────────────────────────────────────────────────
# 10. 主执行
# ─────────────────────────────────────────────────────────
def _check_entry_zone(opp: dict) -> tuple:
    live = fetch_ticker_price(opp["instId"])
    if live <= 0:
        return False, 0.0, "⚪ 无法取得即时价"
    entry = opp["entry"]
    side = opp["side"]
    tol = ENTRY_TOLERANCE
    in_zone = (side=="LONG" and live <= entry * (1 + tol) and live >= entry * (1 - tol * 3)) or (side=="SHORT" and live >= entry * (1 - tol) and live <= entry * (1 + tol * 3))
    dist_pct = (live - entry) / entry * 100
    if in_zone:
        return True, live, f"✅ 已在进场区！即时价 {live:.4f}（距进场 {dist_pct:+.2f}%）"
    elif (side=="LONG" and live > entry):
        return False, live, f"⚠️ 价格高于进场区 {dist_pct:+.2f}%"
    elif (side=="SHORT" and live < entry):
        return False, live, f"⚠️ 价格低于进场区 {dist_pct:+.2f}%"
    else:
        return False, live, f"⏳ 等待接近进场区（距离 {abs(dist_pct):.2f}%）"

def run_scan(tracker: SignalTracker) -> int:
    logging.info(f"🚀 Alpha Oracle v8.2 扫描  阈值={SETUP_SCORE_THRESHOLD}分")
    sent = 0
    for i, coin in enumerate(ALL_COINS, 1):
        if sent >= MAX_SIGNALS_PER_RUN:
            break
        logging.info(f"[{i}/{len(ALL_COINS)}] {coin} ...")
        if not check_news_cooldown(coin):
            logging.info(f"  [{coin}] 新闻冷却期")
            continue
        try:
            opps = scan_for_opportunity(coin)
            if opps:
                opps.sort(key=lambda x: x["score"], reverse=True)
                logging.info(f"  ✅ {len(opps)} signal(s)")
                for opp in opps:
                    if sent >= MAX_SIGNALS_PER_RUN:
                        break
                    if send_tg(format_signal(opp)):
                        sent += 1
                        logging.info(f"  📤 #{sent} [{opp['tf']}]{opp['side']} {opp['score']}分 {opp['grade']}")
                        in_zone, live, zone_msg = _check_entry_zone(opp)
                        logging.info(f"     即时价格: {zone_msg}")
                        if in_zone and live > 0:
                            time.sleep(0.5)
                            send_tg(format_alert(opp["instId"].split("-")[0], opp["side"], "ENTRY", live, opp["entry"], opp["sl"], opp["tp1"], opp["tp2"], opp["tp3"], score=opp["score"]))
                            logging.info(f"     ✅ 进场提醒已发送")
                        tracker.add(opp)
                    time.sleep(1)
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"❌ {coin}: {e}")
            traceback.print_exc()
    logging.info(f"📊 扫描完成，共发送 {sent} 讯号")
    if sent > 0:
        send_tg(tracker.status_summary())
    return sent

def main():
    global stop_requested
    parser = argparse.ArgumentParser(description="Alpha Oracle v8.2")
    parser.add_argument("--mode", default="all", choices=["scan", "monitor", "loop", "all"], help="scan=只扫描 | monitor=只监控 | loop=定时扫描+监控 | all=扫描后持续监控（预设）")
    parser.add_argument("--interval", type=int, default=30, help="监控轮询间隔（秒），预设30")
    parser.add_argument("--loop-interval", type=int, default=900, help="loop模式扫描间隔（秒），预设900=15分钟")
    parser.add_argument("--max-duration", type=int, default=None, help="最大运行时间（秒），预设无限制")
    parser.add_argument("--status", action="store_true", help="印出目前追踪中讯号并传送 TG 摘要")
    parser.add_argument("--report", action="store_true", help="发送昨日战报")
    parser.add_argument("--report-date", type=str, default=None, help="指定战报日期 (YYYY-MM-DD)，预设昨天")
    args = parser.parse_args()
    tracker = SignalTracker()
    if args.status:
        summary = tracker.status_summary()
        print(summary)
        send_tg(summary)
        return
    if args.report:
        report = trade_history.generate_daily_report(args.report_date)
        print(report)
        send_tg(report)
        return
    if args.mode == "scan":
        run_scan(tracker)
        return
    if args.mode == "monitor":
        try:
            monitor_loop(tracker, interval=args.interval, max_duration=args.max_duration)
        except KeyboardInterrupt:
            logging.info("👋 监控停止")
        return
    if args.mode == "loop":
        stop_ev = threading.Event()
        t = threading.Thread(target=monitor_loop, args=(tracker, args.interval, args.max_duration, stop_ev), daemon=True)
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
            logging.info("👋 循环停止")
            stop_ev.set()
            stop_requested = True
        return
    run_scan(tracker)
    try:
        monitor_loop(tracker, interval=args.interval, max_duration=args.max_duration)
    except KeyboardInterrupt:
        logging.info("👋 停止")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"💥 {e}")
        traceback.print_exc()
        sys.exit(1)
