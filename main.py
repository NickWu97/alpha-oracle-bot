#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v7.2 — 進階盤口行為技術分析策略框架
══════════════════════════════════════════════════════════════
新增功能：
  ✅ 十字線（Doji）偵測 — 多空分界定價中心
  ✅ 主動掃單（Sweep）識別 — 連續吃掉多層水位
  ✅ 釣魚單過濾 — 排除掛而不成交的洗盤陷阱
  ✅ 新聞冷卻機制 — 發布後1小時強制等待
  ✅ 帶量止損驗證 — 1-2-3順序驗證法
  ✅ 測牆機制 — 買賣牆對等性觀察
  ✅ 吸收過濾 — 大量成交但價格移動緩慢偵測
  ✅ CVD 背離偵測 — 累積成交量差分析
  ✅ 大單追蹤 — 鯨魚訂單識別
  ✅ 冰山訂單偵測 — 隱藏大單發現
  ✅ 價格衝擊分析 — 市場脆弱性評估
  ✅ 主動買賣比 — 攻守力道量化
  ✅ 多通道通知 — Telegram/Discord/Pushover/LINE/Email
══════════════════════════════════════════════════════════════
"""

import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Union
from collections import deque

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alpha_oracle_v7.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)

# Telegram 設定
TG_TOKEN            = os.getenv("TG_TOKEN")
CHAT_ID             = os.getenv("CHAT_ID")

# 多通道通知設定
DISCORD_WEBHOOK     = os.getenv("DISCORD_WEBHOOK", "")
PUSHOVER_APP_TOKEN  = os.getenv("PUSHOVER_APP_TOKEN", "")
PUSHOVER_USER_KEY   = os.getenv("PUSHOVER_USER_KEY", "")
LINE_TOKEN          = os.getenv("LINE_TOKEN", "")
EMAIL_FROM          = os.getenv("EMAIL_FROM", "")
EMAIL_TO            = os.getenv("EMAIL_TO", "")
EMAIL_USER          = os.getenv("EMAIL_USER", "")
EMAIL_PASS          = os.getenv("EMAIL_PASS", "")

COINANK_API_KEY     = os.getenv("COINANK_API_KEY", "")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP"
]

MAX_SIGNALS_PER_RUN = int(os.getenv("MAX_SIGNALS", "5"))
SETUP_SCORE_THRESHOLD = 40  # 綜合評分閾值 40分

# 盤口行為參數
CROSSLINE_BODY_RATIO = 0.30        # 十字線：實體 < 30% 總範圍
SWEEP_VOLUME_RATIO = 1.8           # 掃單：成交量 > 1.8倍均量
SWEEP_CONSECUTIVE_MOVES = 2        # 掃單：連續移動 >= 2根
NEWS_COOLDOWN_MINUTES = 60         # 新聞冷卻期 60分鐘
ABSORPTION_VOL_MULTIPLIER = 1.8    # 吸收：成交量 > 1.8倍均量
ABSORPTION_PRICE_THRESHOLD = 0.002 # 吸收：價格變動 < 0.2%
FISHING_PRICE_MOVE = 0.005         # 釣魚單：價格移動 >= 0.5%
FISHING_VOL_RATIO = 0.75           # 釣魚單：成交量 < 0.75倍均量

# 進階盤口參數
CVD_LOOKBACK = 50                  # CVD 計算週期
LARGE_ORDER_THRESHOLD = 10000      # 大單閾值 (USDT)
ICEBERG_REPEAT_COUNT = 3           # 冰山訂單重複次數
PRICE_IMPACT_WINDOW = 5            # 價格衝擊計算窗口
AGGRESSOR_RATIO_THRESHOLD = 0.70   # 主動買賣比閾值

# 新聞冷卻追蹤
_news_cooldown: Dict[str, float] = {}

# 訂單簿歷史（用於冰山偵測）
_orderbook_history: Dict[str, deque] = {}

# ─────────────────────────────────────────────
# 2. 工具函數
# ─────────────────────────────────────────────

def safe_float(val, fallback=0.0):
    try:    return float(val)
    except: return fallback

# ─────────────────────────────────────────────
# 2.1 多通道通知管理器
# ─────────────────────────────────────────────
class NotificationManager:
    """統一通知管理器 — 支援多通道發送"""
    
    def __init__(self):
        self.channels = {
            'telegram': bool(TG_TOKEN and CHAT_ID),
            'discord': bool(DISCORD_WEBHOOK),
            'pushover': bool(PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY),
            'line': bool(LINE_TOKEN),
            'email': bool(EMAIL_FROM and EMAIL_TO and EMAIL_USER and EMAIL_PASS)
        }
        logging.info(f"📡 通知通道: {sum(self.channels.values())}/5 已啟用")
    
    def send(self, message: str, priority: str = "normal", title: str = "Alpha Oracle"):
        """根據優先級選擇通知通道"""
        results = {}
        
        if priority == "critical":
            # 緊急：所有可用通道
            results = self._send_all(message, title)
        elif priority == "high":
            # 高優先：Telegram + Pushover + LINE
            if self.channels['telegram']:
                results['telegram'] = self._send_telegram(message)
            if self.channels['pushover']:
                results['pushover'] = self._send_pushover(message, priority=1)
            if self.channels['line']:
                results['line'] = self._send_line(f"{title}\n{message}")
        else:
            # 一般：僅 Telegram
            if self.channels['telegram']:
                results['telegram'] = self._send_telegram(message)
        
        # 記錄發送結果
        success = sum(1 for v in results.values() if v)
        if success > 0:
            logging.info(f"✅ 通知發送成功: {success} 通道")
        else:
            logging.warning("⚠️ 所有通知通道發送失敗")
        
        return results
    
    def _send_telegram(self, message: str) -> bool:
        """發送 Telegram 通知"""
        if not TG_TOKEN or not CHAT_ID:
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"},
                timeout=15
            )
            return response.status_code == 200
        except Exception as e:
            logging.error(f"Telegram 發送異常: {e}")
            return False
    
    def _send_discord(self, message: str, title: str = "Alpha Oracle") -> bool:
        """發送 Discord 通知"""
        if not DISCORD_WEBHOOK:
            return False
        try:
            # 解析 Markdown 為 Discord 格式
            content = message.replace("*", "**").replace("`", "```")
            embed = {
                "title": title,
                "description": content[:4000],  # Discord 限制
                "color": 0x00ff00 if "🟢" in message else (0xff0000 if "🔴" in message else 0xffff00),
                "timestamp": datetime.utcnow().isoformat(),
                "footer": {"text": "Alpha Oracle Bot"}
            }
            response = requests.post(
                DISCORD_WEBHOOK,
                json={"embeds": [embed]},
                timeout=15
            )
            return response.status_code == 204
        except Exception as e:
            logging.error(f"Discord 發送異常: {e}")
            return False
    
    def _send_pushover(self, message: str, priority: int = 0) -> bool:
        """發送 Pushover 通知"""
        if not PUSHOVER_APP_TOKEN or not PUSHOVER_USER_KEY:
            return False
        try:
            # 移除 Markdown 格式
            plain_msg = message.replace("*", "").replace("`", "")
            response = requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": PUSHOVER_APP_TOKEN,
                    "user": PUSHOVER_USER_KEY,
                    "message": plain_msg[:500],  # Pushover 限制
                    "title": "Alpha Oracle",
                    "priority": priority,
                    "sound": "pushover" if priority >= 1 else "none"
                },
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            logging.error(f"Pushover 發送異常: {e}")
            return False
    
    def _send_line(self, message: str) -> bool:
        """發送 LINE Notify 通知"""
        if not LINE_TOKEN:
            return False
        try:
            # 移除 Markdown 格式
            plain_msg = message.replace("*", "").replace("`", "")
            response = requests.post(
                "https://notify-api.line.me/api/notify",
                headers={"Authorization": f"Bearer {LINE_TOKEN}"},
                data={"message": plain_msg[:1000]},  # LINE 限制
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            logging.error(f"LINE Notify 發送異常: {e}")
            return False
    
    def _send_email(self, subject: str, body: str) -> bool:
        """發送 Email 通知"""
        if not all([EMAIL_FROM, EMAIL_TO, EMAIL_USER, EMAIL_PASS]):
            return False
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            
            msg = MIMEMultipart()
            msg['Subject'] = subject
            msg['From'] = EMAIL_FROM
            msg['To'] = EMAIL_TO
            
            # 純文字版本
            plain_body = body.replace("*", "").replace("`", "")
            msg.attach(MIMEText(plain_body, 'plain', 'utf-8'))
            
            # HTML 版本（可選）
            html_body = body.replace("\n", "<br>").replace("*", "**")
            msg.attach(MIMEText(f"<pre>{html_body}</pre>", 'html', 'utf-8'))
            
            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(EMAIL_USER, EMAIL_PASS)
                server.send_message(msg)
            return True
        except Exception as e:
            logging.error(f"Email 發送異常: {e}")
            return False
    
    def _send_all(self, message: str, title: str) -> Dict[str, bool]:
        """發送到所有可用通道"""
        results = {}
        if self.channels['telegram']:
            results['telegram'] = self._send_telegram(message)
        if self.channels['discord']:
            results['discord'] = self._send_discord(message, title)
        if self.channels['pushover']:
            results['pushover'] = self._send_pushover(message, priority=2)
        if self.channels['line']:
            results['line'] = self._send_line(f"{title}\n{message}")
        if self.channels['email']:
            results['email'] = self._send_email(title, message)
        return results

# 初始化通知管理器
notifier = NotificationManager()

def check_news_cooldown(instId: str) -> bool:
    """檢查是否在新聞冷卻期內"""
    now = time.time()
    if instId in _news_cooldown:
        if now - _news_cooldown[instId] < NEWS_COOLDOWN_MINUTES * 60:
            return False
    return True

def mark_news_event(instId: str):
    """標記新聞事件，啟動冷卻期"""
    _news_cooldown[instId] = time.time()
    logging.info(f"📰 News cooldown set for {instId}")

# ─────────────────────────────────────────────
# 3. 數據抓取
# ─────────────────────────────────────────────

def fetch_okx(instId: str, tf: str = "15m", limit: int = 200) -> Optional[pd.DataFrame]:
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') != '0': 
            logging.warning(f"[{instId}] API 錯誤: {res.get('msg')}")
            return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm']=="1"].iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.warning(f"[{instId}] Fetch Error: {e}")
        return None

def fetch_okx_trades(instId: str, limit: int = 100) -> Optional[pd.DataFrame]:
    """抓取最近成交記錄（用於大單/主動單分析）"""
    try:
        url = f"https://www.okx.com/api/v5/market/trades?instId={instId}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if res.get('code') != '0':
            return None
        df = pd.DataFrame(res['data'], columns=['ts','tradeId','px','sz','side','tt'])
        df[['px', 'sz']] = df[['px', 'sz']].astype(float)
        return df
    except Exception as e:
        logging.warning(f"[{instId}] Trades Fetch Error: {e}")
        return None

def fetch_funding_rate(instId: str) -> float:
    try:
        res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        return float(res['data'][0]['fundingRate']) if res.get('data') else 0
    except Exception as e:
        logging.warning(f"[{instId}] 費率抓取錯誤: {e}")
        return 0

def fetch_ls_ratio(symbol: str) -> str:
    """獲取多空比"""
    try:
        base_id = symbol.split('-')[0]
        res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        return res['data'][0]['ratio'] if res.get('data') else "N/A"
    except Exception as e:
        logging.warning(f"[{symbol}] 多空比抓取錯誤: {e}")
        return "N/A"

def fetch_order_book(instId: str, depth: int = 20) -> Dict:
    """獲取完整盤口數據"""
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}", timeout=5).json()
        if res.get('code') != '0' or not res.get('data'):
            return {"bids": [], "asks": [], "ts": None}
        data = res['data'][0]
        return {
            "bids": [[float(b[0]), float(b[1])] for b in data['bids']],  # [price, amount]
            "asks": [[float(a[0]), float(a[1])] for a in data['asks']],
            "ts": data.get('ts')
        }
    except Exception as e:
        logging.warning(f"[{instId}] 盤口抓取錯誤: {e}")
        return {"bids": [], "asks": [], "ts": None}

def fetch_order_book_imbalance(instId: str, depth: int = 20) -> tuple:
    """獲取盤口不平衡度"""
    try:
        ob = fetch_order_book(instId, depth)
        if not ob['bids'] or not ob['asks']:
            return 1.0, "⚪ 盤口均衡"
        
        bid_vol = sum(b[1] for b in ob['bids'])
        ask_vol = sum(a[1] for a in ob['asks']) or 1e-10
        ratio = bid_vol / ask_vol
        
        if ratio >= 1.30:
            label = f"🟢 買盤強勢 ({ratio:.2f})"
        elif ratio >= 1.05:
            label = f"🟡 買盤略強 ({ratio:.2f})"
        elif ratio >= 0.95:
            label = f"⚪ 盤口均衡 ({ratio:.2f})"
        elif ratio >= 0.77:
            label = f"🟡 賣盤略強 ({ratio:.2f})"
        else:
            label = f"🔴 賣盤強勢 ({ratio:.2f})"
        return ratio, label
    except Exception as e:
        logging.warning(f"[{instId}] 盤口抓取錯誤: {e}")
        return 1.0, "⚪ 盤口均衡"

# ─────────────────────────────────────────────
# 4. 技術指標計算
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else 0.001

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> tuple:
    """計算 Supertrend 指標"""
    if len(df) < period + 2: 
        return 0, "⚪ 未知"
    
    high = df['h'].values.astype(float)
    low = df['l'].values.astype(float)
    close = df['c'].values.astype(float)
    n = len(df)
    
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    
    atr = np.zeros(n)
    atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    
    hl2 = (high + low) / 2.0
    basic_up = hl2 - multiplier * atr
    basic_dn = hl2 + multiplier * atr
    
    final_up = np.zeros(n)
    final_dn = np.zeros(n)
    trend = np.ones(n, dtype=int)
    
    final_up[period] = basic_up[period]
    final_dn[period] = basic_dn[period]
    
    for i in range(period+1, n):
        final_up[i] = basic_up[i] if basic_up[i] > final_up[i-1] or close[i-1] < final_up[i-1] else final_up[i-1]
        final_dn[i] = basic_dn[i] if basic_dn[i] < final_dn[i-1] or close[i-1] > final_dn[i-1] else final_dn[i-1]
        
        if trend[i-1] == -1 and close[i] > final_dn[i-1]:
            trend[i] = 1
        elif trend[i-1] == 1 and close[i] < final_up[i-1]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]
    
    if trend[-1] == 1:
        return 1, "🟢 多頭"
    elif trend[-1] == -1:
        return -1, "🔴 空頭"
    return 0, "⚪ 未知"

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple:
    """尋找擺動高點和低點"""
    data = df.tail(lookback).reset_index(drop=True)
    sh, sl = [], []
    for i in range(n, len(data) - n):
        wh = data['h'].iloc[i-n:i+n+1]
        wl = data['l'].iloc[i-n:i+n+1]
        if data['h'].iloc[i] == wh.max():
            sh.append(data['h'].iloc[i])
        if data['l'].iloc[i] == wl.min():
            sl.append(data['l'].iloc[i])
    return sorted(set(sh)), sorted(set(sl))

def detect_market_structure(df: pd.DataFrame, side: str = None) -> str:
    """檢測市場結構（M頭/W底）"""
    sh, sl = find_swing_points(df, n=3, lookback=60)
    
    has_w = len(sl) >= 2 and sl[-2] > 0 and abs(sl[-2] - sl[-1]) / sl[-2] < 0.015
    has_m = len(sh) >= 2 and sh[-2] > 0 and abs(sh[-2] - sh[-1]) / sh[-2] < 0.015
    
    if side == "LONG":
        if has_w: return "W 底反轉 📐"
        if has_m: return "M 頭壓制 ⚠️"
    elif side == "SHORT":
        if has_m: return "M 頭反轉 📐"
        if has_w: return "W 底支撐 ⚠️"
    
    if has_w: return "W 底反轉 📐"
    if has_m: return "M 頭反轉 📐"
    
    recent = df.tail(20)
    slope = (recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    if slope > 0.025: return "上升趨勢延續 📈"
    elif slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"

def find_snr_zones(df: pd.DataFrame, side: str, lookback: int = 30) -> dict:
    """尋找支撐阻力區域"""
    sh, sl = find_swing_points(df, n=2, lookback=lookback)
    price = df['c'].iloc[-1]
    
    if side == "LONG":
        valid = [s for s in sl if s < price * 0.995]
        if valid:
            s = max(valid)
            return {"support": s, "resistance": None, "active_level": s, "text": f"支撐 {s:.4f}"}
    else:
        valid = [r for r in sh if r > price * 1.005]
        if valid:
            r = min(valid)
            return {"support": None, "resistance": r, "active_level": r, "text": f"壓力 {r:.4f}"}
    return None

# ─────────────────────────────────────────────
# 5. 盤口行為分析（v7.2 進階核心）
# ─────────────────────────────────────────────

def detect_crossline(df: pd.DataFrame, lookback: int = 15) -> Optional[Dict]:
    """十字線（Doji）偵測 — 多空分界定價中心"""
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        rng = k['h'] - k['l'] + 1e-10
        
        if body < CROSSLINE_BODY_RATIO * rng:
            up_wick = k['h'] - max(k['c'], k['o'])
            dn_wick = min(k['c'], k['o']) - k['l']
            
            if up_wick > dn_wick * 1.5:
                potential = "SHORT"
            elif dn_wick > up_wick * 1.5:
                potential = "LONG"
            else:
                potential = "NEUTRAL"
            
            dist_from_now = len(df) - 1 - i
            
            return {
                "price": k['c'],
                "high": k['h'],
                "low": k['l'],
                "body_ratio": body/rng,
                "potential_side": potential,
                "distance": dist_from_now,
                "desc": f"🎯 十字線 @ {k['c']:.4f}（潛在：{potential}，{dist_from_now}根前）"
            }
    return None

def detect_active_sweep(df: pd.DataFrame, side: str) -> Tuple[bool, float, str]:
    """主動掃單偵測 — 訂單流連續攻擊"""
    if len(df) < 8:
        return False, 0.0, "⚪ 數據不足"
    
    recent = df.tail(8)
    vol_ma = df['v'].tail(20).mean()
    last = recent.iloc[-1]
    vol_sc = last['v'] / (vol_ma + 1e-10)
    
    if vol_sc < SWEEP_VOLUME_RATIO:
        return False, 0.0, f"⚪ 量能不足 ({vol_sc:.1f}x均量)"
    
    moves = 0
    for i in range(len(recent)-1, 0, -1):
        if side == "LONG" and recent['c'].iloc[i] > recent['c'].iloc[i-1]:
            moves += 1
        elif side == "SHORT" and recent['c'].iloc[i] < recent['c'].iloc[i-1]:
            moves += 1
        else:
            break
    
    if moves >= SWEEP_CONSECUTIVE_MOVES:
        strength = min(vol_sc / 3.0, 1.0)
        desc = f"⚡ 主動掃單確認！連續{moves}根+{vol_sc:.1f}x量能"
        return True, strength, desc
    
    return False, 0.0, f"⚪ 無連續掃單（方向根數={moves}）"

def detect_fishing_trap(df: pd.DataFrame, side: str) -> bool:
    """釣魚單過濾 — 無量價格移動"""
    if len(df) < 6:
        return False
    
    recent = df.tail(6)
    vol_ma = df['v'].tail(20).mean()
    price_mv = abs(recent['c'].iloc[-1] - recent['c'].iloc[0]) / (recent['c'].iloc[0] + 1e-10)
    
    if price_mv < FISHING_PRICE_MOVE:
        return False
    
    last_vol = recent['v'].iloc[-1]
    return last_vol < FISHING_VOL_RATIO * vol_ma

def detect_absorption(df: pd.DataFrame, side: str) -> Tuple[bool, str]:
    """吸收信號 — 大量成交但價格幾乎不動"""
    if len(df) < 15:
        return False, "⚪ 無吸收"
    
    recent = df.tail(5)
    vol_ma = df['v'].tail(20).mean()
    avg_vol3 = recent['v'].iloc[-3:].mean()
    px_chg = abs(recent['c'].iloc[-1] - recent['c'].iloc[-4]) / (recent['c'].iloc[-4] + 1e-10)
    
    if avg_vol3 > ABSORPTION_VOL_MULTIPLIER * vol_ma and px_chg < ABSORPTION_PRICE_THRESHOLD:
        return True, f"🔄 吸收信號！量{avg_vol3/vol_ma:.1f}x均量但價格僅動{px_chg*100:.2f}%（主力換籌中）"
    return False, "⚪ 無明顯吸收"

def check_volume_breakout(df: pd.DataFrame) -> bool:
    """帶量止損驗證"""
    if len(df) < 6:
        return True
    recent = df.tail(6)
    vol_ma = recent['v'].iloc[:-1].mean()
    last_vol = recent['v'].iloc[-1]
    return last_vol >= 1.5 * vol_ma

def detect_wall_imbalance(df: pd.DataFrame, instId: str) -> Tuple[str, str]:
    """測牆機制 — 觀察買賣牆的對等性"""
    ratio, label = fetch_order_book_imbalance(instId)
    
    if ratio >= 1.30:
        return label, "🔴 賣壓可能（買牆撤單風險）"
    elif ratio <= 0.77:
        return label, "🟢 買壓可能（賣牆撤單風險）"
    else:
        return label, "⚪ 牆體平衡（等待失衡）"

# ─────────────────────────────────────────────
# 5.1 進階盤口指標
# ─────────────────────────────────────────────

def calculate_cvd(df: pd.DataFrame, periods: int = None) -> Dict:
    """
    CVD (Cumulative Volume Delta) 計算
    返回: {值, 斜率, 標籤, 分數, 背離狀態}
    """
    if periods is None:
        periods = CVD_LOOKBACK
    
    data = df.tail(periods).copy()
    if len(data) < 10:
        return {"value": 0, "slope": 0, "label": "⚪ 數據不足", "score": 0.3, "divergence": None}
    
    # 簡化 CVD: 陽線成交量為正，陰線為負
    delta = np.where(data['c'] > data['o'], data['v'],
                     np.where(data['c'] < data['o'], -data['v'], 0))
    cvd = np.cumsum(delta)
    
    cur = cvd.iloc[-1]
    slope = cur - cvd.iloc[-10] if len(cvd) >= 10 else cur
    
    # 背離偵測
    price_trend = data['c'].iloc[-1] - data['c'].iloc[-10]
    divergence = None
    if price_trend > 0 and slope < -data['v'].mean() * 2:
        divergence = "🔴 頂背離（價格漲/CVD跌）"
    elif price_trend < 0 and slope > data['v'].mean() * 2:
        divergence = "🟢 底背離（價格跌/CVD漲）"
    
    # 評分
    if slope > 0 and cur > 0:
        label, sc = f"🟢 買盤累積 CVD+{cur:,.0f}", 1.0
    elif slope > 0 and cur < 0:
        label, sc = f"🟡 CVD底部翻正（吸籌）", 0.65
    elif slope < 0 and cur < 0:
        label, sc = f"🔴 賣盤累積 CVD{cur:,.0f}", 1.0
    elif slope < 0 and cur > 0:
        label, sc = f"🟡 CVD頂部翻負（出貨）", 0.65
    else:
        label, sc = f"⚪ CVD持平", 0.3
    
    return {
        "value": float(cur),
        "slope": float(slope),
        "label": label,
        "score": sc,
        "divergence": divergence
    }

def analyze_trade_sizes(trades_df: pd.DataFrame, current_price: float) -> Dict:
    """
    交易規模分析 — 識別大單/鯨魚活動
    """
    if trades_df is None or len(trades_df) == 0:
        return {"large_buys": 0, "large_sells": 0, "whale_signal": "⚪ 無大單", "score": 0.3}
    
    # 計算每筆交易的 USDT 價值
    trades_df = trades_df.copy()
    trades_df['usdt_value'] = trades_df['px'] * trades_df['sz']
    
    # 分類
    large_buys = trades_df[(trades_df['side'] == 'buy') & (trades_df['usdt_value'] >= LARGE_ORDER_THRESHOLD)]
    large_sells = trades_df[(trades_df['side'] == 'sell') & (trades_df['usdt_value'] >= LARGE_ORDER_THRESHOLD)]
    
    whale_buys = trades_df[(trades_df['side'] == 'buy') & (trades_df['usdt_value'] >= LARGE_ORDER_THRESHOLD * 5)]
    whale_sells = trades_df[(trades_df['side'] == 'sell') & (trades_df['usdt_value'] >= LARGE_ORDER_THRESHOLD * 5)]
    
    # 信號判斷
    if len(whale_buys) > len(whale_sells) * 2:
        whale_signal = "🔵 鯨魚吸籌"
        score = 0.9
    elif len(whale_sells) > len(whale_buys) * 2:
        whale_signal = "🔴 鯨魚派發"
        score = 0.9
    elif len(large_buys) > len(large_sells):
        whale_signal = "🟢 大單偏多"
        score = 0.7
    elif len(large_sells) > len(large_buys):
        whale_signal = "🔴 大單偏空"
        score = 0.7
    else:
        whale_signal = "⚪ 大單均衡"
        score = 0.3
    
    return {
        "large_buys": len(large_buys),
        "large_sells": len(large_sells),
        "whale_buys": len(whale_buys),
        "whale_sells": len(whale_sells),
        "whale_signal": whale_signal,
        "score": score,
        "total_value": float(trades_df['usdt_value'].sum())
    }

def detect_iceberg_orders(instId: str, ob: Dict, lookback: int = 10) -> List[Dict]:
    """
    冰山訂單偵測 — 識別隱藏大單
    原理: 同一價位重複出現相似數量的訂單
    """
    if instId not in _orderbook_history:
        _orderbook_history[instId] = deque(maxlen=lookback)
    
    history = _orderbook_history[instId]
    current_hash = hashlib.md5(json.dumps(ob, sort_keys=True).encode()).hexdigest()
    
    # 檢查歷史中是否有相似盤口
    icebergs = []
    for hist_ob in history:
        # 檢查買一/賣一價是否重複
        if ob['bids'] and hist_ob.get('bids'):
            if abs(ob['bids'][0][0] - hist_ob['bids'][0][0]) < 0.0001:
                # 價格相同，檢查數量變化
                if abs(ob['bids'][0][1] - hist_ob['bids'][0][1]) / (hist_ob['bids'][0][1] + 1e-10) < 0.1:
                    icebergs.append({
                        "type": "bid",
                        "price": ob['bids'][0][0],
                        "amount": ob['bids'][0][1],
                        "desc": f"🧊 冰山買單 @ {ob['bids'][0][0]:.4f}"
                    })
        
        if ob['asks'] and hist_ob.get('asks'):
            if abs(ob['asks'][0][0] - hist_ob['asks'][0][0]) < 0.0001:
                if abs(ob['asks'][0][1] - hist_ob['asks'][0][1]) / (hist_ob['asks'][0][1] + 1e-10) < 0.1:
                    icebergs.append({
                        "type": "ask",
                        "price": ob['asks'][0][0],
                        "amount": ob['asks'][0][1],
                        "desc": f"🧊 冰山賣單 @ {ob['asks'][0][0]:.4f}"
                    })
    
    # 記錄當前盤口
    history.append(ob)
    
    return icebergs[:2]  # 最多返回2個

def calculate_price_impact(df: pd.DataFrame, window: int = None) -> Dict:
    """
    價格衝擊分析 — 計算單位成交量對價格的影響
    返回值越小代表流動性越好
    """
    if window is None:
        window = PRICE_IMPACT_WINDOW
    
    if len(df) < window + 1:
        return {"impact": 0, "label": "⚪ 數據不足", "score": 0.3}
    
    recent = df.tail(window + 1)
    
    # 計算每根K線的價格衝擊
    impacts = []
    for i in range(1, len(recent)):
        price_chg = abs(recent['c'].iloc[i] - recent['c'].iloc[i-1]) / recent['c'].iloc[i-1]
        vol = recent['v'].iloc[i]
        if vol > 0:
            impacts.append(price_chg / vol)
    
    if not impacts:
        return {"impact": 0, "label": "⚪ 無有效數據", "score": 0.3}
    
    avg_impact = np.mean(impacts)
    
    # 分類
    if avg_impact < 1e-8:
        label, score = "🟢 流動性極佳", 1.0
    elif avg_impact < 5e-8:
        label, score = "🟡 流動性良好", 0.7
    elif avg_impact < 2e-7:
        label, score = "🟠 流動性一般", 0.4
    else:
        label, score = "🔴 流動性脆弱", 0.1
    
    return {
        "impact": float(avg_impact),
        "label": label,
        "score": score,
        "std": float(np.std(impacts)) if len(impacts) > 1 else 0
    }

def calculate_aggressor_ratio(trades_df: pd.DataFrame) -> Dict:
    """
    主動買賣比 — 統計主動買入 vs 主動賣出
    """
    if trades_df is None or len(trades_df) == 0:
        return {"ratio": 1.0, "label": "⚪ 無數據", "score": 0.3}
    
    buys = len(trades_df[trades_df['side'] == 'buy'])
    sells = len(trades_df[trades_df['side'] == 'sell'])
    total = buys + sells
    
    if total == 0:
        return {"ratio": 1.0, "label": "⚪ 無交易", "score": 0.3}
    
    ratio = buys / total
    
    if ratio >= AGGRESSOR_RATIO_THRESHOLD:
        label, score = f"🟢 主動買入強勢 ({ratio*100:.0f}%)", 0.9
    elif ratio <= (1 - AGGRESSOR_RATIO_THRESHOLD):
        label, score = f"🔴 主動賣出強勢 ({(1-ratio)*100:.0f}%)", 0.9
    elif ratio >= 0.55:
        label, score = f"🟡 買方略強 ({ratio*100:.0f}%)", 0.6
    elif ratio <= 0.45:
        label, score = f"🟡 賣方略強 ({(1-ratio)*100:.0f}%)", 0.6
    else:
        label, score = f"⚪ 買賣均衡 ({ratio*100:.0f}%)", 0.3
    
    return {
        "ratio": ratio,
        "buys": buys,
        "sells": sells,
        "label": label,
        "score": score
    }

# ─────────────────────────────────────────────
# 6. 價格行為分析
# ─────────────────────────────────────────────

def detect_price_action(df: pd.DataFrame, side: str) -> list:
    """檢測價格行為形態"""
    signals = []
    
    for i in range(len(df) - 1, max(len(df) - 5, 0), -1):
        k = df.iloc[i]
        body = abs(k['c'] - k['o'])
        total_range = k['h'] - k['l'] + 1e-10
        upper_wick = k['h'] - max(k['c'], k['o'])
        lower_wick = min(k['c'], k['o']) - k['l']
        
        if side == "SHORT" and upper_wick >= body * 2.0 and lower_wick <= body * 0.5:
            strength = min(upper_wick / (body + 1e-10), 5.0)
            signals.append(f"空頭流星線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        
        if side == "LONG" and lower_wick >= body * 2.0 and upper_wick <= body * 0.5:
            strength = min(lower_wick / (body + 1e-10), 5.0)
            signals.append(f"多頭錘子線 ({strength:.1f}R 影) @ {k['c']:.4f}")
        
        if side == "SHORT" and upper_wick / total_range > 0.40 and k['c'] < k['o']:
            signals.append(f"壓力位拒絕 (上影 {upper_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        
        if side == "LONG" and lower_wick / total_range > 0.40 and k['c'] > k['o']:
            signals.append(f"支撐位拒絕 (下影 {lower_wick/total_range*100:.0f}%) @ {k['c']:.4f}")
        
        body_pct = body / total_range
        if body_pct >= 0.70:
            if (side == "LONG" and k['c'] > k['o']) or (side == "SHORT" and k['c'] < k['o']):
                signals.append(f"{'多頭' if side=='LONG' else '空頭'}動量棒 ({body_pct*100:.0f}%實體) @ {k['c']:.4f}")
    
    return signals[:3]

def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple:
    """計算價格行為評分"""
    score = 0.0
    signals = detect_price_action(df, side)
    
    if len(signals) >= 3:
        score += 0.60
    elif len(signals) >= 2:
        score += 0.40
    elif len(signals) >= 1:
        score += 0.20
    
    last_k = df.iloc[-1]
    body = abs(last_k['c'] - last_k['o'])
    rng = last_k['h'] - last_k['l'] + 1e-10
    
    if body / rng > 0.70:
        score += 0.20
    if (side == "LONG" and last_k['c'] > last_k['o']) or (side == "SHORT" and last_k['c'] < last_k['o']):
        score += 0.20
    
    score = min(score, 1.0)
    
    if score >= 0.65:
        label = "✅ 強勢PA"
    elif score >= 0.40:
        label = "⚠️ 中等PA"
    else:
        label = "⛔ 弱PA"
    
    return score * 100, label, signals

def detect_whale_zones(df: pd.DataFrame, side: str) -> list:
    """檢測主力區域"""
    zones = []
    vol_ma = df['v'].rolling(20).mean()
    vol_std = df['v'].rolling(20).std()
    
    for i in range(max(len(df) - 10, 0), len(df)):
        if df['v'].iloc[i] > vol_ma.iloc[i] + 2 * vol_std.iloc[i]:
            if df['c'].iloc[i] > df['o'].iloc[i] and side == "LONG":
                zones.append(f"🔵 主力吸籌區 {df['c'].iloc[i]:.4f}")
            elif df['c'].iloc[i] < df['o'].iloc[i] and side == "SHORT":
                zones.append(f"🔴 主力派發區 {df['c'].iloc[i]:.4f}")
    
    recent_high = df['h'].iloc[-20:].max()
    recent_low = df['l'].iloc[-20:].min()
    if side == "SHORT":
        zones.append(f"🔴 多頭清算熱點 {recent_high:.4f}")
    else:
        zones.append(f"🔵 空頭清算熱點 {recent_low:.4f}")
    
    return zones[:2]

def calculate_setup_score(setup: dict) -> float:
    """計算綜合評分（100分制）"""
    score = 0.0
    
    # 主力信號 (25%)
    whale_score = setup.get('whale_score', 0.3)
    if setup.get('whale_signal', '').startswith("✅") or setup.get('whale_signal', '').startswith("🔵"):
        score += 0.25 * whale_score
    elif setup.get('whale_signal', '').startswith("⚠️"):
        score += 0.12 * whale_score
    
    # 價格行為 (20%)
    score += 0.20 * setup.get('pa_score', 0) / 100
    
    # 技術指標 (20%)
    if setup.get('st_label') == "🟢 多頭" and setup.get('side') == "LONG":
        score += 0.20
    elif setup.get('st_label') == "🔴 空頭" and setup.get('side') == "SHORT":
        score += 0.20
    
    # CVD (15%)
    score += 0.15 * setup.get('cvd_score', 0.3)
    
    # 盤口行為 (10%)
    score += 0.10 * setup.get('orderflow_score', 0.3)
    
    # 資金費率 (10%)
    try:
        fr = setup.get('funding_rate', 0)
        if setup.get('side') == "LONG" and fr < 0.0003:
            score += 0.10
        elif setup.get('side') == "SHORT" and fr > -0.0003:
            score += 0.10
    except:
        pass
    
    return min(score, 1.0) * 100

# ─────────────────────────────────────────────
# 7. 主掃描邏輯
# ─────────────────────────────────────────────

def scan_for_opportunity(instId: str) -> list:
    """核心掃描函數"""
    df_15m = fetch_okx(instId, tf="15m", limit=150)
    if df_15m is None: 
        return []
    
    if not check_news_cooldown(instId):
        logging.info(f"[{instId}] 新聞冷卻期，跳過")
        return []
    
    atr = calculate_atr(df_15m)
    st_val, st_label = calculate_supertrend(df_15m)
    
    # 盤口行為分析
    crossline = detect_crossline(df_15m)
    abs_detected, abs_desc = detect_absorption(df_15m, "LONG")
    
    # 進階盤口指標
    cvd_data = calculate_cvd(df_15m)
    trades_df = fetch_okx_trades(instId, limit=50)
    current_price = df_15m['c'].iloc[-1]
    trade_analysis = analyze_trade_sizes(trades_df, current_price)
    price_impact = calculate_price_impact(df_15m)
    
    # 盤口數據
    ob = fetch_order_book(instId)
    icebergs = detect_iceberg_orders(instId, ob)
    aggressor = calculate_aggressor_ratio(trades_df)
    
    opportunities = []
    
    for side in ["LONG", "SHORT"]:
        if detect_fishing_trap(df_15m, side):
            logging.info(f"[{instId}/{side}] 釣魚單，跳過")
            continue
        
        snr_zone = find_snr_zones(df_15m, side)
        if not snr_zone: 
            continue
        
        pa_score, pa_label, pa_signals = calculate_pa_score(df_15m, side)
        structure = detect_market_structure(df_15m, side)
        
        whale_zones = detect_whale_zones(df_15m, side)
        funding_rate = fetch_funding_rate(instId)
        ls_ratio = fetch_ls_ratio(instId)
        ob_ratio, ob_label = fetch_order_book_imbalance(instId)
        
        sweep_detected, sweep_strength, sweep_desc = detect_active_sweep(df_15m, side)
        vol_ok = check_volume_breakout(df_15m)
        wall_status, wall_direction = detect_wall_imbalance(df_15m, instId)
        
        # 綜合評分參數
        orderflow_score = np.mean([
            cvd_data['score'],
            trade_analysis['score'],
            price_impact['score'],
            aggressor['score'],
            0.8 if sweep_detected else 0.3
        ])
        
        setup = {
            'side': side,
            'pa_score': pa_score,
            'st_label': st_label,
            'cvd_score': cvd_data['score'],
            'whale_signal': trade_analysis['whale_signal'],
            'whale_score': trade_analysis['score'],
            'funding_rate': funding_rate,
            'orderflow_score': orderflow_score
        }
        setup_score = calculate_setup_score(setup)
        
        if setup_score < SETUP_SCORE_THRESHOLD:
            logging.info(f"[{instId}/{side}] {setup_score:.0f}分 < {SETUP_SCORE_THRESHOLD}，跳過")
            continue
        
        # 進場價計算
        if crossline:
            entry = crossline['low'] * 1.001 if side == "LONG" else crossline['high'] * 0.999
        elif side == "LONG":
            entry = snr_zone['support'] if snr_zone['support'] else current_price * 0.995
        else:
            entry = snr_zone['resistance'] if snr_zone['resistance'] else current_price * 1.005
        
        sl = entry - atr * 1.5 if side == "LONG" else entry + atr * 1.5
        risk = abs(entry - sl)
        tp1 = entry + risk * 1.0 if side == "LONG" else entry - risk * 1.0
        tp2 = entry + risk * 2.5 if side == "LONG" else entry - risk * 2.5
        tp3 = entry + risk * 4.0 if side == "LONG" else entry - risk * 4.0
        
        opp = {
            "instId": instId,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "structure": structure,
            "snr_zone": snr_zone,
            "pa_score": pa_score,
            "pa_label": pa_label,
            "pa_signals": pa_signals,
            "cvd_data": cvd_data,
            "ls_ratio": ls_ratio,
            "funding_rate": funding_rate,
            "ob_label": ob_label,
            "whale_zones": whale_zones,
            "st_label": st_label,
            "setup_score": setup_score,
            "leverage": "10x ~ 20x (低波動)" if atr / current_price < 0.015 else "3x ~ 5x (高波動)",
            "crossline": crossline,
            "sweep_detected": sweep_detected,
            "sweep_desc": sweep_desc,
            "absorption_detected": abs_detected,
            "absorption_desc": abs_desc,
            "wall_status": wall_status,
            "wall_direction": wall_direction,
            "vol_ok": vol_ok,
            "atr": atr,
            # 進階指標
            "trade_analysis": trade_analysis,
            "price_impact": price_impact,
            "aggressor": aggressor,
            "icebergs": icebergs,
            "orderflow_score": orderflow_score
        }
        opportunities.append(opp)
    
    return opportunities

def format_signal_message(opp: dict) -> str:
    """格式化信號消息（完全匹配圖片格式 + 進階指標）"""
    coin_symbol = opp['instId'].split('-')[0]
    side_emoji = "🟢" if opp['side'] == "LONG" else "🔴"
    side_text = "多單 (LONG)" if opp['side'] == "LONG" else "空單 (SHORT)"
    
    # SNR 顯示
    support_str = f"{opp['snr_zone']['support']:.4f}" if opp['snr_zone'] and opp['snr_zone'].get('support') else "─"
    resistance_str = f"{opp['snr_zone']['resistance']:.4f}" if opp['snr_zone'] and opp['snr_zone'].get('resistance') else "─"
    snr_display = f"🟢 支撐 {support_str} | 🔴 壓力 {resistance_str}"
    snr_active = f"✅ 參考 {opp['snr_zone']['text']}" if opp['snr_zone'] else "⚠️ 無明顯關鍵位"
    
    # PA 信號
    pa_lines = "".join(f"   {sig}\n" for sig in opp['pa_signals'][:3]) if opp['pa_signals'] else "   ─ 無明顯 PA 訊號\n"
    
    # 主力區
    whale_text = " | ".join(opp['whale_zones']) if opp['whale_zones'] else "─"
    
    # 盤口行為
    crossline_txt = opp['crossline']['desc'] if opp['crossline'] else "⚪ 無近期十字線"
    sweep_txt = opp['sweep_desc'] if opp['sweep_detected'] else "⚪ 無主動掃單"
    abs_txt = opp['absorption_desc'] if opp['absorption_detected'] else "⚪ 無吸收信號"
    
    # 進階指標顯示
    cvd_div = f"\n   {opp['cvd_data']['divergence']}" if opp['cvd_data'].get('divergence') else ""
    whale_detail = f"{opp['trade_analysis']['whale_signal']} (買{opp['trade_analysis']['large_buys']}/賣{opp['trade_analysis']['large_sells']})"
    impact_label = opp['price_impact']['label']
    aggressor_label = opp['aggressor']['label']
    
    # 冰山訂單
    iceberg_txt = ""
    if opp['icebergs']:
        iceberg_txt = "\n" + "\n".join(f"   {ib['desc']}" for ib in opp['icebergs'])
    else:
        iceberg_txt = "\n   ⚪ 無冰山訂單"
    
    # 進場位標記
    entry_marker = "⚡ (十字線突破)" if opp['crossline'] else "⚡ (突破點)"
    vol_warn = "" if opp['vol_ok'] else "\n⚠️ 當前K線量能偏低，注意假突破"
    
    # 盤口狀態勾選
    sweep_check = "✅" if opp['sweep_detected'] else "⚪"
    wall_check = "✅" if "失衡" in opp['wall_direction'] else "⚪"
    flow_check = "✅" if opp['orderflow_score'] >= 0.6 else "⚪"
    
    msg = (
        f"🔥 *Alpha Oracle v7.2 | 進階盤口訊號* 🔥\n"
        f"──────────────────\n"
        f"💎 幣種：#{coin_symbol}\n"
        f"🎯 方向：{side_emoji} {side_text}\n"
        f"⏰ 週期：15m\n"
        f"📊 多空比 {opp['ls_ratio']} | 資費 {opp['funding_rate']*100:.4f}%\n"
        f"🧬 CVD：{opp['cvd_data']['label']}{cvd_div}\n"
        f"📚 盤口：{opp['ob_label']}\n"
        f"\n"
        f"💰 進場位：{opp['entry']:.4f} {entry_marker}\n"
        f"🛑 止損位：{opp['sl']:.4f} (-1R){vol_warn}\n"
        f"💰 TP1 (1.0R): {opp['tp1']:.4f}\n"
        f"💰 TP2 (2.5R): {opp['tp2']:.4f}\n"
        f"💰 TP3 (4.0R): {opp['tp3']:.4f}\n"
        f"\n"
        f"🏗️ 結構：{opp['structure']}\n"
        f"🛡️ SNR：{snr_display}\n"
        f"    {snr_active}\n"
        f"\n"
        f"🕯️ 價格行為 ({opp['pa_label']} {opp['pa_score']:.0f}分)\n"
        f"{pa_lines}"
        f"🐋 主力：{whale_detail}\n"
        f"🎯 主力區：{whale_text}\n"
        f"📡 Supertrend：{opp['st_label']}\n"
        f"🕹️ 槓桿：{opp['leverage']}\n"
        f"📌 類型：長單 (波段)\n"
        f"📊 綜合評分：{opp['setup_score']:.0f}分 (閾值:{SETUP_SCORE_THRESHOLD:.0f}分)\n"
        f"\n"
        f"📋 盤口行為：\n"
        f"   {crossline_txt}\n"
        f"   {sweep_txt}\n"
        f"   {abs_txt}{iceberg_txt}\n"
        f"\n"
        f"🔬 進階指標：\n"
        f"   📈 價格衝擊：{impact_label}\n"
        f"   ⚔️  主動比：{aggressor_label}\n"
        f"   💧 訂單流：{'✅ 強勢' if opp['orderflow_score']>=0.6 else '⚪ 一般'} ({opp['orderflow_score']*100:.0f}分)\n"
        f"\n"
        f"✅ 狀態：{sweep_check}掃單確認 | {wall_check}牆體失衡 | {flow_check}訂單流\n"
        f"\n"
        f"💡 *{'等待掃單確認後成交...' if not opp['sweep_detected'] else '🚀 訊號確認，可考慮進場'}*"
    )
    return msg

# ─────────────────────────────────────────────
# 8. 主執行函數
# ─────────────────────────────────────────────

def main():
    """主函數 - 一次性掃描"""
    logging.info(f"🚀 Alpha Oracle v7.2 Started | 閾值={SETUP_SCORE_THRESHOLD}分")
    
    signals_sent = 0
    
    for i, coin in enumerate(ALL_COINS, 1):
        logging.info(f"[{i}/{len(ALL_COINS)}] Scanning {coin}...")
        
        try:
            opps = scan_for_opportunity(coin)
            
            if opps:
                logging.info(f"✅ Found {len(opps)} opportunity(ies) for {coin}")
                
                for opp in opps:
                    if signals_sent >= MAX_SIGNALS_PER_RUN:
                        break
                    
                    msg = format_signal_message(opp)
                    
                    # 根據評分決定優先級
                    priority = "critical" if opp['setup_score'] >= 80 else ("high" if opp['setup_score'] >= 60 else "normal")
                    
                    # 多通道發送
                    notifier.send(msg, priority=priority, title=f"#{coin.split('-')[0]} {opp['side']}")
                    
                    signals_sent += 1
                    logging.info(f"✅ Signal {signals_sent} sent | {opp['setup_score']:.0f}分 {opp['side']}")
                    
                    time.sleep(1)
            
            time.sleep(0.5)
            
        except Exception as e:
            logging.error(f"❌ Scan Error for {coin}: {e}")
            traceback.print_exc()
            continue
    
    logging.info(f"📊 Scan Complete. Sent {signals_sent} signals.")
    return signals_sent

if __name__ == "__main__":
    try:
        signals_count = main()
        exit(0)
    except Exception as e:
        logging.error(f"💥 Bot crashed: {e}")
        traceback.print_exc()
        exit(1)
