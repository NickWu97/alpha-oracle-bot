#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle v5.1 - 機構級 SMC + 數據背離引擎（完整整合版）
═══════════════════════════════════════════════════════════════
  ✅ 連續監控：while True 每 60 秒掃描（修復進場通知丟失）
  ✅ 心跳監控：每小時 Telegram 確認存活
  ✅ SMC 精準化：OB 50% Mean 回踩進場，FVG > 1.5 ATR 過濾假缺口
  ✅ MTF 趨勢鎖定：1H Supertrend + EMA50 確認方向，禁止逆勢
  ✅ 數據背離引擎：CVD / LS Ratio / Funding Rate 判斷主力意圖
  ✅ PA 確認觸發：OB/FVG 區域內出現 Pin Bar / Engulfing 才發訊號
  ✅ RSI-14 / EMA-200 / Volume Spike 多重過濾
  ✅ 交易時段過濾（倫敦 / 紐約盤，UTC 07-21）
  ✅ 訊號冷卻機制（每幣 1 小時冷卻，防重複）
  ✅ None-safety 全面保護（API 失敗不崩潰）
  ✅ 評分制過濾（0-100 分，>=45 才通過）
  ✅ 檔案日誌 alpha_oracle.log
  ✅ 啟動通知 + 每日午夜報告
  🔧 修復：進場通知 100% 可靠（is_hit 先於 missed_entry）
  🔧 修復：OB/FVG 進場點精確化（OB 50% mean, FVG midpoint）
  🔧 修復：v5.0 check_data_divergence 語法錯誤
═══════════════════════════════════════════════════════════════
環境變數：
  TG_TOKEN            Telegram Bot Token
  CHAT_ID             Telegram Chat ID
  COINANK_API_KEY     CoinAnk API（可選）
  GLASSNODE_API_KEY   Glassnode API（可選）
  CRYPTOQUANT_API_KEY CryptoQuant API（可選）
  PAPER_TRADING       "true" 啟用紙交易模式
  SESSION_FILTER      "false" 關閉時段過濾
  SCAN_INTERVAL       掃描間隔秒數（預設 60）
"""
import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
import json
import functools
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# 1. 基礎配置
# ─────────────────────────────────────────────

# 雙輸出：檔案 + 控制台
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("alpha_oracle.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TG_TOKEN            = os.getenv("TG_TOKEN")
CHAT_ID             = os.getenv("CHAT_ID")
COINANK_API_KEY     = os.getenv("COINANK_API_KEY", "")
GLASSNODE_API_KEY   = os.getenv("GLASSNODE_API_KEY", "")
CRYPTOQUANT_API_KEY = os.getenv("CRYPTOQUANT_API_KEY", "")
OPTIMIZATION_FILE   = "whale_optimization.json"
HEARTBEAT_FILE      = "last_heartbeat.txt"

# 模式開關
PAPER_TRADING          = os.getenv("PAPER_TRADING",   "false").lower() == "true"
SESSION_FILTER_ENABLED = os.getenv("SESSION_FILTER",  "true").lower()  == "true"
SCAN_INTERVAL          = int(os.getenv("SCAN_INTERVAL", "60"))  # 秒

# 過濾閾值
SETUP_SCORE_THRESHOLD = 0.45
PA_MIN_SCORE          = 0.30
WAITING_EXPIRY_BARS   = 20       # 15m×20 = 5 小時
RSI_OVERBOUGHT        = 72
RSI_OVERSOLD          = 28
MIN_FVG_ATR_MULT      = 1.5      # 🆕 FVG 最小高度（相對 ATR）
SIGNAL_COOLDOWN_BARS  = 4        # 4×15m = 1 小時冷卻

# 時段（UTC）
SESSION_LONDON_START, SESSION_LONDON_END = 7,  16
SESSION_NY_START,     SESSION_NY_END     = 13, 21

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP",
    "BCH-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"
]

LOG_FILE   = "active_trades.csv"
STATS_FILE = "daily_stats.csv"

LOG_COLS = [
    "instId", "side", "status", "entry", "sl", "tp1", "tp2", "tp3",
    "locked", "wait_since", "tp1_hit", "entry_source",
    "snr_display", "snr_active",
    "whale_signal", "whale_confidence", "whale_category",
    "pa_score", "pa_signals", "setup_score", "divergence_type",
]
STATS_COLS = [
    "instId", "result", "whale_signal", "whale_confidence",
    "whale_category", "pa_score", "setup_score", "divergence_type"
]

_signal_cooldown: dict[str, int] = {}

# ─────────────────────────────────────────────
# 2. 工具函數
# ─────────────────────────────────────────────

def retry_on_failure(max_retries: int = 3, delay: float = 1.0):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < max_retries - 1:
                        wt = delay * (2 ** attempt)
                        logging.warning(f"{func.__name__} 失敗 ({attempt+1}/{max_retries})，{wt:.0f}s 後重試：{e}")
                        time.sleep(wt)
                    else:
                        logging.warning(f"{func.__name__} 全部重試失敗：{e}")
            return None
        return wrapper
    return decorator


def safe_float(val, fallback: float = 0.0) -> float:
    try:    return float(val)
    except: return fallback


def safe_int(val, fallback: int = 0) -> int:
    try:    return int(float(val))
    except: return fallback


def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception as e:
        logging.warning(f"TG 發送失敗：{e}")


def heartbeat_check():
    """🆕 每小時確認機器人存活"""
    now = time.time()
    try:
        with open(HEARTBEAT_FILE, 'r') as f:
            last_time = float(f.read().strip())
        if now - last_time >= 3600:
            raise ValueError("need refresh")
    except Exception:
        mode_tag = "🧪 紙交易" if PAPER_TRADING else "🔴 實盤"
        send_tg(
            f"💓 *Alpha Oracle v5.1 | 心跳確認*\n"
            f"──────────────────\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"📡 監控幣種：{len(ALL_COINS)} 個\n"
            f"🔄 掃描間隔：{SCAN_INTERVAL}s\n"
            f"📌 模式：{mode_tag}\n"
            f"✅ 系統運作正常"
        )
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(str(now))


def is_in_cooldown(instId: str, current_bar: int) -> bool:
    last = _signal_cooldown.get(instId)
    return last is not None and (current_bar - last) < SIGNAL_COOLDOWN_BARS


def set_cooldown(instId: str, current_bar: int):
    _signal_cooldown[instId] = current_bar


def load_optimization_params() -> dict:
    if os.path.exists(OPTIMIZATION_FILE):
        try:
            with open(OPTIMIZATION_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "base_threshold":   0.7,
        "aligned_win_rate": 0.75,
        "warning_win_rate": 0.60,
        "reverse_win_rate": 0.40,
        "total_samples":    0
    }


def save_optimization_params(params: dict):
    with open(OPTIMIZATION_FILE, 'w') as f:
        json.dump(params, f)


def get_dynamic_threshold(opt: dict) -> float:
    base = opt['base_threshold']
    awr  = opt['aligned_win_rate']
    if   awr > 0.80: return max(0.5, base - 0.1)
    elif awr < 0.65: return min(0.9, base + 0.1)
    return base


def normalize_trade(t: dict) -> dict:
    return {
        "instId":           str(t.get("instId", "")),
        "side":             str(t.get("side", "")),
        "status":           str(t.get("status", "")),
        "entry":            safe_float(t.get("entry")),
        "sl":               safe_float(t.get("sl")),
        "tp1":              safe_float(t.get("tp1")),
        "tp2":              safe_float(t.get("tp2")),
        "tp3":              safe_float(t.get("tp3")),
        "locked":           safe_int(t.get("locked")),
        "wait_since":       safe_int(t.get("wait_since", 0)),
        "tp1_hit":          safe_int(t.get("tp1_hit", 0)),
        "entry_source":     str(t.get("entry_source", "OB")),
        "snr_display":      str(t.get("snr_display", "🟢 支撐 ─ | 🔴 壓力 ─")),
        "snr_active":       str(t.get("snr_active",  "⚠️ 無明顯關鍵位")),
        "whale_signal":     str(t.get("whale_signal", "─")),
        "whale_confidence": safe_float(t.get("whale_confidence", 0)),
        "whale_category":   str(t.get("whale_category", "Unknown")),
        "pa_score":         safe_float(t.get("pa_score", 0)),
        "pa_signals":       str(t.get("pa_signals", "─")),
        "setup_score":      safe_float(t.get("setup_score", 0)),
        "divergence_type":  str(t.get("divergence_type", "─")),
    }


def get_whale_position_rec(signal: str, conf: float) -> tuple[str, str, str]:
    if signal in ("✅ 主力一致", "✅ 技術面主導"):
        if   conf >= 0.80: return "✅ 正常 (100%)", "75-85%", "🟢"
        elif conf >= 0.65: return "🟡 標準 (75%)", "70-78%", "🟡"
        else:              return "🟠 保守 (50%)", "60-70%", "🟠"
    elif signal in ("⚠️ 主力警示", "⚠️ 技術面中等"):
        if   conf >= 0.60: return "🟠 保守 (50%)", "60-70%", "🟠"
        else:              return "🔴 觀望/極小",  "<60%",    "🔴"
    return "⛔ 建議跳過", "<50%", "🔴"


# ─────────────────────────────────────────────
# 3. 數據抓取（全部 None-safe）
# ─────────────────────────────────────────────

@retry_on_failure(3, 1.0)
def fetch_okx(instId: str, tf: str = "15m", limit: int = 150) -> pd.DataFrame | None:
    url = (f"https://www.okx.com/api/v5/market/candles"
           f"?instId={instId}&bar={tf}&limit={limit}")
    res = requests.get(url, timeout=10).json()
    if res.get('code') != '0' or not res.get('data'):
        return None
    df = pd.DataFrame(
        res['data'],
        columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm']
    )
    df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
    confirmed = df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    return confirmed if not confirmed.empty else None


@retry_on_failure(3, 1.0)
def fetch_okx_1h(instId: str, limit: int = 60) -> pd.DataFrame | None:
    url = (f"https://www.okx.com/api/v5/market/candles"
           f"?instId={instId}&bar=1H&limit={limit}")
    res = requests.get(url, timeout=10).json()
    if res.get('code') != '0' or not res.get('data'):
        return None
    df = pd.DataFrame(
        res['data'],
        columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm']
    )
    df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
    confirmed = df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    return confirmed if not confirmed.empty else None


@retry_on_failure(3, 0.5)
def _fetch_live_hl_raw(instId: str) -> tuple[float, float] | None:
    """抓取當前未完成 K 棒的即時 High/Low"""
    url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar=15m&limit=3"
    res = requests.get(url, timeout=5).json()
    for row in res['data']:
        if row[8] == "0":
            return float(row[3]), float(row[2])   # low, high
    return None


def fetch_live_hl(instId: str) -> tuple[float, float]:
    result = _fetch_live_hl_raw(instId)
    return result if result is not None else (float('inf'), float('-inf'))


@retry_on_failure(3, 1.0)
def _fetch_funding_ls_raw(instId: str) -> tuple[str, str]:
    base_id  = instId.replace("-SWAP", "").split("-")[0]
    funding  = "N/A"
    ls_ratio = "N/A"
    try:
        f_res = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
        ).json()
        funding = f"{float(f_res['data'][0]['fundingRate']) * 100:.4f}%"
    except Exception:
        pass
    try:
        ls_res = requests.get(
            f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio"
            f"?instId={base_id}", timeout=5
        ).json()
        ls_ratio = ls_res['data'][0]['ratio']
    except Exception:
        pass
    return funding, ls_ratio


def get_funding_ls(instId: str) -> tuple[str, str]:
    r = _fetch_funding_ls_raw(instId)
    return r if r is not None else ("N/A", "N/A")


@retry_on_failure(3, 1.0)
def _fetch_funding_rate_raw(instId: str) -> float | None:
    res = requests.get(
        f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5
    ).json()
    return float(res['data'][0]['fundingRate'])


def fetch_funding_rate(instId: str) -> float | None:
    return _fetch_funding_rate_raw(instId)


@retry_on_failure(3, 1.0)
def _fetch_ob_raw(instId: str, depth: int = 20) -> tuple[float, str] | None:
    url = f"https://www.okx.com/api/v5/market/books?instId={instId}&sz={depth}"
    res = requests.get(url, timeout=5).json()
    if res['code'] != '0' or not res['data']:
        return None
    data    = res['data'][0]
    bid_vol = sum(float(b[1]) for b in data['bids'])
    ask_vol = sum(float(a[1]) for a in data['asks'])
    if ask_vol == 0:
        return 1.0, "⚪ 盤口均衡"
    ratio = bid_vol / ask_vol
    if   ratio > 1.2: label = f"🟢 買盤強勢 ({ratio:.2f})"
    elif ratio < 0.8: label = f"🔴 賣盤強勢 ({ratio:.2f})"
    else:             label = f"⚪ 盤口均衡 ({ratio:.2f})"
    return ratio, label


def fetch_order_book(instId: str) -> tuple[float, str]:
    r = _fetch_ob_raw(instId)
    return r if r is not None else (1.0, "⚪ 盤口均衡（數據缺失）")


# ─────────────────────────────────────────────
# 3.5 主力數據框架 + 背離分析
# ─────────────────────────────────────────────

@retry_on_failure(3, 1.0)
def fetch_coinank_spot_cvd(symbol: str) -> dict | None:
    if not COINANK_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {COINANK_API_KEY}"}
    res     = requests.get(
        "https://api.coinank.com/api/indicators/spot-cvd",
        params={"symbol": symbol, "period": "24h"},
        headers=headers, timeout=10
    ).json()
    if res.get('code') == 200 and res.get('data'):
        v = float(res['data']['cvd_value'])
        return {"cvd": v, "trend": "bullish" if v > 0 else "bearish"}
    return None


@retry_on_failure(3, 1.0)
def fetch_glassnode_whale_flow(symbol: str) -> dict | None:
    if not GLASSNODE_API_KEY:
        return None
    url = (f"https://rest.glassnode.com/v1/metrics/transfers/exchange_net_flow"
           f"?asset={symbol}&resolution=24h&api_key={GLASSNODE_API_KEY}")
    res = requests.get(url, timeout=10).json()
    if res and len(res) > 0:
        flow = res[-1]['value']
        return {"net_flow": flow, "signal": "inflow" if flow > 0 else "outflow"}
    return None


@retry_on_failure(3, 1.0)
def fetch_cryptoquant_oi(symbol: str) -> dict | None:
    if not CRYPTOQUANT_API_KEY:
        return None
    url = (f"https://api.cryptoquant.com/v1/data/bitcoin/metrics/open-interest"
           f"?api_key={CRYPTOQUANT_API_KEY}")
    res = requests.get(url, timeout=10).json()
    if res and 'data' in res and len(res['data']) >= 2:
        d    = res['data']
        chg  = (d[-1]['value'] - d[-2]['value']) / (abs(d[-2]['value']) + 1e-10)
        sig  = "rising" if chg > 0.05 else ("falling" if chg < -0.05 else "stable")
        return {"oi_change": chg, "signal": sig}
    return None


def check_data_divergence(
    instId: str,
    side:   str,
    df:     pd.DataFrame
) -> tuple[bool, str]:
    """
    🆕 整合版數據背離檢測（修復 v5.0 語法錯誤版本）
    從 OKX 抓資金費率 + 多空比，從 CoinAnk 抓 CVD（有 API key 才用）
    返回 (is_divergent, description)
    """
    symbol = instId.split('-')[0]

    # 收集可用數據
    fr = fetch_funding_rate(instId)
    _, ls_str = get_funding_ls(instId)
    try:
        ls = float(ls_str)
    except Exception:
        ls = 1.0

    cvd_data = fetch_coinank_spot_cvd(symbol)
    cvd_bull  = cvd_data is not None and cvd_data['trend'] == "bullish"
    cvd_bear  = cvd_data is not None and cvd_data['trend'] == "bearish"

    # 技術面 CVD 作為備用
    tech_cvd_val, _ = calculate_cvd(df)

    signals     = []
    div_score   = 0.0

    if side == "LONG":
        # 多頭背離：主力暗中吸籌
        if cvd_bull or tech_cvd_val > 0:
            signals.append("🟢 CVD 吸籌"); div_score += 0.35
        if ls < 0.9:
            signals.append(f"🟢 散戶做空(LS={ls:.2f})"); div_score += 0.30
        if fr is not None and fr < -0.0003:
            signals.append(f"🟢 負費率誘多"); div_score += 0.25
        elif fr is not None and fr < 0.0002:
            signals.append("⚪ 費率偏低"); div_score += 0.10

    else:  # SHORT
        # 空頭背離：主力暗中派發
        if cvd_bear or tech_cvd_val < 0:
            signals.append("🔴 CVD 出貨"); div_score += 0.35
        if ls > 1.1:
            signals.append(f"🔴 散戶追多(LS={ls:.2f})"); div_score += 0.30
        if fr is not None and fr > 0.0005:
            signals.append(f"🔴 高費率擠泡沫"); div_score += 0.25
        elif fr is not None and fr > 0.0002:
            signals.append("⚪ 費率偏高"); div_score += 0.10

    is_div   = div_score >= 0.50
    desc     = " | ".join(signals) if signals else "─"
    div_type = ("✅ 背離確認" if is_div else "⚠️ 背離不足")

    return is_div, f"{div_type}（{desc}）"


def analyze_whale_direction(
    instId: str, side: str, opt: dict, df: pd.DataFrame = None
) -> tuple[str, float, str, str]:
    symbol      = instId.split('-')[0]
    spot_cvd    = fetch_coinank_spot_cvd(symbol)
    whale_flow  = fetch_glassnode_whale_flow(symbol)
    oi_data     = fetch_cryptoquant_oi(symbol)

    data_ok = sum([spot_cvd is not None, whale_flow is not None, oi_data is not None])

    if data_ok == 0 and df is not None:
        conf = _tech_confidence(df, side)
        if   conf >= 0.65: return "✅ 技術面主導", conf, "主力數據缺失，技術面極強",   "Technical"
        elif conf >= 0.45: return "⚠️ 技術面中等", conf, "主力數據缺失，建議降低倉位", "LowConf"
        else:              return "🔴 技術面弱",   conf, "主力數據缺失，建議跳過",     "Skip"

    _, ls_str = get_funding_ls(instId)
    try:    ls = float(ls_str)
    except: ls = 1.0

    signals, conf, cat = [], 0.0, "Aligned"

    if spot_cvd:
        if   side == "LONG"  and spot_cvd['trend'] == "bearish": signals.append("🔴 出貨"); conf += 0.35; cat = "Reverse"
        elif side == "SHORT" and spot_cvd['trend'] == "bullish": signals.append("🟢 吸籌"); conf += 0.35; cat = "Reverse"
        else: conf += 0.10

    if whale_flow:
        if   side == "LONG"  and whale_flow['signal'] == "inflow":  signals.append("🔴 鯨魚流入"); conf += 0.25
        elif side == "SHORT" and whale_flow['signal'] == "outflow": signals.append("🟢 鯨魚鎖倉"); conf += 0.25

    if oi_data:
        if   side == "SHORT" and oi_data['signal'] == "rising":  signals.append("🔴 OI 激增"); conf += 0.20
        elif side == "LONG"  and oi_data['signal'] == "falling": conf -= 0.10

    if   ls > 1.1 and side == "LONG":  signals.append("🔴 散戶過多"); conf += 0.15; cat = "Reverse"
    elif ls < 0.9 and side == "SHORT": signals.append("🟢 散戶看空"); conf += 0.15; cat = "Reverse"

    dyn = get_dynamic_threshold(opt)
    conf = max(0.0, min(1.0, conf))

    if cat == "Reverse" and conf >= dyn:
        return "🔴 主力反向", conf, f"主力反向（{conf*100:.0f}%）", "Reverse"
    elif conf >= 0.5:
        return "⚠️ 主力警示", conf, f"主力存在衝突（{conf*100:.0f}%）", "Warning"
    else:
        return "✅ 主力一致", conf, f"主力順向（{conf*100:.0f}%）", "Aligned"


def _tech_confidence(df: pd.DataFrame, side: str) -> float:
    s = 0.0
    if (side == "LONG" and calculate_supertrend(df) == 1) or (side == "SHORT" and calculate_supertrend(df) == -1):
        s += 0.30
    rsi = calculate_rsi(df)
    if 40 <= rsi <= 60: s += 0.20
    price, ema200 = df['c'].iloc[-1], calculate_ema(df, 200)
    if (side == "LONG" and price > ema200) or (side == "SHORT" and price < ema200): s += 0.20
    cvd_val, _ = calculate_cvd(df)
    if (side == "LONG" and cvd_val > 0) or (side == "SHORT" and cvd_val < 0): s += 0.15
    pa, _ = calculate_pa_score(df, side)
    s += 0.15 * pa
    return min(1.0, s)


def detect_whale_entry_zones(df: pd.DataFrame, side: str) -> list[dict]:
    zones  = []
    vol_ma = df['v'].rolling(20).mean()
    vol_sd = df['v'].rolling(20).std()
    for i in range(max(len(df) - 10, 0), len(df)):
        if df['v'].iloc[i] > vol_ma.iloc[i] + 2 * vol_sd.iloc[i]:
            if df['c'].iloc[i] > df['o'].iloc[i] and side == "LONG":
                zones.append({"type": "whale_accumulation", "price": df['c'].iloc[i],
                               "desc": f"🐋 主力吸籌 {df['c'].iloc[i]:.4f}"})
            elif df['c'].iloc[i] < df['o'].iloc[i] and side == "SHORT":
                zones.append({"type": "whale_distribution", "price": df['c'].iloc[i],
                               "desc": f"🐋 主力派發 {df['c'].iloc[i]:.4f}"})
    recent_high = df['h'].iloc[-20:].max()
    recent_low  = df['l'].iloc[-20:].min()
    zones.append({
        "type":  "liquidation_cluster",
        "price": recent_high if side == "SHORT" else recent_low,
        "desc":  f"💥 清算熱點 {recent_high if side=='SHORT' else recent_low:.4f}"
    })
    return zones[:3]


# ─────────────────────────────────────────────
# 4. 技術指標
# ─────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    v  = tr.rolling(period).mean().iloc[-1]
    return float(v) if not np.isnan(v) else 0.001


def calculate_cvd(df: pd.DataFrame, lookback: int = 20) -> tuple[float, str]:
    r   = df.tail(lookback).copy()
    b   = (r['h'] - r['l']).replace(0, 1e-10)
    r['delta'] = np.where(
        r['c'] >= r['o'],
        r['v'] * (r['c'] - r['l']) / b,
        -r['v'] * (r['h'] - r['c']) / b
    )
    cvd = r['delta'].sum()
    return cvd, ("🟢 大戶吸籌 (CVD+)" if cvd > 0 else "🔴 大戶出貨 (CVD-)")


def calculate_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> int:
    if len(df) < period + 2: return 0
    h, l, c = df['h'].values.astype(float), df['l'].values.astype(float), df['c'].values.astype(float)
    n  = len(df)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    atr = np.zeros(n); atr[period] = tr[1:period+1].mean()
    for i in range(period+1, n): atr[i] = (atr[i-1]*(period-1)+tr[i])/period
    hl2 = (h+l)/2.0
    bu  = hl2 - mult*atr
    bd  = hl2 + mult*atr
    fu  = np.zeros(n); fd = np.zeros(n); t = np.ones(n, dtype=int)
    fu[period] = bu[period]; fd[period] = bd[period]
    for i in range(period+1, n):
        fu[i] = bu[i] if bu[i]>fu[i-1] or c[i-1]<fu[i-1] else fu[i-1]
        fd[i] = bd[i] if bd[i]<fd[i-1] or c[i-1]>fd[i-1] else fd[i-1]
        if   t[i-1]==-1 and c[i]>fd[i-1]: t[i]=1
        elif t[i-1]==1  and c[i]<fu[i-1]: t[i]=-1
        else: t[i]=t[i-1]
    return int(t[-1])


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period+2: return 50.0
    delta = df['c'].diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rsi   = 100 - (100 / (1 + gain/(loss+1e-10)))
    v     = rsi.iloc[-1]
    return float(v) if not np.isnan(v) else 50.0


def calculate_ema(df: pd.DataFrame, period: int = 200) -> float:
    if len(df) < period//2: return float(df['c'].iloc[-1])
    v = df['c'].ewm(span=period, adjust=False).mean().iloc[-1]
    return float(v) if not np.isnan(v) else float(df['c'].iloc[-1])


def is_volume_spike(df: pd.DataFrame, mult: float = 1.5) -> bool:
    if len(df) < 21: return True
    return float(df['v'].iloc[-1]) > df['v'].rolling(20).mean().iloc[-1] * mult


def is_active_session() -> bool:
    if not SESSION_FILTER_ENABLED: return True
    h = datetime.utcnow().hour
    return (SESSION_LONDON_START <= h < SESSION_LONDON_END or
            SESSION_NY_START     <= h < SESSION_NY_END)


def get_1h_trend(instId: str, side: str) -> tuple[bool, str]:
    """真實 1H 趨勢確認（fail-open：API 失敗時允許通過）"""
    df1h = fetch_okx_1h(instId, 60)
    if df1h is None or len(df1h) < 15:
        return True, "⚠️ 1H 數據不足，跳過確認"

    st   = calculate_supertrend(df1h)
    ema50 = calculate_ema(df1h, 50)
    rsi1h = calculate_rsi(df1h, 14)
    price = float(df1h['c'].iloc[-1])
    score = 0
    parts = []

    if side == "LONG":
        if st == 1:            score += 2; parts.append("ST↑")
        elif st == -1:         score -= 2; parts.append("ST↓")
        if price > ema50:      score += 1; parts.append("EMA50↑")
        else:                  score -= 1; parts.append("EMA50↓")
        if rsi1h >= 45:        score += 1; parts.append(f"RSI={rsi1h:.0f}")
        else:                  score -= 1; parts.append(f"RSI={rsi1h:.0f}⚠️")
    else:
        if st == -1:           score += 2; parts.append("ST↓")
        elif st == 1:          score -= 2; parts.append("ST↑")
        if price < ema50:      score += 1; parts.append("EMA50↓")
        else:                  score -= 1; parts.append("EMA50↑")
        if rsi1h <= 55:        score += 1; parts.append(f"RSI={rsi1h:.0f}")
        else:                  score -= 1; parts.append(f"RSI={rsi1h:.0f}⚠️")

    icon = "✅" if score >= 2 else ("⚠️" if score >= 0 else "❌")
    return score >= 0, f"{icon} 1H({' '.join(parts)}) 分={score}"


# ─────────────────────────────────────────────
# 4.5 價格行為學（PA）模組
# ─────────────────────────────────────────────

def detect_pin_bar(df: pd.DataFrame, lookback: int = 3) -> dict:
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k = df.iloc[i]
        body = abs(k['c']-k['o']); rng = k['h']-k['l']+1e-10
        if body < rng*0.05: continue
        uw = k['h']-max(k['c'],k['o']); lw = min(k['c'],k['o'])-k['l']
        if lw >= body*2.0 and uw <= body*0.5:
            return {"detected": True, "type": "bullish_pin",  "strength": min(lw/(body+1e-10)/5,1.0), "desc": f"📌 錘子({lw/body:.1f}R)@{k['c']:.4f}"}
        if uw >= body*2.0 and lw <= body*0.5:
            return {"detected": True, "type": "bearish_pin",  "strength": min(uw/(body+1e-10)/5,1.0), "desc": f"📌 流星({uw/body:.1f}R)@{k['c']:.4f}"}
    return {"detected": False, "type": None, "strength": 0, "desc": ""}


def detect_engulfing(df: pd.DataFrame, lookback: int = 3) -> dict:
    for i in range(len(df)-1, max(len(df)-lookback-1, 1), -1):
        c, p = df.iloc[i], df.iloc[i-1]
        cb, pb = abs(c['c']-c['o']), abs(p['c']-p['o'])
        if pb < 1e-10: continue
        if c['c']>c['o'] and p['c']<p['o'] and c['o']<=p['c'] and c['c']>=p['o']:
            return {"detected": True, "type": "bullish_engulfing", "strength": min(cb/(pb+1e-10)/3,1.0), "desc": f"🕯️ 多吞噬({cb/pb:.1f}x)@{c['c']:.4f}"}
        if c['c']<c['o'] and p['c']>p['o'] and c['o']>=p['c'] and c['c']<=p['o']:
            return {"detected": True, "type": "bearish_engulfing", "strength": min(cb/(pb+1e-10)/3,1.0), "desc": f"🕯️ 空吞噬({cb/pb:.1f}x)@{c['c']:.4f}"}
    return {"detected": False, "type": None, "strength": 0, "desc": ""}


def detect_rejection_candle(df: pd.DataFrame, side: str) -> dict:
    k = df.iloc[-1]
    rng = k['h']-k['l']+1e-10
    uw, lw = k['h']-max(k['c'],k['o']), min(k['c'],k['o'])-k['l']
    if side == "LONG"  and lw/rng > 0.40 and k['c'] > k['o']:
        return {"detected": True, "strength": lw/rng, "desc": f"🔄 支撐拒絕(下影{lw/rng*100:.0f}%)@{k['c']:.4f}"}
    if side == "SHORT" and uw/rng > 0.40 and k['c'] < k['o']:
        return {"detected": True, "strength": uw/rng, "desc": f"🔄 壓力拒絕(上影{uw/rng*100:.0f}%)@{k['c']:.4f}"}
    return {"detected": False, "strength": 0, "desc": ""}


def detect_momentum_bar(df: pd.DataFrame, side: str, lookback: int = 5) -> dict:
    atr = calculate_atr(df)
    for i in range(len(df)-1, max(len(df)-lookback-1, 0), -1):
        k = df.iloc[i]
        body = abs(k['c']-k['o']); rng = k['h']-k['l']+1e-10
        bp = body/rng
        if bp >= 0.70 and body >= atr*0.8:
            bull = k['c'] > k['o']
            if (side=="LONG" and bull) or (side=="SHORT" and not bull):
                return {"detected": True, "strength": bp, "desc": f"⚡ {'多頭' if bull else '空頭'}動量棒({bp*100:.0f}%)@{k['c']:.4f}"}
    return {"detected": False, "strength": 0, "desc": ""}


def detect_false_breakout(df: pd.DataFrame, side: str, lookback: int = 10) -> dict:
    if len(df) < lookback+2: return {"detected": False}
    recent = df.tail(lookback)
    rh, rl = recent['h'].iloc[:-1].max(), recent['l'].iloc[:-1].min()
    if side == "LONG":
        for i in range(len(df)-3, max(len(df)-lookback-1, 1), -1):
            k = df.iloc[i]
            if k['l'] < rl and k['c'] > rl:
                return {"detected": True, "desc": f"🪤 熊陷阱({k['l']:.4f}→{k['c']:.4f})"}
    elif side == "SHORT":
        for i in range(len(df)-3, max(len(df)-lookback-1, 1), -1):
            k = df.iloc[i]
            if k['h'] > rh and k['c'] < rh:
                return {"detected": True, "desc": f"🪤 牛陷阱({k['h']:.4f}→{k['c']:.4f})"}
    return {"detected": False}


def detect_inside_bar(df: pd.DataFrame) -> dict:
    if len(df) < 2: return {"detected": False}
    c, p = df.iloc[-1], df.iloc[-2]
    if c['h'] <= p['h'] and c['l'] >= p['l']:
        comp = 1.0 - (c['h']-c['l'])/(p['h']-p['l']+1e-10)
        return {"detected": True, "compression": comp, "desc": f"📦 內包棒({comp*100:.0f}%)"}
    return {"detected": False}


def calculate_pa_score(df: pd.DataFrame, side: str) -> tuple[float, list[str]]:
    score, sigs = 0.0, []

    pin = detect_pin_bar(df)
    if pin['detected']:
        al = (side=="LONG" and pin['type']=="bullish_pin") or (side=="SHORT" and pin['type']=="bearish_pin")
        if al: score += 0.25*pin['strength']; sigs.append(pin['desc'])
        elif pin['strength'] > 0.6: score -= 0.10; sigs.append(f"⚠️反向{pin['desc']}")

    eng = detect_engulfing(df)
    if eng['detected']:
        al = (side=="LONG" and eng['type']=="bullish_engulfing") or (side=="SHORT" and eng['type']=="bearish_engulfing")
        if al: score += 0.20*eng['strength']; sigs.append(eng['desc'])
        else:  score -= 0.05; sigs.append(f"⚠️反向{eng['desc']}")

    rej = detect_rejection_candle(df, side)
    if rej['detected']: score += 0.20*rej['strength']; sigs.append(rej['desc'])

    mom = detect_momentum_bar(df, side)
    if mom['detected']: score += 0.15*mom['strength']; sigs.append(mom['desc'])

    fbo = detect_false_breakout(df, side)
    if fbo['detected']: score += 0.15; sigs.append(fbo['desc'])

    ib = detect_inside_bar(df)
    if ib['detected']: score += 0.10*ib['compression']; sigs.append(ib['desc'])

    pos = len([s for s in sigs if not s.startswith("⚠️")])
    if pos >= 3: score += 0.10
    elif pos >= 2: score += 0.05

    return max(0.0, min(1.0, score)), sigs


def pa_in_zone(df: pd.DataFrame, zone: dict, side: str) -> bool:
    """
    🆕 v5.0 概念：PA 必須在 SMC 區域內才觸發（in-zone PA confirmation）
    """
    k = df.iloc[-1]
    in_zone = (k['l'] <= zone['high'] and k['h'] >= zone['low'])
    if not in_zone:
        return False

    pin = detect_pin_bar(df, lookback=2)
    eng = detect_engulfing(df, lookback=2)

    if side == "LONG":
        if pin['detected'] and pin['type'] == "bullish_pin":  return True
        if eng['detected'] and eng['type'] == "bullish_engulfing": return True
    else:
        if pin['detected'] and pin['type'] == "bearish_pin":  return True
        if eng['detected'] and eng['type'] == "bearish_engulfing": return True

    return False


# ─────────────────────────────────────────────
# 4.6 評分系統
# ─────────────────────────────────────────────

def calculate_setup_score(
    setup:      dict,
    df:         pd.DataFrame,
    rsi_val:    float,
    ema200:     float,
    vol_spike:  bool,
    h1_aligned: bool,
    is_div:     bool
) -> float:
    """
    v5.1 完整評分（0-1）
    主力 30% | PA 20% | ST 15% | 1H 15% | CVD 10% | EMA+RSI 5% | Vol+背離 5%
    """
    s    = 0.0
    side = setup.get('side', '')

    if setup['whale_signal'] in ("✅ 主力一致", "✅ 技術面主導"):
        s += 0.30 * setup['whale_confidence']
    elif setup['whale_signal'] in ("⚠️ 主力警示", "⚠️ 技術面中等"):
        s += 0.15 * setup['whale_confidence']

    s += 0.20 * setup['pa_score']

    if (setup.get('st_val') == 1 and side == "LONG") or (setup.get('st_val') == -1 and side == "SHORT"):
        s += 0.15

    if h1_aligned: s += 0.15

    if setup.get('cvd_label', '').startswith("🟢") and side == "LONG":  s += 0.10
    elif setup.get('cvd_label', '').startswith("🔴") and side == "SHORT": s += 0.10

    price = df['c'].iloc[-1]
    if (side == "LONG" and price > ema200) or (side == "SHORT" and price < ema200): s += 0.025
    if side == "LONG"  and 35 <= rsi_val <= RSI_OVERBOUGHT: s += 0.025
    if side == "SHORT" and RSI_OVERSOLD <= rsi_val <= 65:   s += 0.025

    if vol_spike: s += 0.025
    if is_div:    s += 0.05

    return min(1.0, s)


# ─────────────────────────────────────────────
# 5. SMC & ICT 結構分析
# ─────────────────────────────────────────────

def find_swing_points(df: pd.DataFrame, n: int = 2, lookback: int = 80) -> tuple[list, list]:
    data = df.tail(lookback).reset_index(drop=True)
    sh, sl = [], []
    for i in range(n, len(data)-n):
        if data['h'].iloc[i] == data['h'].iloc[i-n:i+n+1].max(): sh.append(data['h'].iloc[i])
        if data['l'].iloc[i] == data['l'].iloc[i-n:i+n+1].min(): sl.append(data['l'].iloc[i])
    return sorted(set(sh)), sorted(set(sl))


def detect_market_structure(df: pd.DataFrame, side: str = None) -> str:
    sh, sl = find_swing_points(df, 3, 60)
    has_w  = len(sl) >= 2 and sl[-2] > 0 and abs(sl[-2]-sl[-1])/sl[-2] < 0.015
    has_m  = len(sh) >= 2 and sh[-2] > 0 and abs(sh[-2]-sh[-1])/sh[-2] < 0.015
    if side == "LONG":
        if has_w: return "W 底反轉 📐"
        if has_m: return "M 頭壓制 ⚠️"
    elif side == "SHORT":
        if has_m: return "M 頭反轉 📐"
        if has_w: return "W 底支撐 ⚠️"
    if has_w: return "W 底反轉 📐"
    if has_m: return "M 頭反轉 📐"
    slope = (df.tail(20)['c'].iloc[-1]-df.tail(20)['c'].iloc[0])/(df.tail(20)['c'].iloc[0]+1e-10)
    if slope >  0.025: return "上升趨勢延續 📈"
    if slope < -0.025: return "下降趨勢延續 📉"
    return "區間盤整 ↔️"


def find_order_block_zone(df: pd.DataFrame, side: str, lookback: int = 50) -> dict | None:
    """
    🆕 v5.0 改進：返回 OB 區間 + 50% Mean Threshold
    進場在 50% mean 等待回踩，比直接用 OB high/low 更精準
    """
    data = df.tail(lookback).reset_index(drop=True)
    for i in range(len(data)-2, 0, -1):
        k, kn = data.iloc[i], data.iloc[i+1]
        if side == "LONG"  and k['c'] < k['o'] and kn['c'] > kn['o']:
            high, low = k['o'], k['l']
            return {"high": high, "low": low, "mean": (high+low)/2, "type": "OB"}
        if side == "SHORT" and k['c'] > k['o'] and kn['c'] < kn['o']:
            high, low = k['h'], k['c']
            return {"high": high, "low": low, "mean": (high+low)/2, "type": "OB"}
    return None


def find_valid_fvg(df: pd.DataFrame, side: str, atr: float) -> dict | None:
    """
    🆕 v5.0 改進：FVG 必須 > 1.5 ATR 才算有效（過濾假缺口）
    進場在 FVG midpoint（中點）
    """
    for i in range(len(df)-3, max(len(df)-50, 1), -1):
        k0, k2 = df.iloc[i-1], df.iloc[i+1]
        if side == "LONG"  and k2['l'] > k0['h']:
            gap = k2['l'] - k0['h']
            if gap > MIN_FVG_ATR_MULT * atr:
                mid = (k2['l'] + k0['h']) / 2
                return {"high": k2['l'], "low": k0['h'], "mean": mid, "type": "FVG"}
        if side == "SHORT" and k2['h'] < k0['l']:
            gap = k0['l'] - k2['h']
            if gap > MIN_FVG_ATR_MULT * atr:
                mid = (k0['l'] + k2['h']) / 2
                return {"high": k0['l'], "low": k2['h'], "mean": mid, "type": "FVG"}
    return None


def find_ict_snr_zones(df: pd.DataFrame, side: str) -> dict | None:
    sh, sl = find_swing_points(df, 2, 30)
    price  = df['c'].iloc[-1]
    if side == "LONG":
        valid = [s for s in sl if s < price*0.995]
        if valid:
            s = max(valid)
            return {"support": s, "resistance": None, "active_level": s,
                    "type": "support", "text": f"支撐 {s:.4f}"}
    else:
        valid = [r for r in sh if r > price*1.005]
        if valid:
            r = min(valid)
            return {"support": None, "resistance": r, "active_level": r,
                    "type": "resistance", "text": f"壓力 {r:.4f}"}
    return None


def calculate_structural_sl(df: pd.DataFrame, side: str, entry: float, atr: float) -> float:
    buf = atr * 0.25
    ob  = find_order_block_zone(df, side)
    fvg = find_valid_fvg(df, side, atr)
    snr = find_ict_snr_zones(df, side)
    if side == "LONG":
        cands = []
        if ob  and ob['low']  < entry: cands.append(ob['low']  - buf)
        if fvg and fvg['low'] < entry: cands.append(fvg['low'] - buf)
        if snr and snr.get('active_level') and snr['active_level'] < entry:
            cands.append(snr['active_level'] - buf)
        if cands:
            sl = max(cands)
            return sl if (entry-sl)/(entry+1e-10) >= 0.004 else entry-atr*1.5
        return entry - atr*1.5
    else:
        cands = []
        if ob  and ob['high']  > entry: cands.append(ob['high']  + buf)
        if fvg and fvg['high'] > entry: cands.append(fvg['high'] + buf)
        if snr and snr.get('active_level') and snr['active_level'] > entry:
            cands.append(snr['active_level'] + buf)
        if cands:
            sl = min(cands)
            return sl if (sl-entry)/(entry+1e-10) >= 0.004 else entry+atr*1.5
        return entry + atr*1.5


def get_fixed_r_tps(entry: float, sl: float, side: str) -> tuple[float, float, float]:
    risk = abs(entry-sl)+1e-10
    if side == "LONG":  return entry+risk, entry+risk*2, entry+risk*3
    else:               return entry-risk, entry-risk*2, entry-risk*3


def suggest_leverage(atr: float, price: float, whale_conf: float = 0.5) -> tuple[str, str]:
    vol = (atr/(price+1e-10))*100
    if whale_conf < 0.4:
        if vol > 3:   return "2x~3x",   "⚠️ 主力不明+高波動"
        if vol > 1.5: return "3x~5x",   "⚠️ 主力不明+中波動"
        return           "5x~8x",   "⚠️ 主力不明+低波動"
    if vol > 3:   return "3x~5x",   "⚠️ 高波動"
    if vol > 1.5: return "5x~10x",   "中波動"
    return           "10x~20x", "低波動"


def classify_trade(side: str, structure: str, risk_pct: float) -> str:
    if "反轉" in structure: return "📊 長單(波段)"
    if risk_pct < 1.0:      return "⚡ 短單(日內)"
    return "📊 長單(波段)"


# ─────────────────────────────────────────────
# 6. 過濾器
# ─────────────────────────────────────────────

def is_trending_market(df: pd.DataFrame) -> bool:
    if len(df) < 50: return True
    hl = df['h']-df['l']
    hc = np.abs(df['h']-df['c'].shift())
    lc = np.abs(df['l']-df['c'].shift())
    tr = pd.concat([hl,hc,lc],axis=1).max(axis=1)
    return tr.rolling(14).mean().iloc[-1] > tr.tail(50).mean()*0.7


def get_btc_direction(df: pd.DataFrame, lookback: int = 5) -> str:
    if df is None or len(df) < lookback: return "NEUTRAL"
    r = df.tail(lookback)
    b = int((r['c'] < r['o']).sum())
    if b >= 4: return "DOWN"
    if (lookback-b) >= 4: return "UP"
    return "NEUTRAL"


# ─────────────────────────────────────────────
# 7. 核心訊號掃描（v5.1 全整合版）
# ─────────────────────────────────────────────

ENTRY_SRC_EMOJI = {
    "OB": "🧱", "FVG": "🕳️", "Breakout": "⚡",
    "Whale-whale_accumulation": "🐋", "Whale-whale_distribution": "🐋",
    "Whale-liquidation_cluster": "💥",
}
ENTRY_SRC_TEXT = {
    "OB": "OB 訂單塊", "FVG": "FVG 缺口", "Breakout": "突破點",
    "Whale-whale_accumulation": "主力吸籌", "Whale-whale_distribution": "主力派發",
    "Whale-liquidation_cluster": "清算熱點",
}
WHALE_EMOJI = {
    "✅ 主力一致": "🐋", "✅ 技術面主導": "📡",
    "⚠️ 主力警示": "⚠️", "⚠️ 技術面中等": "⚠️",
    "🔴 主力反向": "🚫", "🔴 技術面弱": "🚫",
}


def find_smc_setup(
    df: pd.DataFrame, instId: str, opt: dict, current_bar: int
) -> dict | None:
    """
    v5.1 完整整合訊號掃描
    過濾順序：冷卻→時段→EMA200→RSI→ST→Volume→1H→PA(in-zone)→背離→評分
    """
    if df is None or len(df) < 50: return None

    # 1. 冷卻期
    if is_in_cooldown(instId, current_bar):
        return None

    # 2. 時段
    if not is_active_session():
        return None

    atr   = calculate_atr(df)
    price = df['c'].iloc[-1]

    # 3. 找有效 SMC 區域（OB 優先，FVG 備用）
    ob_long  = find_order_block_zone(df, "LONG")
    ob_short = find_order_block_zone(df, "SHORT")
    fvg_long = find_valid_fvg(df, "LONG",  atr)
    fvg_short= find_valid_fvg(df, "SHORT", atr)

    # 確定訊號方向和候選區域
    candidates = []

    if ob_long and pa_in_zone(df, ob_long, "LONG"):
        candidates.append(("LONG", ob_long, "OB"))
    if fvg_long and pa_in_zone(df, fvg_long, "LONG"):
        candidates.append(("LONG", fvg_long, "FVG"))
    if ob_short and pa_in_zone(df, ob_short, "SHORT"):
        candidates.append(("SHORT", ob_short, "OB"))
    if fvg_short and pa_in_zone(df, fvg_short, "SHORT"):
        candidates.append(("SHORT", fvg_short, "FVG"))

    if not candidates:
        return None

    # 4. BOS 確認（確保有真實突破）
    valid_candidates = []
    for side, zone, zone_type in candidates:
        if side == "LONG":
            bos = df['c'].iloc[-1] > df['h'].iloc[-20:-1].max()
        else:
            bos = df['c'].iloc[-1] < df['l'].iloc[-20:-1].min()
        if bos:
            valid_candidates.append((side, zone, zone_type))

    if not valid_candidates:
        return None

    side, zone, zone_type = valid_candidates[0]

    # 5. EMA-200 方向
    ema200 = calculate_ema(df, 200)
    if side == "LONG"  and price < ema200:
        logging.info(f"[{instId}] EMA200 反向(多)，跳過"); return None
    if side == "SHORT" and price > ema200:
        logging.info(f"[{instId}] EMA200 反向(空)，跳過"); return None

    # 6. RSI
    rsi_val = calculate_rsi(df)
    if side == "LONG"  and rsi_val > RSI_OVERBOUGHT:
        logging.info(f"[{instId}] RSI={rsi_val:.1f} 過熱，跳過"); return None
    if side == "SHORT" and rsi_val < RSI_OVERSOLD:
        logging.info(f"[{instId}] RSI={rsi_val:.1f} 過冷，跳過"); return None

    # 7. Supertrend
    st_val = calculate_supertrend(df)
    if st_val == -1 and side == "LONG":
        logging.info(f"[{instId}] ST 空頭，跳過 LONG"); return None
    if st_val ==  1 and side == "SHORT":
        logging.info(f"[{instId}] ST 多頭，跳過 SHORT"); return None
    st_label = "📈 多頭" if st_val == 1 else ("📉 空頭" if st_val == -1 else "⚪ 未知")

    # 8. Volume
    vol_spike = is_volume_spike(df)
    if not vol_spike:
        logging.info(f"[{instId}] 成交量不足，跳過"); return None

    # 9. 1H 趨勢
    h1_aligned, h1_desc = get_1h_trend(instId, side)
    if not h1_aligned:
        logging.info(f"[{instId}] 1H 反向：{h1_desc}"); return None

    # 10. PA 評分
    pa_score, pa_sigs = calculate_pa_score(df, side)
    if pa_score < PA_MIN_SCORE:
        logging.info(f"[{instId}] PA={pa_score:.2f} 不足"); return None

    # 11. 數據背離
    is_div, div_desc = check_data_divergence(instId, side, df)

    # 12. CVD / 主力
    cvd_val, cvd_label = calculate_cvd(df)
    whale_signal, whale_conf, whale_desc, whale_cat = analyze_whale_direction(instId, side, opt, df)

    # 13. 評分
    temp = {
        'whale_signal': whale_signal, 'whale_confidence': whale_conf,
        'pa_score': pa_score, 'side': side, 'cvd_label': cvd_label, 'st_val': st_val,
    }
    setup_score = calculate_setup_score(temp, df, rsi_val, ema200, vol_spike, h1_aligned, is_div)
    if setup_score < SETUP_SCORE_THRESHOLD:
        logging.info(f"[{instId}] 評分{setup_score:.2f}<{SETUP_SCORE_THRESHOLD}，跳過"); return None

    # 14. 進場價 = 區域 50% Mean（OB/FVG 統一）
    entry        = zone['mean']
    entry_source = zone_type

    # 確認進場價距當前價合理
    if abs(entry - price) / price > 0.04:
        # 超過 4% 就用 mean 但標注過遠
        entry_source = f"{zone_type} (過遠)"

    sl            = calculate_structural_sl(df, side, entry, atr)
    tp1, tp2, tp3 = get_fixed_r_tps(entry, sl, side)
    risk_pct      = abs(entry-sl)/(entry+1e-10)*100
    structure     = detect_market_structure(df, side)
    lev, lev_note = suggest_leverage(atr, price, whale_conf)
    trade_type    = classify_trade(side, structure, risk_pct)
    snr_zone      = find_ict_snr_zones(df, side)
    whale_zones   = detect_whale_entry_zones(df, side)

    if snr_zone:
        s_txt = f"{snr_zone['support']:.4f}"    if snr_zone.get('support')    else "─"
        r_txt = f"{snr_zone['resistance']:.4f}" if snr_zone.get('resistance') else "─"
        snr_display = f"🟢 支撐 {s_txt} | 🔴 壓力 {r_txt}"
        snr_active  = f"✅ 參考 {snr_zone['text']}"
    else:
        snr_display = "🟢 支撐 ─ | 🔴 壓力 ─"
        snr_active  = "⚠️ 無明顯關鍵位"

    pa_label = "✅ 強勢PA" if pa_score >= 0.65 else ("⚠️ 中等PA" if pa_score >= 0.40 else "⛔ 弱PA")
    ema_diff = (price-ema200)/(ema200+1e-10)*100
    ema_tag  = f"{'↑' if price>ema200 else '↓'}{abs(ema_diff):.1f}%"
    rsi_tag  = "超買⚠️" if rsi_val>65 else ("超賣⚠️" if rsi_val<35 else "正常")

    return {
        "side":             side,
        "entry":            entry,
        "entry_source":     entry_source,
        "zone":             zone,
        "sl":               sl,
        "tp1":              tp1,  "tp2":  tp2,  "tp3":  tp3,
        "structure":        structure,
        "leverage":         lev,  "leverage_note": lev_note,
        "trade_type":       trade_type,
        "cvd_label":        cvd_label,
        "st_val":           st_val, "st_label": st_label,
        "snr_display":      snr_display, "snr_active": snr_active, "snr_zone": snr_zone,
        "whale_signal":     whale_signal, "whale_confidence": whale_conf,
        "whale_desc":       whale_desc,
        "whale_zones":      " | ".join([z['desc'] for z in whale_zones[:2]]) or "─",
        "whale_category":   whale_cat,
        "pa_score":         pa_score,
        "pa_label":         pa_label,
        "pa_signals":       " | ".join(pa_sigs) if pa_sigs else "─",
        "setup_score":      setup_score,
        "divergence_type":  div_desc,
        "is_div":           is_div,
        "rsi_val":          rsi_val,
        "ema200":           ema200,
        "ema_tag":          ema_tag,
        "rsi_tag":          rsi_tag,
        "h1_desc":          h1_desc,
        "vol_spike":        vol_spike,
    }


# ─────────────────────────────────────────────
# 主力績效 & 午夜報告
# ─────────────────────────────────────────────

def update_whale_stats(cat: str, result: str):
    f   = "whale_perf_temp.csv"
    row = pd.DataFrame([{"category": cat, "result": result}])
    if os.path.exists(f):
        pd.concat([pd.read_csv(f), row], ignore_index=True).to_csv(f, index=False)
    else:
        row.to_csv(f, index=False)


def generate_midnight_report(opt: dict) -> str:
    f = "whale_perf_temp.csv"
    if not os.path.exists(f): return ""
    df = pd.read_csv(f)
    if df.empty: return ""
    def wr(sub): return len(sub[sub['result']=='TP'])/len(sub)*100 if len(sub)>0 else 0
    awr = wr(df[df['category']=='Aligned'])
    wwr = wr(df[df['category']=='Warning'])
    rwr = wr(df[df['category']=='Reverse'])
    opt.update({'aligned_win_rate':awr/100,'warning_win_rate':wwr/100,
                'reverse_win_rate':rwr/100,'total_samples':len(df)})
    save_optimization_params(opt)
    os.remove(f)
    return (
        f"\n🐋 *主力績效統計 (近 {len(df)} 單)*\n"
        f"   ✅ 主力一致勝率：{awr:.1f}%\n"
        f"   ⚠️ 主力警示勝率：{wwr:.1f}%\n"
        f"   🚫 主力反向勝率：{rwr:.1f}%\n"
        f"   🔄 動態閾值：{get_dynamic_threshold(opt):.2f}"
    )


# ─────────────────────────────────────────────
# 8. 主程式（while True 連續監控 → 修復進場通知丟失）
# ─────────────────────────────────────────────

def run_one_cycle(opt: dict):
    """單次掃描循環（每 SCAN_INTERVAL 秒執行一次）"""
    now_tw      = datetime.utcnow() + timedelta(hours=8)
    current_bar = int(datetime.utcnow().timestamp() // 900)

    # ── 初始化 CSV ──────────────────────────────
    for fp, cols in zip([LOG_FILE, STATS_FILE], [LOG_COLS, STATS_COLS]):
        if not os.path.exists(fp) or os.stat(fp).st_size == 0:
            pd.DataFrame(columns=cols).to_csv(fp, index=False)

    # ── 午夜報告 ────────────────────────────────
    is_midnight = (now_tw.hour == 0 and 0 <= now_tw.minute < 15)
    manual_rpt  = os.getenv("MANUAL_REPORT", "false").lower() == "true"
    if is_midnight or manual_rpt:
        if not os.path.exists("midnight.ok") or manual_rpt:
            try:
                df_s = pd.read_csv(STATS_FILE)
                if not df_s.empty:
                    tp_c  = len(df_s[df_s['result']=='TP'])
                    sl_c  = len(df_s[df_s['result']=='SL'])
                    total = tp_c + sl_c
                    wr    = tp_c/total*100 if total > 0 else 0
                    avg_r = (tp_c*2+sl_c*(-1))/total if total > 0 else 0
                    w_rpt = generate_midnight_report(opt)
                    p_tag = "\n🧪 *紙交易模式*" if PAPER_TRADING else ""
                    send_tg(
                        f"📊 *Alpha Oracle v5.1 | 每日戰績*{p_tag}\n"
                        f"══════════════════════\n"
                        f"📅 {(now_tw-timedelta(days=1)).strftime('%Y-%m-%d')} "
                        f"⏰ {now_tw.strftime('%H:%M')}\n\n"
                        f"✅ 盈利：{tp_c}單  ❌ 止損：{sl_c}單\n"
                        f"📊 總計：{total}單  🎯 勝率：*{wr:.1f}%*\n"
                        f"💰 平均 R：{avg_r:.2f}R\n"
                        f"{w_rpt}\n══════════════════════"
                    )
                if is_midnight:
                    pd.DataFrame(columns=STATS_COLS).to_csv(STATS_FILE, index=False)
                    with open("midnight.ok","w") as fh:
                        fh.write(f"ok_{now_tw.strftime('%Y%m%d')}")
            except Exception as e:
                logging.error(f"午夜報告失敗：{e}")
    elif now_tw.hour != 0 and os.path.exists("midnight.ok"):
        os.remove("midnight.ok")

    # ── 讀取現有持倉 ─────────────────────────────
    try:
        trades_df = pd.read_csv(LOG_FILE)
        for col in ["wait_since","tp1_hit","entry_source","snr_display","snr_active",
                    "whale_signal","whale_confidence","whale_category",
                    "pa_score","pa_signals","setup_score","divergence_type"]:
            if col not in trades_df.columns:
                defs = {
                    "entry_source":"OB","snr_display":"🟢 支撐─|🔴 壓力─",
                    "snr_active":"⚠️ 無明顯關鍵位","whale_signal":"─",
                    "whale_confidence":0.5,"whale_category":"Unknown",
                    "pa_score":0.0,"pa_signals":"─","setup_score":0.0,
                    "divergence_type":"─",
                }
                trades_df[col] = defs.get(col, 0)
    except Exception:
        trades_df = pd.DataFrame(columns=LOG_COLS)

    active_ids     = trades_df['instId'].tolist()
    updated_trades = []

    btc_df    = fetch_okx("BTC-USDT-SWAP")
    btc_trend = get_btc_direction(btc_df)

    for instId in ALL_COINS:
        df = fetch_okx(instId)
        if df is None or df.empty:
            time.sleep(0.3); continue

        curr_p   = df['c'].iloc[-1]
        coin_sym = instId.split('-')[0]
        paper_tag = "🧪 紙交易 | " if PAPER_TRADING else ""

        # ══════════════════════════════════════
        # 1. 尋找新機會
        # ══════════════════════════════════════
        if instId not in active_ids:
            if not is_trending_market(df):
                time.sleep(0.3); continue

            setup = find_smc_setup(df, instId, opt, current_bar)
            if setup:
                side = setup['side']

                # CVD 再確認
                cvd_val, _ = calculate_cvd(df)
                if side == "LONG"  and cvd_val < 0: time.sleep(0.3); continue
                if side == "SHORT" and cvd_val > 0: time.sleep(0.3); continue

                # 資金費率（None-safe）
                fr = fetch_funding_rate(instId)
                if fr is not None:
                    if side == "LONG"  and fr >  0.0005: time.sleep(0.3); continue
                    if side == "SHORT" and fr < -0.0005: time.sleep(0.3); continue

                # BTC 大盤方向
                if instId != "BTC-USDT-SWAP":
                    if side == "LONG"  and btc_trend == "DOWN": time.sleep(0.3); continue
                    if side == "SHORT" and btc_trend == "UP":   time.sleep(0.3); continue

                # 盤口過濾
                ob_ratio, ob_label = fetch_order_book(instId)
                if side == "LONG"  and ob_ratio < 0.9: time.sleep(0.3); continue
                if side == "SHORT" and ob_ratio > 1.1: time.sleep(0.3); continue

                # 設定冷卻
                set_cooldown(instId, current_bar)

                # ── 發送訊號通知 ──────────────────────────
                funding, ls_ratio = get_funding_ls(instId)
                side_emoji = "🟢" if side == "LONG" else "🔴"
                side_zh    = "多單 (LONG)" if side == "LONG" else "空單 (SHORT)"
                src_emoji  = ENTRY_SRC_EMOJI.get(setup['entry_source'], "📍")
                src_text   = ENTRY_SRC_TEXT.get(setup['entry_source'], setup['entry_source'])
                st_emoji   = "📈" if setup['st_val']==1 else ("📉" if setup['st_val']==-1 else "⚪")
                w_emoji    = WHALE_EMOJI.get(setup['whale_signal'], "❓")

                if "反轉" in setup['structure']:   tp_labels = ("1.0R","2.5R","4.0R")
                elif "盤整" in setup['structure']: tp_labels = ("0.8R","1.5R","2.0R")
                else:                              tp_labels = ("1.0R","2.0R","3.0R")

                pa_lines = ""
                for sig in setup['pa_signals'].split(" | ")[:3]:
                    if sig and sig != "─": pa_lines += f"   {sig}\n"
                if not pa_lines: pa_lines = "   ─ 無明顯 PA 訊號\n"

                div_line = f"🔬 背離：{setup['divergence_type']}\n" if setup['is_div'] else ""

                send_tg(
                    f"🔥 *{paper_tag}Alpha Oracle v5.1 訊號* 🔥\n"
                    f"──────────────────\n"
                    f"💎 幣種：#{coin_sym}\n"
                    f"🎯 方向：{side_emoji} {side_zh}\n"
                    f"⏰ 週期：15m + 1H 確認\n"
                    f"📊 多空比 {ls_ratio} | 資費 {funding}\n"
                    f"🧬 CVD：{setup['cvd_label']}\n"
                    f"📚 盤口：{ob_label}\n\n"
                    f"💰 進場位：*{setup['entry']:.4f}* {src_emoji}({src_text} 50%)\n"
                    f"🛑 止損位：{setup['sl']:.4f}  (-1R)\n"
                    f"💰 TP1 ({tp_labels[0]}): {setup['tp1']:.4f}\n"
                    f"💰 TP2 ({tp_labels[1]}): {setup['tp2']:.4f}\n"
                    f"💰 TP3 ({tp_labels[2]}): {setup['tp3']:.4f}\n\n"
                    f"🏗️ 結構：{setup['structure']}\n"
                    f"🛡️ SNR：{setup['snr_display']}\n"
                    f"    {setup['snr_active']}\n\n"
                    f"📡 *技術指標*\n"
                    f"   {st_emoji} Supertrend：{setup['st_label']}\n"
                    f"   📊 RSI-14：{setup['rsi_val']:.1f} ({setup['rsi_tag']})\n"
                    f"   📉 EMA200：{setup['ema200']:.4f} ({setup['ema_tag']})\n"
                    f"   {setup['h1_desc']}\n"
                    f"   📦 量能：{'✅ 放量' if setup['vol_spike'] else '⚠️ 縮量'}\n\n"
                    f"🕯️ *PA 確認 ({setup['pa_label']} {setup['pa_score']*100:.0f}分)*\n"
                    f"{pa_lines}"
                    f"{div_line}"
                    f"🐋 主力：{w_emoji} {setup['whale_signal']} ({setup['whale_confidence']*100:.0f}%)\n"
                    f"    {setup['whale_desc']}\n"
                    f"🎯 主力區：{setup['whale_zones']}\n"
                    f"🕹️ 槓桿：{setup['leverage']} ({setup['leverage_note']})\n"
                    f"📌 類型：{setup['trade_type']}\n"
                    f"📊 綜合評分：*{setup['setup_score']*100:.0f}分*（門檻{SETUP_SCORE_THRESHOLD*100:.0f}分）\n\n"
                    f"💡 *等待回踩 {src_text} 50% 位置成交...*"
                )

                updated_trades.append({
                    "instId":           instId,
                    "side":             side,
                    "status":           "WAITING",
                    "entry":            setup['entry'],
                    "sl":               setup['sl'],
                    "tp1":              setup['tp1'],
                    "tp2":              setup['tp2'],
                    "tp3":              setup['tp3'],
                    "locked":           0,
                    "wait_since":       current_bar,
                    "tp1_hit":          0,
                    "entry_source":     setup['entry_source'],
                    "snr_display":      setup['snr_display'],
                    "snr_active":       setup['snr_active'],
                    "whale_signal":     setup['whale_signal'],
                    "whale_confidence": setup['whale_confidence'],
                    "whale_category":   setup['whale_category'],
                    "pa_score":         setup['pa_score'],
                    "pa_signals":       setup['pa_signals'],
                    "setup_score":      setup['setup_score'],
                    "divergence_type":  setup['divergence_type'],
                })

            time.sleep(0.3)
            continue

        # ══════════════════════════════════════
        # 2. 管理現有持倉
        # ══════════════════════════════════════
        t = normalize_trade(
            trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
        )

        if t['status'] == "WAITING":
            # 🔧 修復：is_hit 必須在 missed_entry 之前判斷
            n_chk            = min(3, len(df))
            live_low, live_high = fetch_live_hl(instId)
            check_low        = min(df['l'].iloc[-n_chk:].min(), live_low)
            check_high       = max(df['h'].iloc[-n_chk:].max(), live_high)

            is_hit = (
                (t['side'] == "LONG"  and check_low  <= t['entry']) or
                (t['side'] == "SHORT" and check_high >= t['entry'])
            )
            already_sl = (
                (t['side'] == "LONG"  and curr_p < t['sl']) or
                (t['side'] == "SHORT" and curr_p > t['sl'])
            )

            # ① 先判斷進場
            if is_hit and already_sl:
                logging.info(f"[{instId}] 觸及進場但穿破止損，放棄")
                time.sleep(0.3); continue

            if is_hit:
                t['status'] = "ACTIVE"
                side_emoji  = "🟢" if t['side'] == "LONG" else "🔴"
                side_zh     = "多單 (LONG)" if t['side'] == "LONG" else "空單 (SHORT)"
                risk        = abs(t['entry']-t['sl'])+1e-10
                risk_pct    = risk/t['entry']*100
                r1 = abs(t['tp1']-t['entry'])/risk
                r2 = abs(t['tp2']-t['entry'])/risk
                r3 = abs(t['tp3']-t['entry'])/risk
                now_str   = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
                src_emoji = ENTRY_SRC_EMOJI.get(t['entry_source'], "📍")
                src_text  = ENTRY_SRC_TEXT.get(t['entry_source'], t['entry_source'])
                w_emoji   = WHALE_EMOJI.get(t['whale_signal'], "❓")
                pos_rec, wr_range, conf_color = get_whale_position_rec(
                    t['whale_signal'], t['whale_confidence']
                )
                extra_warn = "\n⚠️ *主力動向不明，建議謹慎*" if "跳過" in pos_rec or "觀望" in pos_rec else ""
                pa_sum = ""
                if t['pa_signals'] and t['pa_signals'] != "─":
                    pa_sum = f"\n🕯️ PA：{t['pa_signals'].split(' | ')[0]} ({t['pa_score']*100:.0f}分)"
                div_sum = f"\n🔬 背離：{t['divergence_type']}" if t.get('divergence_type','─') not in ("─","") else ""

                send_tg(
                    f"🚀 *{paper_tag}Alpha Oracle v5.1 | 進場成交* 🚀\n"
                    f"──────────────────\n"
                    f"💎 幣種：#{coin_sym}\n"
                    f"🎯 方向：{side_emoji} {side_zh}\n"
                    f"⏰ 時間：{now_str} UTC\n\n"
                    f"💰 *進場價：{t['entry']:.4f}* {src_emoji}({src_text})\n"
                    f"🛑 *止損 SL：{t['sl']:.4f}* (風險 {risk_pct:.2f}%)\n"
                    f"{pa_sum}{div_sum}\n\n"
                    f"🐋 主力分析：\n"
                    f"   {conf_color} 信心 {t['whale_confidence']*100:.0f}% | {t['whale_signal']}\n"
                    f"   📊 預期勝率：{wr_range}\n"
                    f"   💡 建議倉位：{pos_rec}{extra_warn}\n\n"
                    f"🎯 *止盈目標：*\n"
                    f"💰 TP1 (+{r1:.1f}R)：{t['tp1']:.4f}\n"
                    f"💰 TP2 (+{r2:.1f}R)：{t['tp2']:.4f}\n"
                    f"💰 TP3 (+{r3:.1f}R)：{t['tp3']:.4f}\n\n"
                    f"🛡️ {t['snr_display']}\n"
                    f"    {t['snr_active']}\n"
                    f"📊 綜合評分：{t['setup_score']*100:.0f}分\n"
                    f"🔒 移動止損啟用 | 嚴格風控"
                )
                logging.info(f"[{instId}] ✅ 進場成交 {side_zh} @ {t['entry']:.4f}")
                t['wait_since'] = current_bar
                updated_trades.append(t)
                time.sleep(0.3)
                continue

            # ② 未觸發：檢查過期 / 失效
            bars_waited = current_bar - t['wait_since']
            if bars_waited > WAITING_EXPIRY_BARS:
                logging.info(f"[{instId}] 等待逾期 {bars_waited}bars，清除")
                time.sleep(0.3); continue

            if bars_waited > 10:
                pdiff = abs(curr_p-t['entry'])/t['entry']*100
                missed = (
                    (t['side'] == "LONG"  and curr_p > t['entry']*1.02) or
                    (t['side'] == "SHORT" and curr_p < t['entry']*0.98)
                )
                if missed and pdiff > 2.0:
                    dir_txt = "上漲" if t['side'] == "LONG" else "下跌"
                    send_tg(
                        f"⚠️ *Alpha Oracle | 訊號失效*\n"
                        f"──────────────────\n"
                        f"💎 #{coin_sym}｜{'🟢 多單' if t['side']=='LONG' else '🔴 空單'}\n"
                        f"⏰ 等待 {bars_waited}根 K 棒 (~{bars_waited*15//60}h)\n\n"
                        f"📍 進場價：{t['entry']:.4f}\n"
                        f"📍 當前價：{curr_p:.4f}（偏離 {pdiff:.2f}%）\n\n"
                        f"❌ 價格直接{dir_txt}未回踩\n"
                        f"💡 *此單失效，請勿追單*"
                    )
                    time.sleep(0.3); continue

            # ③ 繼續等待
            updated_trades.append(t)

        elif t['status'] == "ACTIVE":
            risk_r = abs(t['entry']-t['sl'])+1e-10

            # TP1
            if t['tp1_hit'] == 0 and (
                (t['side'] == "LONG"  and curr_p >= t['tp1']) or
                (t['side'] == "SHORT" and curr_p <= t['tp1'])
            ):
                t['tp1_hit'] = 1
                t['sl']      = t['entry']
                send_tg(
                    f"🎯 *{paper_tag}Alpha Oracle | TP1 達標・止損移至成本*\n"
                    f"──────────────────\n"
                    f"💎 #{coin_sym} | 當前價 {curr_p:.4f}\n"
                    f"✅ TP1 (+{abs(t['tp1']-t['entry'])/risk_r:.1f}R) {t['tp1']:.4f} 已達\n"
                    f"💰 TP2 (+{abs(t['tp2']-t['entry'])/risk_r:.1f}R) {t['tp2']:.4f}\n"
                    f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R) {t['tp3']:.4f}\n"
                    f"🔒 止損移至成本 {t['entry']:.4f}\n"
                    f"💡 *建議平倉 50% 鎖定 +1R*"
                )

            # TP2
            if t['locked'] == 0 and (
                (t['side'] == "LONG"  and curr_p >= t['tp2']) or
                (t['side'] == "SHORT" and curr_p <= t['tp2'])
            ):
                t['locked'] = 1
                t['sl']     = t['tp1']
                send_tg(
                    f"🔒 *{paper_tag}Alpha Oracle | TP2 達標・鎖利保護*\n"
                    f"──────────────────\n"
                    f"💎 #{coin_sym} | 當前價 {curr_p:.4f}\n"
                    f"✅ TP2 達標，止損移至 TP1 {t['tp1']:.4f}（+1R 保底）\n"
                    f"💰 TP3 (+{abs(t['tp3']-t['entry'])/risk_r:.1f}R) {t['tp3']:.4f}"
                )

            # SL / TP3
            is_sl  = ((t['side']=="LONG"  and curr_p <= t['sl']) or
                      (t['side']=="SHORT" and curr_p >= t['sl']))
            is_tp3 = ((t['side']=="LONG"  and curr_p >= t['tp3']) or
                      (t['side']=="SHORT" and curr_p <= t['tp3']))

            if is_sl or is_tp3:
                is_be     = is_sl and t['locked'] == 1
                res       = "SL" if (is_sl and not is_be) else "TP"
                res_label = ("💰 TP3 達標" if is_tp3
                             else ("🔒 保本出場" if is_be else "❌ 止損離場"))
                send_tg(
                    f"🏁 *{paper_tag}Alpha Oracle | 交易結算* {res_label}\n"
                    f"──────────────────\n"
                    f"💎 #{coin_sym} | 離場 {curr_p:.4f}\n"
                    f"🚫 SL：{t['sl']:.4f}\n"
                    f"💰 TP1/2/3：{t['tp1']:.4f}/{t['tp2']:.4f}/{t['tp3']:.4f}\n"
                    f"📊 結果：{'✅ 盈利' if res=='TP' else '❌ 虧損'}\n"
                    f"🕯️ PA {t['pa_score']*100:.0f}分 | 綜合 {t['setup_score']*100:.0f}分"
                )
                if not PAPER_TRADING:
                    update_whale_stats(t.get('whale_category','Unknown'), res)
                    pd.DataFrame([{
                        "instId":           instId,
                        "result":           res,
                        "whale_signal":     t['whale_signal'],
                        "whale_confidence": t['whale_confidence'],
                        "whale_category":   t.get('whale_category','Unknown'),
                        "pa_score":         t['pa_score'],
                        "setup_score":      t['setup_score'],
                        "divergence_type":  t.get('divergence_type','─'),
                    }]).to_csv(STATS_FILE, mode='a', header=False, index=False)
                logging.info(f"[{instId}] 交易結算：{res} @ {curr_p:.4f}")
                time.sleep(0.3); continue

            updated_trades.append(t)

        time.sleep(0.3)

    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)


def main():
    """🔧 核心修復：while True 連續監控，每 SCAN_INTERVAL 秒掃描一次"""
    logging.info("=" * 60)
    logging.info(f"🚀 Alpha Oracle v5.1 啟動")
    logging.info(f"   掃描間隔：{SCAN_INTERVAL}s")
    logging.info(f"   監控幣種：{len(ALL_COINS)}個")
    logging.info(f"   紙交易：{'開啟' if PAPER_TRADING else '關閉'}")
    logging.info(f"   時段過濾：{'開啟' if SESSION_FILTER_ENABLED else '關閉'}")
    logging.info("=" * 60)

    opt = load_optimization_params()

    # 啟動通知
    send_tg(
        f"🟢 *Alpha Oracle v5.1 已啟動*\n"
        f"──────────────────\n"
        f"📡 監控 {len(ALL_COINS)} 個幣種\n"
        f"🔄 掃描間隔：每 {SCAN_INTERVAL} 秒\n"
        f"⏰ 時段過濾：{'✅ 倫敦/紐約盤' if SESSION_FILTER_ENABLED else '⛔ 關閉'}\n"
        f"📌 模式：{'🧪 紙交易' if PAPER_TRADING else '🔴 實盤'}\n"
        f"🛡️ OB 50%Mean | FVG 1.5ATR | 背離引擎 全啟用"
    )

    while True:
        try:
            heartbeat_check()
            run_one_cycle(opt)
        except KeyboardInterrupt:
            logging.info("使用者中斷，停止監控")
            send_tg("⛔ *Alpha Oracle v5.1 已停止監控*")
            break
        except Exception as e:
            logging.error(f"主循環異常：{e}")
            traceback.print_exc()
            send_tg(f"⚠️ *Alpha Oracle 異常*\n`{str(e)[:200]}`")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
