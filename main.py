#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Oracle Pro v15.2 — 專業精簡版（繁體中文）
══════════════════════════════════════════════════════════════════════
✨ 設計原則：75分門檻不變｜結構化評分｜訊息精簡｜勝率70%+

✅ 評分架構重構（總分100，75分=高品質）：
   🏗️ 結構維度(40)：多時框趨勢20 + 價格結構20
   ⚡ 動能維度(25)：RSI動能15 + 量能確認10
   🎯 進場維度(20)：OB/FVG精度15 + 價格行為5
   🛡️ 風控維度(15)：波動環境10 + 流動性5

✅ 硬閘門過濾（不滿足直接放棄）：
   1. 1H+4H Supertrend 必須同向
   2. 量能 > 20均量 × 1.2
   3. 資金費率非極端值

✅ 精簡訊息：只留方向/評分/進場/SL/TP1/2關鍵理由/風險%

✅ 結構化止損：以前高/前低 + 緩衝，減少無謂掃損
══════════════════════════════════════════════════════════════════════
"""
import os
import sys
import json
import time
import uuid
import logging
import requests
from datetime import datetime, timezone, timedelta


# ═════════════════════════════════════════════════════════
# 1. 基礎設定
# ═════════════════════════════════════════════════════════
TW_TZ = timezone(timedelta(hours=8))


def tw_now() -> datetime:
    return datetime.now(TW_TZ)


def tw_ts() -> str:
    return tw_now().strftime("%Y-%m-%d %H:%M:%S 台灣時間")


def _get_env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val.strip() if val and val.strip() else default


def _get_env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    try:
        return int(val.strip()) if val and val.strip() else default
    except Exception:
        return default


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    stream=sys.stdout,
)

TG_TOKEN = _get_env("TG_TOKEN")
CHAT_ID = _get_env("CHAT_ID")

ALL_COINS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
    "BNB-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP",
    "ADA-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP",
    "DOT-USDT-SWAP", "TON-USDT-SWAP", "NEAR-USDT-SWAP",
]

ACTIVE_SIGNALS_FILE = "active_signals.json"
TRADE_HISTORY_FILE = "trade_history.json"
SIGNAL_COOLDOWN_FILE = "signal_cooldown.json"
SYSTEM_STATE_FILE = "system_state.json"
LEARNING_FILE = "learning_state.json"
CONFIG_FILE = "config.json"

DEFAULT_CONFIG: dict = {
    "coins": ALL_COINS,
    "max_signals_per_scan": 2,
    "score_threshold": 75,
    "cooldown_hours": 3,
    "signal_expire_hours": 24,
    "atr_max_pct": 0.04,
    "min_rr_ratio": 1.5,
    "show_score_breakdown": False,

    "capital_management": {
        "capital_per_trade_usd": 100,
        "max_loss_usd": 20,
        "max_leverage": 50,
        "min_leverage": 2,
    },

    "daily_limits": {
        "max_concurrent_positions": 2,
        "daily_loss_limit_pct": 5.0,
    },

    "circuit_breaker": {
        "loss_threshold": 3,
        "pause_hours": 24,
    },

    "price_verification": {
        "enabled": True,
        "max_deviation_pct": 0.5,
        "block_on_unverified": False,
    },

    "learning": {
        "enabled": True,
        "knn_enabled": True,
        "min_samples": 5,
        "max_score_adjust": 10,
    },

    "post_mortem": {
        "enabled": True,
        "loss_only": False,
    },

    "blackout_windows_tw": [
        {"start": "07:50", "end": "08:10", "reason": "資金費率結算（00 UTC）"},
        {"start": "15:50", "end": "16:10", "reason": "資金費率結算（08 UTC）"},
        {"start": "23:50", "end": "00:10", "reason": "資金費率結算（16 UTC）"},
        {"start": "21:25", "end": "21:45", "reason": "美股開盤波動"},
        {"start": "02:00", "end": "02:30", "reason": "FOMC 公布時段"},
    ],
    "auto_news_blackout": {
        "nfp": True,
        "cpi": True,
    },
    "news_blackouts": [],
}

_price_cache: dict = {}
_candle_cache: dict = {}
_mtf_cache: dict = {}
_tv_cache: dict = {}


# ═════════════════════════════════════════════════════════
# 2. 持久化 + 配置
# ═════════════════════════════════════════════════════════
def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"⚠️ 讀取 {path} 失敗：{e}")
    return default


def _save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logging.error(f"❌ 寫入 {path} 失敗：{e}")


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    user_cfg = _load_json(CONFIG_FILE, {})
    return _deep_merge(DEFAULT_CONFIG, user_cfg) if user_cfg else dict(DEFAULT_CONFIG)


def get_system_state() -> dict:
    return _load_json(SYSTEM_STATE_FILE, {})


def set_system_state(state: dict) -> None:
    _save_json(SYSTEM_STATE_FILE, state)


# ═════════════════════════════════════════════════════════
# 3. Telegram 通知（含重試）
# ═════════════════════════════════════════════════════════
def send_tg(msg: str, parse_mode: str = "Markdown",
            reply_markup: dict | None = None,
            reply_to_message_id: int | None = None,
            max_retries: int = 3) -> int | None:
    """📤 送 TG，429/5xx 自動重試"""
    if not TG_TOKEN or not CHAT_ID:
        return None
    payload = {
        "chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True

    for attempt in range(max_retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json=payload, timeout=8,
            )
            if r.status_code == 200:
                try:
                    s = _load_json(SYSTEM_STATE_FILE, {})
                    s["last_tg_sent"] = time.time()
                    _save_json(SYSTEM_STATE_FILE, s)
                except Exception:
                    pass
                return r.json().get("result", {}).get("message_id")
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait + 0.5, 15))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            logging.error(f"❌ TG {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            logging.warning(f"⏳ TG 失敗 {2 ** attempt}s 後重試：{e}")
            time.sleep(2 ** attempt)
    return None


def _order_keyboard(order_id: str) -> dict:
    return {"inline_keyboard": [[{
        "text": f"🔍 查詢訂單 {order_id[-8:]}",
        "callback_data": f"order_{order_id}",
    }]]}


# ═════════════════════════════════════════════════════════
# 4. 數據抓取
# ═════════════════════════════════════════════════════════
def fetch_price(instId: str) -> float:
    now = time.time()
    if instId in _price_cache:
        price, t = _price_cache[instId]
        if now - t < 5:
            return price
    try:
        r = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={instId}",
            timeout=5,
        ).json()
        if r.get("code") == "0" and r.get("data"):
            price = float(r["data"][0]["last"])
            if price > 0:
                _price_cache[instId] = (price, now)
                return price
    except Exception as e:
        logging.warning(f"⚠️ {instId} 價格失敗：{e}")
    return _price_cache.get(instId, (0.0, 0))[0]


def fetch_candles(instId: str, tf: str = "15m", limit: int = 100,
                  include_unconfirmed: bool = True) -> list:
    cache_key = f"{instId}_{tf}_{limit}_{include_unconfirmed}"
    now = time.time()
    if cache_key in _candle_cache:
        candles, t = _candle_cache[cache_key]
        if now - t < 30:
            return candles
    try:
        r = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={tf}&limit={limit}",
            timeout=6,
        ).json()
        if r.get("code") != "0":
            return _candle_cache.get(cache_key, ([], 0))[0]
        candles = []
        for row in r.get("data", []):
            confirmed = row[8] == "1"
            if not include_unconfirmed and not confirmed:
                continue
            candles.append({
                "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
                "confirmed": confirmed,
            })
        candles.sort(key=lambda x: x["ts"])
        _candle_cache[cache_key] = (candles, now)
        return candles
    except Exception as e:
        logging.warning(f"⚠️ {instId} K 線失敗：{e}")
        return _candle_cache.get(cache_key, ([], 0))[0]


def fetch_funding_rate(instId: str) -> float | None:
    try:
        r = requests.get(
            f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}",
            timeout=5,
        ).json()
        if r.get("code") == "0" and r.get("data"):
            return float(r["data"][0]["fundingRate"])
    except Exception:
        pass
    return None


def fetch_mtf_trend(instId: str) -> dict:
    """🕒 1H + 4H 趨勢（30 秒快取）"""
    now = time.time()
    if instId in _mtf_cache:
        data, t = _mtf_cache[instId]
        if now - t < 30:
            return data
    out = {}
    for tf in ("1H", "4H"):
        df = fetch_candles(instId, tf=tf, limit=50, include_unconfirmed=False)
        if df:
            st = calc_supertrend(df)
            out[tf] = {"supertrend": st, "rsi": round(calc_rsi(df), 1)}
        else:
            out[tf] = {"supertrend": 0, "rsi": 50}
    _mtf_cache[instId] = (out, now)
    return out


def fetch_price_tv(instId: str) -> float | None:
    """📡 TradingView 第二來源（10 秒快取）"""
    now = time.time()
    if instId in _tv_cache:
        price, t = _tv_cache[instId]
        if now - t < 10:
            return price
    try:
        from tradingview_ta import TA_Handler, Interval
    except ImportError:
        return None
    try:
        symbol = instId.replace("-USDT-SWAP", "USDT.P").replace("-", "")
        h = TA_Handler(
            symbol=symbol, exchange="OKX", screener="crypto",
            interval=Interval.INTERVAL_1_MINUTE, timeout=8,
        )
        a = h.get_analysis()
        price = float(a.indicators.get("close", 0) or 0)
        if price > 0:
            _tv_cache[instId] = (price, now)
            return price
    except Exception:
        pass
    return None


def verify_price(instId: str, okx_price: float, max_dev_pct: float = 0.5,
                 block_on_unverified: bool = False) -> tuple[bool, float | None, float]:
    """⚖️ OKX vs TradingView 雙來源價格驗證"""
    tv_price = fetch_price_tv(instId)
    if tv_price is None:
        return (not block_on_unverified, None, 0.0)
    diff = abs(okx_price - tv_price) / okx_price * 100
    if diff > max_dev_pct:
        return False, tv_price, diff
    return True, tv_price, diff


# ═════════════════════════════════════════════════════════
# 5. 基礎技術指標
# ═════════════════════════════════════════════════════════
def calc_atr(df: list, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.001
    trs = []
    for i in range(1, len(df)):
        hl = df[i]["h"] - df[i]["l"]
        hc = abs(df[i]["h"] - df[i - 1]["c"])
        lc = abs(df[i]["l"] - df[i - 1]["c"])
        trs.append(max(hl, hc, lc))
    atr = sum(trs[-period:]) / period if len(trs) >= period else 0.001
    return atr if atr > 0 else 0.001


def calc_supertrend(df: list, period: int = 10) -> int:
    if len(df) < period + 2:
        return 0
    atr = calc_atr(df, period)
    mid = sum(c["c"] for c in df[-20:]) / 20
    cur = df[-1]["c"]
    band = atr * 0.5
    if cur > mid + band:
        return 1
    if cur < mid - band:
        return -1
    return 0


def calc_rsi(df: list, period: int = 14) -> float:
    if len(df) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(df)):
        ch = df[i]["c"] - df[i - 1]["c"]
        gains.append(ch if ch > 0 else 0)
        losses.append(-ch if ch < 0 else 0)
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


# ═════════════════════════════════════════════════════════
# 6. SMC / ICT / SNR / PA / 流動性 / 動能
# ═════════════════════════════════════════════════════════
def find_order_block(df: list, side: str, lookback: int = 30) -> dict | None:
    """🧱 訂單塊（OB）"""
    n = len(df)
    if n < lookback + 5:
        return None
    start = max(0, n - lookback)
    if side == "LONG":
        for i in range(n - 4, start, -1):
            if df[i]["c"] < df[i]["o"]:
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] > df[i]["h"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    else:
        for i in range(n - 4, start, -1):
            if df[i]["c"] > df[i]["o"]:
                for j in range(i + 1, min(i + 4, n)):
                    if df[j]["c"] < df[i]["l"]:
                        return {"low": df[i]["l"], "high": df[i]["h"]}
    return None


def find_fvg(df: list, side: str, lookback: int = 30) -> dict | None:
    """⚡ 公允價值缺口（FVG）"""
    n = len(df)
    if n < 4:
        return None
    start = max(2, n - lookback)
    for i in range(n - 1, start, -1):
        if side == "LONG":
            if df[i]["l"] > df[i - 2]["h"]:
                return {"low": df[i - 2]["h"], "high": df[i]["l"]}
        else:
            if df[i]["h"] < df[i - 2]["l"]:
                return {"low": df[i]["h"], "high": df[i - 2]["l"]}
    return None


def calc_snr(df: list, lookback: int = 100) -> tuple[float, float]:
    """📏 動態支撐 / 阻力（近 N 根極值）"""
    seg = df[-lookback:] if len(df) >= lookback else df
    return min(c["l"] for c in seg), max(c["h"] for c in seg)


def detect_price_action(df: list, side: str) -> bool:
    """📊 Pin Bar 或吞沒形態"""
    if len(df) < 2:
        return False
    last, prev = df[-1], df[-2]
    body = abs(last["c"] - last["o"])
    upper = last["h"] - max(last["c"], last["o"])
    lower = min(last["c"], last["o"]) - last["l"]
    if body > 0:
        if side == "LONG" and lower > body * 2 and lower > upper:
            return True
        if side == "SHORT" and upper > body * 2 and upper > lower:
            return True
    if side == "LONG":
        if (prev["c"] < prev["o"] and last["c"] > last["o"]
                and last["c"] > prev["o"] and last["o"] < prev["c"]):
            return True
    else:
        if (prev["c"] > prev["o"] and last["c"] < last["o"]
                and last["c"] < prev["o"] and last["o"] > prev["c"]):
            return True
    return False


def detect_liquidity_sweep(df: list, side: str, lookback: int = 20) -> bool:
    """💧 流動性掃蕩"""
    if len(df) < lookback + 1:
        return False
    seg = df[-(lookback + 1):-1]
    last = df[-1]
    pl = min(c["l"] for c in seg)
    ph = max(c["h"] for c in seg)
    mid = (pl + ph) / 2
    if side == "LONG":
        return last["l"] < pl and last["c"] > mid
    return last["h"] > ph and last["c"] < mid


def calc_momentum_ratio(df: list, side: str, n: int = 5) -> bool:
    """📈 盤口動能：最近 N 根 K 多空比例"""
    seg = df[-n:]
    bull = sum(1 for c in seg if c["c"] > c["o"])
    ratio = bull / max(1, len(seg))
    return ratio >= 0.6 if side == "LONG" else ratio <= 0.4


# ═════════════════════════════════════════════════════════
# 7. EMA + 量能 + 結構確認（新增）
# ═════════════════════════════════════════════════════════
def calc_ema(df: list, period: int) -> float:
    if len(df) < period:
        return df[-1]["c"] if df else 0.0
    closes = [c["c"] for c in df]
    m = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * m + ema * (1 - m)
    return ema


def calc_ema_alignment(df: list, side: str) -> tuple[int, str]:
    """🪜 EMA 多週期排列"""
    if len(df) < 200 + 5:
        return 0, ""
    e20, e50, e200 = calc_ema(df, 20), calc_ema(df, 50), calc_ema(df, 200)
    p = df[-1]["c"]
    if side == "LONG":
        if p > e20 > e50 > e200:
            return 5, "多頭完美排列"
        if p > e20 > e50:
            return 3, "短中期多頭"
        if p < e200:
            return -5, "在 EMA200 之下"
    else:
        if p < e20 < e50 < e200:
            return 5, "空頭完美排列"
        if p < e20 < e50:
            return 3, "短中期空頭"
        if p > e200:
            return -5, "在 EMA200 之上"
    return 0, ""


def calc_volume_quality(df: list, lookback: int = 20) -> tuple[float, int]:
    """📊 量能品質 → (倍數, 評分 -10 ~ +8)"""
    if len(df) < lookback + 1:
        return 1.0, 0
    seg = df[-(lookback + 1):-1]
    avg = sum(c["v"] for c in seg) / lookback
    if avg <= 0:
        return 1.0, 0
    ratio = df[-1]["v"] / avg
    if ratio >= 2.0:
        s = 8
    elif ratio >= 1.5:
        s = 5
    elif ratio >= 1.0:
        s = 2
    elif ratio >= 0.5:
        s = 0
    else:
        s = -10
    return round(ratio, 2), s


def calc_mtf_alignment(mtf: dict, side: str) -> tuple[int, str]:
    """🎯 多時框共振 → (-15 ~ +15, 描述)"""
    expect = 1 if side == "LONG" else -1
    h1 = mtf.get("1H", {}).get("supertrend", 0)
    h4 = mtf.get("4H", {}).get("supertrend", 0)
    s = 0
    if h1 == expect:
        s += 8
    elif h1 == -expect:
        s -= 5
    if h4 == expect:
        s += 7
    elif h4 == -expect:
        s -= 5
    s = max(-15, min(15, s))
    desc = f"1H={'順' if h1 == expect else '反' if h1 == -expect else '中'} / 4H={'順' if h4 == expect else '反' if h4 == -expect else '中'}"
    return s, desc


# ═════════════════════════════════════════════════════════
# 🔹 新增：價格結構確認（HH/HL 或 LH/LL）
# ═════════════════════════════════════════════════════════
def calc_price_structure(df: list, side: str, lookback: int = 50) -> int:
    """確認價格結構：多頭看 HH/HL，空頭看 LH/LL"""
    if len(df) < lookback + 5:
        return 0
    
    if side == "LONG":
        lows = [(i, df[i]["l"]) for i in range(len(df)-lookback, len(df)-2)
                if df[i]["l"] < df[i-1]["l"] and df[i]["l"] < df[i+1]["l"]]
        if len(lows) >= 2:
            if lows[-1][1] > lows[-2][1] * 1.002:
                return 20
            elif lows[-1][1] > lows[-2][1] * 0.998:
                return 12
        return 0
    else:
        highs = [(i, df[i]["h"]) for i in range(len(df)-lookback, len(df)-2)
                 if df[i]["h"] > df[i-1]["h"] and df[i]["h"] > df[i+1]["h"]]
        if len(highs) >= 2:
            if highs[-1][1] < highs[-2][1] * 0.998:
                return 20
            elif highs[-1][1] < highs[-2][1] * 1.002:
                return 12
        return 0


# ═════════════════════════════════════════════════════════
# 🔹 新增：OB/FVG 回測精度計算
# ═════════════════════════════════════════════════════════
def calc_ob_fvg_precision(df: list, side: str, current_price: float) -> int:
    """OB/FVG 回測越精準，分數越高（0/8/15分）"""
    ob = find_order_block(df, side)
    fvg = find_fvg(df, side)
    
    if ob and fvg:
        ob_dist = min(abs(current_price - ob["low"]), abs(current_price - ob["high"])) / current_price
        fvg_dist = min(abs(current_price - fvg["low"]), abs(current_price - fvg["high"])) / current_price
        min_dist = min(ob_dist, fvg_dist)
    elif ob:
        min_dist = min(abs(current_price - ob["low"]), abs(current_price - ob["high"])) / current_price
    elif fvg:
        min_dist = min(abs(current_price - fvg["low"]), abs(current_price - fvg["high"])) / current_price
    else:
        return 0
    
    if min_dist <= 0.003:
        return 15
    elif min_dist <= 0.008:
        return 8
    elif min_dist <= 0.015:
        return 4
    return 0


# ═════════════════════════════════════════════════════════
# 🔹 新增：硬閘門過濾（專業交易員必過關卡）
# ═════════════════════════════════════════════════════════
def check_mandatory_conditions(df: list, side: str, mtf: dict, funding_rate: float | None) -> bool:
    """專業交易員硬閘門：不滿足直接放棄"""
    # 1. 多時框共振：1H+4H Supertrend 必須同向
    h1 = mtf.get("1H", {}).get("supertrend", 0)
    h4 = mtf.get("4H", {}).get("supertrend", 0)
    expected = 1 if side == "LONG" else -1
    if not (h1 == expected and h4 == expected):
        return False
    
    # 2. 量能確認：當前量能 > 20均量 × 1.2
    vol_ratio, _ = calc_volume_quality(df)
    if vol_ratio < 1.2:
        return False
    
    # 3. 資金費率極端值過濾
    if funding_rate is not None:
        if side == "LONG" and funding_rate > 0.001:
            return False
        if side == "SHORT" and funding_rate < -0.001:
            return False
    
    return True


# ═════════════════════════════════════════════════════════
# 🔹 新增：75分品質驗證（確保均衡高分）
# ═════════════════════════════════════════════════════════
def validate_75_quality(score: int, detail: dict) -> bool:
    """確保75分是「均衡高分」而非「單一維度湊分」"""
    if score < 75:
        return False
    
    struct = detail.get("multi_tf_trend", 0) + detail.get("price_structure", 0)
    momentum = detail.get("rsi_momentum", 0) + detail.get("volume_confirmation", 0)
    entry = detail.get("ob_fvg_precision", 0) + detail.get("price_action", 0)
    
    passed = sum([struct >= 25, momentum >= 12, entry >= 12])
    return passed >= 2


# ═════════════════════════════════════════════════════════
# 🔹 新增：高品質訊號額外加分
# ═════════════════════════════════════════════════════════
def apply_bonus_points(score: int, detail: dict) -> int:
    """高品質訊號額外加分（上限+10分）"""
    bonus = 0
    if (detail.get("multi_tf_trend") == 20 and 
        detail.get("price_structure") == 20 and
        detail.get("volume_confirmation") >= 5):
        bonus += 3
    if detail.get("ob_fvg_precision") == 15 and detail.get("price_action") == 5:
        bonus += 3
    if detail.get("vol_ratio", 0) >= 2.0 and detail.get("rsi_momentum") == 15:
        bonus += 2
    if detail.get("volatility_context") == 10 and detail.get("price_structure") == 20:
        bonus += 2
    return min(score + bonus, 100)


# ═════════════════════════════════════════════════════════
# 🔹 新增：結構化止損（75分訊號專用）
# ═════════════════════════════════════════════════════════
def calc_sl_for_75_signal(df: list, side: str, entry: float, detail: dict, atr: float) -> float:
    """75分以上訊號專用止損：結構優先，ATR 輔助"""
    if side == "LONG" and detail.get("price_structure", 0) >= 12:
        lows = [(i, df[i]["l"]) for i in range(max(0, len(df)-30), len(df)-2)
                if df[i]["l"] < df[i-1]["l"] and df[i]["l"] < df[i+1]["l"]]
        if lows:
            swing_low = min(lows, key=lambda x: x[0])[1]
            return swing_low * 0.997
    elif side == "SHORT" and detail.get("price_structure", 0) >= 12:
        highs = [(i, df[i]["h"]) for i in range(max(0, len(df)-30), len(df)-2)
                 if df[i]["h"] > df[i-1]["h"] and df[i]["h"] > df[i+1]["h"]]
        if highs:
            swing_high = max(highs, key=lambda x: x[0])[1]
            return swing_high * 1.003
    return entry - atr * 1.2 if side == "LONG" else entry + atr * 1.2


# ═════════════════════════════════════════════════════════
# 8. 評分系統（重構版：總分100，75分=高品質）
# ═════════════════════════════════════════════════════════
def calc_score(df: list, side: str, current_price: float,
               mtf: dict | None = None, instId: str | None = None) -> tuple[int, str, dict]:
    """總分100，75分為高品質門檻｜四大維度結構化評分"""
    detail = {}
    score = 0
    
    # 🏗️ 結構維度（40分）
    # 1. 多時框趨勢共振（20分）
    if mtf is None and instId:
        mtf = fetch_mtf_trend(instId)
    h1 = mtf.get("1H", {}).get("supertrend", 0) if mtf else 0
    h4 = mtf.get("4H", {}).get("supertrend", 0) if mtf else 0
    expected = 1 if side == "LONG" else -1
    
    if h1 == expected and h4 == expected:
        score += 20; detail["multi_tf_trend"] = 20
    elif h1 == expected or h4 == expected:
        score += 12; detail["multi_tf_trend"] = 12
    else:
        detail["multi_tf_trend"] = 0
    
    # 2. 價格結構確認（20分）
    structure_score = calc_price_structure(df, side)
    score += structure_score
    detail["price_structure"] = structure_score
    
    # ⚡ 動能維度（25分）
    # 3. RSI 動能（15分）- 避免極端值
    rsi = calc_rsi(df)
    detail["rsi_value"] = round(rsi, 1)
    if 40 <= rsi <= 60:
        score += 15; detail["rsi_momentum"] = 15
    elif (30 <= rsi < 40) or (60 < rsi <= 70):
        score += 8; detail["rsi_momentum"] = 8
    else:
        detail["rsi_momentum"] = 0
    
    # 4. 量能確認（10分）
    vol_ratio, vol_score = calc_volume_quality(df)
    vol_final = 10 if vol_score >= 5 else (5 if vol_score >= 2 else 0)
    score += vol_final
    detail["volume_confirmation"] = vol_final
    detail["vol_ratio"] = vol_ratio
    
    # 🎯 進場維度（20分）
    # 5. OB/FVG 回測精度（15分）
    ob_precision = calc_ob_fvg_precision(df, side, current_price)
    score += ob_precision
    detail["ob_fvg_precision"] = ob_precision
    
    # 6. 價格行為確認（5分）
    pa_score = 5 if detect_price_action(df, side) else 0
    score += pa_score
    detail["price_action"] = pa_score
    
    # 🛡️ 風控維度（15分）
    # 7. 波動率環境（10分）
    atr_pct = calc_atr(df) / current_price * 100
    if 0.5 <= atr_pct <= 3.0:
        score += 10; detail["volatility_context"] = 10
    elif (0.3 <= atr_pct < 0.5) or (3.0 < atr_pct <= 5.0):
        score += 5; detail["volatility_context"] = 5
    else:
        detail["volatility_context"] = 0
    detail["atr_pct"] = round(atr_pct, 3)
    
    # 8. 流動性風險評估（5分）
    liq_score = 5 if not detect_liquidity_sweep(df, "SHORT" if side=="LONG" else "LONG") else 0
    score += liq_score
    detail["liquidity_flow"] = liq_score
    
    # 🎯 等級評定
    grade = (
        "🔥 A+ 極強" if score >= 90
        else "⭐ A 強力" if score >= 82
        else "✅ B+ 合格" if score >= 75
        else "⚪ 觀望"
    )
    
    return score, grade, detail


# ═════════════════════════════════════════════════════════
# 9. 學習機制（保持原邏輯，兼容新評分結構）
# ═════════════════════════════════════════════════════════
_FEATURE_SCALE = {
    "score": 30, "rsi": 50, "atr_pct": 3, "funding": 2,
    "vol_ratio": 3, "mtf_h1": 1, "mtf_h4": 1, "side": 1,
}


def vectorize_signal(score: int, side: str, detail: dict, funding_rate,
                     mtf: dict | None = None) -> dict:
    rsi = (detail or {}).get("rsi_value", 50)
    return {
        "score": float(score),
        "rsi": float(rsi),
        "atr_pct": float((detail or {}).get("atr_pct", 1.0)),
        "funding": float(funding_rate or 0) * 1000,
        "vol_ratio": float((detail or {}).get("vol_ratio", 1.0)),
        "mtf_h1": 1.0 if (mtf or {}).get("1H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "mtf_h4": 1.0 if (mtf or {}).get("4H", {}).get("supertrend") == (1 if side == "LONG" else -1) else 0.0,
        "side": 1.0 if side == "LONG" else 0.0,
    }


def find_similar_trades(features: dict, history: list, k: int = 10) -> list:
    candidates = []
    for t in history:
        f = t.get("features")
        if not f:
            continue
        d2 = sum(
            ((features.get(key, 0) - f.get(key, 0)) / max(scale, 1)) ** 2
            for key, scale in _FEATURE_SCALE.items()
        )
        candidates.append((d2, t))
    candidates.sort(key=lambda x: x[0])
    return [t for _, t in candidates[:k]]


def apply_knn_learning(score: int, side: str, detail: dict, funding_rate,
                       mtf: dict | None) -> tuple[int, list]:
    """🧬 KNN：找最相似 10 筆歷史交易看勝率"""
    cfg = load_config()
    if not cfg.get("learning", {}).get("knn_enabled", True):
        return score, []
    history = _load_json(TRADE_HISTORY_FILE, [])
    if len(history) < 10:
        return score, []
    feat = vectorize_signal(score, side, detail, funding_rate, mtf)
    similar = find_similar_trades(feat, history, k=10)
    if len(similar) < 3:
        return score, []
    wins = sum(1 for t in similar if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK"))
    n = len(similar)
    wr = wins / n
    notes = [f"🧬 KNN：{n} 筆最相似 → 勝 {wins} (勝率 {wr:.0%})"]
    if wr < 0.30:
        return score - 8, notes + ["低勝率 -8"]
    if wr < 0.40:
        return score - 4, notes + ["偏低 -4"]
    if wr > 0.70:
        return score + 5, notes + ["高勝率 +5"]
    if wr > 0.60:
        return score + 3, notes + ["中高 +3"]
    return score, notes


def _bucket_score(s: int) -> str:
    if s >= 90:
        return "score:90+"
    if s >= 80:
        return "score:80-89"
    if s >= 70:
        return "score:70-79"
    return "score:60-69"


def _bucket_rsi(rsi: float, side: str) -> str:
    b = int(rsi // 10) * 10
    return f"rsi_{side.lower()}:{b}-{b + 9}"


def _bucket_funding(fr) -> str:
    if fr is None:
        return "fund:none"
    if fr > 0.0008:
        return "fund:very_pos"
    if fr > 0.0001:
        return "fund:pos"
    if fr > -0.0001:
        return "fund:neutral"
    if fr > -0.0008:
        return "fund:neg"
    return "fund:very_neg"


def _signal_buckets(score: int, side: str, detail: dict, funding_rate, coin: str) -> list:
    rsi = (detail or {}).get("rsi_value", 50)
    return [
        _bucket_score(score),
        _bucket_rsi(rsi, side),
        _bucket_funding(funding_rate),
        f"coin:{coin}",
        f"coin_side:{coin}_{side}",
    ]


def update_learning(trade: dict, sig_snapshot: dict | None = None) -> None:
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("buckets", {})
    state.setdefault("by_coin", {})
    state.setdefault("loss_reasons", [])

    score = trade.get("score", 0)
    coin = trade.get("coin", "?")
    side = trade.get("side", "?")
    ct = trade.get("close_type", "?")
    fr = trade.get("funding_rate")
    detail = trade.get("detail") or (sig_snapshot or {}).get("detail", {})

    is_win = ct in ("TP1", "TP2", "TP3", "LOCK")
    is_be = ct == "BE"
    is_loss = ct == "SL"

    for b in _signal_buckets(score, side, detail, fr, coin):
        bd = state["buckets"].setdefault(b, {"win": 0, "loss": 0, "be": 0, "total": 0})
        bd["total"] += 1
        if is_win:
            bd["win"] += 1
        elif is_loss:
            bd["loss"] += 1
        elif is_be:
            bd["be"] += 1

    cd = state["by_coin"].setdefault(coin, {"win": 0, "loss": 0, "be": 0, "total": 0})
    cd["total"] += 1
    if is_win:
        cd["win"] += 1
    elif is_loss:
        cd["loss"] += 1
    elif is_be:
        cd["be"] += 1

    state["updated_at"] = time.time()
    _save_json(LEARNING_FILE, state)


def apply_learning_adjustment(score: int, side: str, detail: dict,
                              funding_rate, coin: str) -> tuple[int, list]:
    cfg = load_config()
    lcfg = cfg.get("learning", {})
    if not lcfg.get("enabled", True):
        return score, []
    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    min_n = lcfg.get("min_samples", 5)
    max_adj = lcfg.get("max_score_adjust", 10)

    notes, total = [], 0
    for b in _signal_buckets(score, side, detail, funding_rate, coin):
        bd = buckets.get(b)
        if not bd or bd.get("total", 0) < min_n:
            continue
        wr = bd["win"] / bd["total"]
        if wr < 0.30:
            d = -3
        elif wr < 0.40:
            d = -2
        elif wr > 0.70:
            d = 2
        elif wr > 0.60:
            d = 1
        else:
            continue
        total += d
        notes.append(f"{b} (n={bd['total']}, 勝率 {wr:.0%}) → {d:+d}")
    total = max(-max_adj, min(max_adj, total))
    return score + total, notes


def record_loss_reason(coin: str, side: str, reasons: list) -> None:
    state = _load_json(LEARNING_FILE, {})
    state.setdefault("loss_reasons", [])
    for r in reasons[:1]:
        state["loss_reasons"].append({
            "ts": time.time(), "coin": coin, "side": side,
            "code": r.get("code"), "title": r.get("title"),
        })
    state["loss_reasons"] = state["loss_reasons"][-100:]
    _save_json(LEARNING_FILE, state)


# ═════════════════════════════════════════════════════════
# 10. 訊號生成（整合硬閘門 + 結構化止損）
# ═════════════════════════════════════════════════════════
def generate_signal(instId: str, df: list, current_price: float,
                    funding_rate: float | None = None,
                    score_threshold: int = 75,
                    atr_max_pct: float = 0.04,
                    signal_expire_hours: int = 24) -> dict | None:
    if df is None or len(df) < 50:
        return None
    atr = calc_atr(df)
    atr_pct = atr / current_price * 100
    if atr / current_price > atr_max_pct:
        return None

    coin = instId.split("-")[0]
    mtf = fetch_mtf_trend(instId)

    candidates = []
    for side in ("LONG", "SHORT"):
        # 🔹 硬閘門過濾
        if not check_mandatory_conditions(df, side, mtf, funding_rate):
            continue
        
        score, grade, detail = calc_score(df, side, current_price, mtf=mtf)
        detail["atr_pct"] = round(atr_pct, 3)

        # 學習雙路：桶統計 + KNN
        adj_score, knn_notes = apply_knn_learning(score, side, detail, funding_rate, mtf)
        adj_score, bucket_notes = apply_learning_adjustment(
            adj_score, side, detail, funding_rate, coin
        )
        if knn_notes or bucket_notes:
            detail["learning_notes"] = knn_notes + bucket_notes
            detail["learning_adjust"] = adj_score - score
        score = adj_score

        # 🔹 75分品質驗證（確保均衡高分）
        if not validate_75_quality(score, detail):
            continue

        # 🔹 高品質額外加分
        score = apply_bonus_points(score, detail)
        if score < score_threshold:
            continue

        entry = current_price
        
        # 🔹 結構化止損（75分訊號專用）
        sl = calc_sl_for_75_signal(df, side, entry, detail, atr)
        risk = abs(entry - sl)

        # 固定 1.5R / 3R / 5R
        if side == "LONG":
            tp1 = entry + risk * 1.5
            tp2 = entry + risk * 3.0
            tp3 = entry + risk * 5.0
        else:
            tp1 = entry - risk * 1.5
            tp2 = entry - risk * 3.0
            tp3 = entry - risk * 5.0

        # R:R 最低門檻
        cfg = load_config()
        min_rr = cfg.get("min_rr_ratio", 1.5)
        actual_tp1_r = abs(tp1 - entry) / max(risk, 1e-9)
        if actual_tp1_r < min_rr - 0.02:
            continue

        ob_zone = find_order_block(df, side)
        candidates.append({
            "instId": instId,
            "side": side,
            "tf": "15m",
            "entry": round(entry, 4),
            "sl": round(sl, 4),
            "tp1": round(tp1, 4),
            "tp2": round(tp2, 4),
            "tp3": round(tp3, 4),
            "score": score,
            "grade": grade,
            "detail": detail,
            "funding_rate": funding_rate,
            "mtf_snapshot": mtf,
            "ob_zone": ob_zone,
            "created": time.time(),
            "expires": time.time() + signal_expire_hours * 3600,
        })

    return max(candidates, key=lambda x: x["score"]) if candidates else None


# ═════════════════════════════════════════════════════════
# 11. 資金管理
# ═════════════════════════════════════════════════════════
def calc_position_sizing(entry: float, sl: float, tp1: float, tp2: float,
                         tp3: float, side: str, cfg: dict | None = None) -> dict | None:
    if cfg is None:
        cfg = load_config()
    cm = cfg.get("capital_management", {})
    capital = cm.get("capital_per_trade_usd", 100)
    max_loss = cm.get("max_loss_usd", 20)
    max_lev = cm.get("max_leverage", 50)
    min_lev = cm.get("min_leverage", 2)

    sl_dist = abs(entry - sl) / entry
    if sl_dist <= 0:
        return None
    leverage = max(min_lev, min(max_lev, round((max_loss / capital) / sl_dist)))
    pos_value = capital * leverage
    contracts = pos_value / entry

    def _pnl(t):
        if side == "LONG":
            return pos_value * (t - entry) / entry
        return pos_value * (entry - t) / entry

    return {
        "capital": capital, "max_loss": max_loss, "leverage": int(leverage),
        "position_value": round(pos_value, 2), "contracts": round(contracts, 4),
        "sl_loss": round(abs(_pnl(sl)), 2),
        "tp1_profit": round(_pnl(tp1), 2),
        "tp2_profit": round(_pnl(tp2), 2),
        "tp3_profit": round(_pnl(tp3), 2),
    }


# ═════════════════════════════════════════════════════════
# 12. 通知格式（🔹 精簡版：只留重點）
# ═════════════════════════════════════════════════════════
def _fmt_entry(sig: dict, current_price: float) -> str:
    """🔹 專業精簡版進場通知：只留決策關鍵資訊"""
    coin = sig["instId"].split("-")[0]
    side = "做多" if sig["side"] == "LONG" else "做空"
    emoji = "🟢" if sig["side"] == "LONG" else "🔴"
    
    # 🔹 只取前2個最強理由
    detail = sig.get("detail", {})
    reasons = []
    if detail.get("multi_tf_trend") == 20: reasons.append("多時框共振")
    if detail.get("price_structure") == 20: reasons.append("結構確認")
    if detail.get("ob_fvg_precision") >= 8: reasons.append("精準回測")
    if detail.get("volume_confirmation") == 10: reasons.append("量能放大")
    key_reasons = " + ".join(reasons[:2]) if reasons else "高品質設定"
    
    # 🔹 風險計算
    risk_pct = abs(sig["entry"] - sig["sl"]) / sig["entry"] * 100
    rr = abs(sig["tp1"] - sig["entry"]) / abs(sig["entry"] - sig["sl"])
    
    # 🔹 等級標籤
    grade_tag = "🔥" if sig["score"] >= 90 else "⭐" if sig["score"] >= 82 else "✅"
    
    sizing = calc_position_sizing(sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], sig["tp3"], sig["side"])
    
    return (
        f"{emoji}{grade_tag} *{coin} {side}* | {sig['score']}分\n"
        f"🎯 `{sig['entry']:.4f}` | 🛑 `{sig['sl']:.4f}` | 🥇 `{sig['tp1']:.4f}`\n"
        f"✅ {key_reasons}\n"
        f"⚠️ 風險 {risk_pct:.2f}% | R:R 1:{rr:.1f}" + (f" | 槓桿 {sizing['leverage']}x" if sizing else "")
    )


def _fmt_tp(coin: str, side: str, order_id: str, tp_level: str, price: float,
            pnl_pct: float, r_mult: float, wick_triggered: bool = False) -> str:
    direction = "做多" if side == "LONG" else "做空"
    advice = (
        "建議平倉 ⅓ 鎖定獲利"
        if tp_level == "TP1"
        else "建議再平倉 ⅓ 落袋為安"
        if tp_level == "TP2"
        else "建議全部平倉，完美收割 🏆"
    )
    wick = "\n🪡 _插針觸發_" if wick_triggered else ""
    return (
        f"🎯 *{coin} {tp_level} 達標！*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"方向：{direction}\n"
        f"觸發價：`{price:.4f}`{wick}\n"
        f"獲利：`{pnl_pct:+.2f}%` (`{r_mult:+.1f}R`)\n"
        f"\n💡 {advice}"
    )


def _fmt_sl(coin: str, side: str, order_id: str, price: float,
            pnl_pct: float, mode: str = "LOSS",
            r_value: float = -1.0, wick_triggered: bool = False) -> str:
    direction = "做多" if side == "LONG" else "做空"
    if mode == "BE":
        label, r_tag, advice = "🔒 保本出場", "`0.0R`", "TP1 已達成、SL 上移到進場價，本筆無損出場 ✨"
    elif mode == "LOCK":
        label, r_tag, advice = "🔐 鎖利出場", f"`+{r_value:.1f}R`", "TP2 已達成、SL 上移到 TP1，鎖住獲利退場 🎉"
    else:
        label, r_tag, advice = "❌ 止損離場", "`-1.0R`", "遵守風控，下一筆訊號會更好 🚀"
    wick = "\n🪡 _插針觸發_" if wick_triggered else ""
    return (
        f"{label} *{coin}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{order_id}`\n"
        f"方向：{direction}\n"
        f"觸發價：`{price:.4f}`{wick}\n"
        f"結果：`{pnl_pct:+.2f}%` {r_tag}\n"
        f"\n💡 {advice}"
    )


def _fmt_position(sig: dict, current_price: float) -> str:
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    entry = sig["entry"]
    pnl = ((current_price - entry) / entry * 100) if side == "LONG" else ((entry - current_price) / entry * 100)
    pnl_e = "🟢" if pnl >= 0 else "🔴"
    if sig.get("hit_tp3"):
        progress = "🏆 TP3 ✅"
    elif sig.get("hit_tp2"):
        progress = "🥇✅ → 🥈✅ → ⏳ TP3"
    elif sig.get("hit_tp1"):
        progress = "🥇✅ → ⏳ TP2"
    else:
        progress = "⏳ 等待 TP1"
    return (
        f"📊 *{coin} 持倉更新*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🆔 `{sig.get('order_id', 'N/A')}`\n"
        f"方向：{direction}\n"
        f"當前：`{current_price:.4f}` {pnl_e}{pnl:+.2f}%\n"
        f"進場：`{entry:.4f}`  SL：`{sig['sl']:.4f}`\n"
        f"進度：{progress}"
    )


# ═════════════════════════════════════════════════════════
# 13. 覆盤分析（保持原邏輯）
# ═════════════════════════════════════════════════════════
def analyze_loss(sig: dict, df_at_loss: list) -> list:
    if not df_at_loss or len(df_at_loss) < 20:
        return [{"code": "SHORT", "title": "📋 資料不足", "detail": "進場後 K 線太少", "severity": 0}]

    side = sig["side"]
    expect = 1 if side == "LONG" else -1
    n = len(df_at_loss)
    df_then = df_at_loss[: max(20, n // 3)]
    df_now = df_at_loss
    reasons = []

    if calc_supertrend(df_then) == expect and calc_supertrend(df_now) == -expect:
        reasons.append({
            "code": "TREND_REVERSAL", "title": "🔄 趨勢反轉",
            "detail": f"進場時 Supertrend 順勢，止損前已翻向反向",
            "severity": 30,
        })

    rsi_then, rsi_now = calc_rsi(df_then), calc_rsi(df_now)
    if side == "LONG" and rsi_then > 45 and rsi_now < 35 and (rsi_then - rsi_now) > 12:
        reasons.append({
            "code": "RSI_COLLAPSE", "title": "📉 多頭動能瓦解",
            "detail": f"RSI 從 {rsi_then:.0f} 急跌至 {rsi_now:.0f}",
            "severity": 25,
        })
    elif side == "SHORT" and rsi_then < 55 and rsi_now > 65 and (rsi_now - rsi_then) > 12:
        reasons.append({
            "code": "RSI_REBOUND", "title": "📈 空頭動能反轉",
            "detail": f"RSI 從 {rsi_then:.0f} 反彈至 {rsi_now:.0f}",
            "severity": 25,
        })

    sweep_dir = "SHORT" if side == "LONG" else "LONG"
    if detect_liquidity_sweep(df_now[-12:], sweep_dir):
        reasons.append({
            "code": "LIQ_SWEEP", "title": "🌊 流動性掃蕩",
            "detail": "止損前出現反向假突破插針後快速收回",
            "severity": 22,
        })

    ob = find_order_block(df_then, side)
    if ob:
        breached = (
            (side == "LONG" and df_now[-1]["c"] < ob["low"])
            or (side == "SHORT" and df_now[-1]["c"] > ob["high"])
        )
        if breached:
            reasons.append({
                "code": "OB_BROKEN", "title": "🧱 訂單塊跌破",
                "detail": "進場依據的 SMC 訂單塊已被收盤跌破",
                "severity": 20,
            })

    atr_then, atr_now = calc_atr(df_then), calc_atr(df_now)
    if atr_then > 0 and atr_now / atr_then > 1.5:
        reasons.append({
            "code": "VOL_SPIKE", "title": "🌪 波動率激增",
            "detail": f"ATR 從 {atr_then:.4f} 擴張至 {atr_now:.4f}（{(atr_now / atr_then - 1) * 100:.0f}%）",
            "severity": 18,
        })

    last10 = df_now[-10:]
    against = sum(
        1 for c in last10
        if (side == "LONG" and c["c"] < c["o"]) or (side == "SHORT" and c["c"] > c["o"])
    )
    if against >= 7:
        reasons.append({
            "code": "AGAINST_K", "title": "💪 持續反向動能",
            "detail": f"出場前 10 根 K 線中 {against} 根反向收線",
            "severity": 15,
        })

    if not reasons:
        reasons.append({
            "code": "NORMAL_NOISE", "title": "📊 正常波動雜訊",
            "detail": "未偵測到明確的趨勢反轉或結構破壞",
            "severity": 5,
        })

    reasons.sort(key=lambda x: -x["severity"])
    return reasons[:3]


def _generate_lessons(reasons: list) -> list:
    advice = {
        "TREND_REVERSAL": "進場後若 Supertrend 翻向反向，建議主動減倉不等止損",
        "RSI_COLLAPSE": "RSI 從中性區（>45）急跌到超賣（<35）通常代表動能轉換",
        "RSI_REBOUND": "RSI 從中性區（<55）反彈到超買（>65）代表空頭動能瓦解",
        "LIQ_SWEEP": "插針型止損後反向 K 隨即出現，多半是主力誘多/誘空",
        "OB_BROKEN": "SMC 訂單塊一旦收盤跌破代表結構失效",
        "VOL_SPIKE": "ATR 突然擴張代表進入高波動區，可縮小倉位",
        "AGAINST_K": "反向 K 連續 7 根以上代表趨勢已轉，應主動止損",
        "NORMAL_NOISE": "本次屬正常波動雜訊，可能 SL 設得太緊",
        "SHORT": "進場後資料不足，無法詳細歸因",
    }
    out, seen = [], set()
    for r in reasons[:2]:
        c = r.get("code")
        if c and c not in seen and c in advice:
            seen.add(c)
            out.append(advice[c])
    return out


def get_similar_stats(score: int, side: str, detail: dict, funding_rate, coin: str) -> tuple:
    state = _load_json(LEARNING_FILE, {})
    bd = state.get("buckets", {}).get(f"coin_side:{coin}_{side}", {})
    n = bd.get("total", 0)
    return (n, bd.get("win", 0), bd.get("loss", 0), bd.get("be", 0))


def _fmt_postmortem(sig: dict, mode: str, reasons: list, lessons: list,
                    similar: tuple | None = None) -> str:
    coin = sig["instId"].split("-")[0]
    side = sig["side"]
    direction = "做多" if side == "LONG" else "做空"
    label = "❌ 止損" if mode == "LOSS" else "🔒 保本" if mode == "BE" else "🔐 鎖利"

    lines = [
        f"🔍 *{coin} 覆盤分析*",
        f"━━━━━━━━━━━━━━",
        f"🆔 `{sig.get('order_id', '?')}`",
        f"⏰ {tw_ts()}",
        f"方向：{direction}　結算：{label}",
        f"原始評分：{sig.get('score', 0)} 分",
        f"",
        f"📋 *主要原因：*",
    ]
    for i, r in enumerate(reasons, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   _{r['detail']}_")
    if lessons:
        lines.append("")
        lines.append("💡 *下次該怎麼判斷：*")
        for l in lessons:
            lines.append(f"  • {l}")
    if similar and similar[0] >= 3:
        n, w, l, be = similar
        wr = w / n * 100
        lines.append("")
        lines.append(
            f"📊 同類設定歷史：{n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）"
        )
    lines.append("")
    lines.append("🧠 _此次主因已寫入學習資料，下次相似情況評分會自動調整_")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 14. 風控（保持原邏輯）
# ═════════════════════════════════════════════════════════
def check_circuit_breaker(cfg: dict) -> tuple[bool, str]:
    cb = cfg.get("circuit_breaker", {})
    threshold = cb.get("loss_threshold", 3)
    pause_h = cb.get("pause_hours", 24)
    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    if len(closed) < threshold:
        return False, ""
    last_n = closed[-threshold:]
    if not all(t.get("close_type") == "SL" for t in last_n):
        return False, ""
    try:
        last_sl_dt = datetime.strptime(last_n[-1]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)
    except Exception:
        return False, ""
    elapsed = (tw_now() - last_sl_dt).total_seconds() / 3600
    if elapsed < pause_h:
        return True, f"🔥 連 {threshold} 敗熔斷，剩餘 `{pause_h - elapsed:.1f}h`"
    return False, ""


def is_in_news_window(cfg: dict) -> tuple[bool, str]:
    now = tw_now()
    for nb in cfg.get("news_blackouts", []):
        try:
            s = datetime.fromisoformat(nb["start"])
            e = datetime.fromisoformat(nb["end"])
            if s.tzinfo is None:
                s = s.replace(tzinfo=TW_TZ)
                e = e.replace(tzinfo=TW_TZ)
            if s <= now <= e:
                return True, nb.get("reason", "新聞事件")
        except Exception:
            continue
    auto = cfg.get("auto_news_blackout", {})
    if auto.get("nfp", True) and now.weekday() == 4 and now.day <= 7:
        cur = now.hour * 60 + now.minute
        if 21 * 60 + 25 <= cur < 22 * 60 + 30:
            return True, "NFP 非農（自動偵測）"
    if auto.get("cpi", True) and 10 <= now.day <= 16:
        cur = now.hour * 60 + now.minute
        if 21 * 60 + 25 <= cur < 22 * 60 + 30:
            return True, "CPI 數據時段（自動偵測）"
    return False, ""


def is_blackout_time(cfg: dict) -> tuple[bool, str]:
    now = tw_now()
    cur = now.hour * 60 + now.minute
    for w in cfg.get("blackout_windows_tw", []):
        try:
            sh, sm = map(int, w["start"].split(":"))
            eh, em = map(int, w["end"].split(":"))
            sm_t, em_t = sh * 60 + sm, eh * 60 + em
            in_window = (
                sm_t <= cur < em_t if sm_t <= em_t
                else (cur >= sm_t or cur < em_t)
            )
            if in_window:
                return True, w.get("reason", "風險時段")
        except Exception:
            continue
    return False, ""


def get_today_stats() -> dict:
    today = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today_trades = [t for t in history if t.get("date") == today]
    closed = [t for t in today_trades if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    return {
        "trades_count": len(today_trades),
        "pnl_pct": sum(t.get("pnl", 0) for t in closed),
        "wins": sum(1 for t in closed if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK")),
        "losses": sum(1 for t in closed if t.get("close_type") == "SL"),
    }


def check_daily_limits(cfg: dict, tracker) -> tuple[bool, str]:
    dl = cfg.get("daily_limits", {})
    open_count = sum(
        1 for s in tracker.signals.values()
        if s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL")
    )
    if open_count >= dl.get("max_concurrent_positions", 2):
        return True, f"📦 持倉達上限：{open_count}/{dl['max_concurrent_positions']}"
    stats = get_today_stats()
    loss_limit = dl.get("daily_loss_limit_pct", 5.0)
    if stats["pnl_pct"] < -loss_limit:
        return True, f"⚠️ 當日 PnL `{stats['pnl_pct']:.2f}%` 已過 -{loss_limit}% 紅線"
    return False, ""


def is_cooling(instId: str, hours: float = 3) -> bool:
    cd = _load_json(SIGNAL_COOLDOWN_FILE, {})
    last = cd.get(instId)
    return last is not None and (time.time() - float(last)) < hours * 3600


def mark_cooldown(instId: str, hours: float = 3) -> None:
    cd = _load_json(SIGNAL_COOLDOWN_FILE, {})
    cd[instId] = time.time()
    cutoff = time.time() - hours * 3600 * 3
    cd = {k: v for k, v in cd.items() if float(v) > cutoff}
    _save_json(SIGNAL_COOLDOWN_FILE, cd)


# ═════════════════════════════════════════════════════════
# 15. 健康監控
# ═════════════════════════════════════════════════════════
def check_health() -> tuple[bool, str]:
    state = get_system_state()
    last_tg = state.get("last_tg_sent", 0)
    last_warn = state.get("last_health_warning", 0)
    if time.time() - last_warn < 6 * 3600:
        return False, ""
    if last_tg > 0:
        hours = (time.time() - last_tg) / 3600
        if hours > 24:
            state["last_health_warning"] = time.time()
            set_system_state(state)
            return True, (
                f"⚠️ *系統健康警報*\n"
                f"超過 *{hours:.0f} 小時*沒送過 TG 訊息\n"
                f"檢查：TG_TOKEN / OKX API / Actions 配額"
            )
    return False, ""


# ═════════════════════════════════════════════════════════
# 16. 交易記錄（增加評分區間統計）
# ═════════════════════════════════════════════════════════
def record_trade(coin: str, side: str, order_id: str, entry: float,
                 close_price: float, close_type: str, score: int,
                 sig_snapshot: dict | None = None) -> None:
    is_win = close_type in ("TP1", "TP2", "TP3", "LOCK")
    pnl = ((close_price - entry) / entry * 100) if side == "LONG" else ((entry - close_price) / entry * 100)
    snap = sig_snapshot or {}
    detail = snap.get("detail", {}) or {}
    fr = snap.get("funding_rate")
    mtf = snap.get("mtf_snapshot")
    features = vectorize_signal(score, side, detail, fr, mtf)
    trade = {
        "time": tw_now().strftime("%Y-%m-%d %H:%M"),
        "date": tw_now().strftime("%Y-%m-%d"),
        "order_id": order_id, "coin": coin, "side": side,
        "entry": entry, "close": close_price, "close_type": close_type,
        "pnl": round(pnl, 2), "is_win": is_win, "score": score,
        "funding_rate": fr, "detail": detail,
        "features": features, "mtf": mtf,
    }
    history = _load_json(TRADE_HISTORY_FILE, [])
    history.append(trade)
    _save_json(TRADE_HISTORY_FILE, history)
    logging.info(f"📝 記錄：{coin} {order_id} {close_type} ({pnl:+.2f}%)")
    
    # 🔹 記錄評分區間勝率統計
    try:
        bucket = "75-81" if 75 <= score < 82 else "82-89" if score < 90 else "90+"
        state = _load_json(LEARNING_FILE, {})
        state.setdefault("score_buckets", {}).setdefault(bucket, {"win":0, "loss":0, "total":0})
        state["score_buckets"][bucket]["total"] += 1
        if is_win:
            state["score_buckets"][bucket]["win"] += 1
        else:
            state["score_buckets"][bucket]["loss"] += 1
        _save_json(LEARNING_FILE, state)
    except Exception:
        pass
    
    try:
        update_learning(trade, sig_snapshot)
    except Exception as e:
        logging.warning(f"⚠️ 學習更新失敗：{e}")


# ═════════════════════════════════════════════════════════
# 17. 訊號追蹤器（保持原邏輯）
# ═════════════════════════════════════════════════════════
class SignalTracker:
    def __init__(self, filepath: str = ACTIVE_SIGNALS_FILE):
        self.filepath = filepath
        self.signals: dict = _load_json(filepath, {})
        self.transitions = 0

    def _save(self) -> None:
        _save_json(self.filepath, self.signals)

    def add(self, signal: dict, active: bool = False) -> tuple[str, str]:
        order_id = f"{int(time.time())}-{uuid.uuid4().hex[:8].upper()}"
        key = f"{signal['instId']}_{signal['side']}_{order_id}"
        now_ts = time.time()
        self.signals[key] = {
            **signal,
            "order_id": order_id,
            "status": "ACTIVE" if active else "PENDING",
            "hit_tp1": False, "hit_tp2": False, "hit_tp3": False,
            "activated_at": now_ts if active else None,
            "last_checked_ts": now_ts if active else None,
            "entry_message_id": None,
        }
        self._save()
        logging.info(f"📌 新增：{order_id} ({signal['instId']} {signal['side']})")
        return key, order_id

    def has_open_position(self, instId: str) -> bool:
        return any(
            s for s in self.signals.values()
            if s.get("instId") == instId
            and s.get("status") in ("PENDING", "ACTIVE", "BE", "TRAIL")
        )

    def set_entry_message_id(self, key: str, msg_id: int | None) -> None:
        if key in self.signals and msg_id:
            self.signals[key]["entry_message_id"] = msg_id
            self._save()

    def check_all(self) -> None:
        self.transitions = 0
        to_remove = []
        for key, sig in list(self.signals.items()):
            if self._check_one(key, sig):
                to_remove.append(key)
        for key in to_remove:
            del self.signals[key]
        if to_remove:
            self._save()

    def _check_one(self, key: str, sig: dict) -> bool:
        try:
            price = fetch_price(sig["instId"])
            if price <= 0:
                return False
            sig["current_price"] = price
            status = sig["status"]
            if status == "PENDING":
                return self._check_pending(sig, price)
            if status not in ("ACTIVE", "BE", "TRAIL"):
                return False

            all_candles = fetch_candles(sig["instId"], tf="15m", limit=100)
            last_ts_s = (
                sig.get("last_checked_ts") or sig.get("activated_at")
                or sig.get("created") or 0
            )
            new_candles = [c for c in all_candles if c["ts"] > int(last_ts_s * 1000)]

            if new_candles and price > 0:
                last = dict(new_candles[-1])
                last["h"] = max(last["h"], price)
                last["l"] = min(last["l"], price)
                new_candles[-1] = last

            for c in new_candles:
                if self._process_candle(sig, c):
                    return True

            confirmed = [c for c in new_candles if c.get("confirmed")]
            if confirmed:
                sig["last_checked_ts"] = max(c["ts"] for c in confirmed) / 1000.0
                self._save()
            return False
        except Exception as e:
            logging.error(f"❌ check_one [{key}]：{e}")
            return False

    def _check_pending(self, sig: dict, price: float) -> bool:
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        side = sig["side"]
        entry = sig["entry"]

        if time.time() > sig["expires"]:
            send_tg(f"⏰ *{coin} 訊號過期*\n🆔 `{order_id}`\n進場 `{entry:.4f}` 未觸發")
            self.transitions += 1
            return True

        in_zone = (
            side == "LONG" and entry * 0.994 <= price <= entry * 1.002
        ) or (
            side == "SHORT" and entry * 0.998 <= price <= entry * 1.006
        )
        if in_zone:
            now_ts = time.time()
            sig["status"] = "ACTIVE"
            sig["activated_at"] = now_ts
            sig["last_checked_ts"] = now_ts
            msg_id = send_tg(_fmt_entry(sig, price), reply_markup=_order_keyboard(order_id))
            if msg_id:
                sig["entry_message_id"] = msg_id
            self._save()
            self.transitions += 1
        return False

    def _process_candle(self, sig: dict, candle: dict) -> bool:
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
        coin = sig["instId"].split("-")[0]
        order_id = sig["order_id"]
        reply_to = sig.get("entry_message_id")
        kb = _order_keyboard(order_id)
        ch, cl, cc = candle["h"], candle["l"], candle["c"]

        if side == "LONG":
            favor_hit = lambda t: ch >= t
            against_hit = lambda t: cl <= t
            wick_favor = lambda t: cc < t and ch >= t
            wick_against = lambda t: cc > t and cl <= t
        else:
            favor_hit = lambda t: cl <= t
            against_hit = lambda t: ch >= t
            wick_favor = lambda t: cc > t and cl <= t
            wick_against = lambda t: cc < t and ch >= t

        if not sig.get("hit_tp1") and favor_hit(tp1):
            sig["hit_tp1"] = True
            sig["sl"] = entry
            sig["status"] = "BE"
            sl = entry
            pnl = ((tp1 - entry) / entry * 100) if side == "LONG" else ((entry - tp1) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP1", tp1, pnl, 1.5, wick_favor(tp1)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, tp1, "TP1", sig["score"], sig)
            self._save()
            self.transitions += 1

        if not sig.get("hit_tp2") and favor_hit(tp2):
            sig["hit_tp2"] = True
            sig["sl"] = tp1
            sig["status"] = "TRAIL"
            sl = tp1
            pnl = ((tp2 - entry) / entry * 100) if side == "LONG" else ((entry - tp2) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP2", tp2, pnl, 3.0, wick_favor(tp2)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, tp2, "TP2", sig["score"], sig)
            self._save()
            self.transitions += 1

        if not sig.get("hit_tp3") and favor_hit(tp3):
            sig["hit_tp3"] = True
            pnl = ((tp3 - entry) / entry * 100) if side == "LONG" else ((entry - tp3) / entry * 100)
            send_tg(_fmt_tp(coin, side, order_id, "TP3", tp3, pnl, 5.0, wick_favor(tp3)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, tp3, "TP3", sig["score"], sig)
            self.transitions += 1
            return True

        if against_hit(sl):
            if sig.get("hit_tp2"):
                mode, r_value, ct = "LOCK", 1.5, "LOCK"
            elif sig.get("hit_tp1"):
                mode, r_value, ct = "BE", 0.0, "BE"
            else:
                mode, r_value, ct = "LOSS", -1.0, "SL"
            pnl = ((sl - entry) / entry * 100) if side == "LONG" else ((entry - sl) / entry * 100)
            send_tg(_fmt_sl(coin, side, order_id, sl, pnl, mode, r_value, wick_against(sl)),
                    reply_markup=kb, reply_to_message_id=reply_to)
            record_trade(coin, side, order_id, entry, sl, ct, sig["score"], sig)
            self._send_postmortem(sig, mode)
            self.transitions += 1
            return True
        return False

    def _send_postmortem(self, sig: dict, mode: str) -> None:
        coin = sig.get("instId", "?").split("-")[0]
        order_id = sig.get("order_id", "?")
        try:
            cfg = load_config()
            if not cfg.get("post_mortem", {}).get("enabled", True):
                return
            if mode == "LOCK" and cfg.get("post_mortem", {}).get("loss_only", False):
                return
            activated = sig.get("activated_at") or sig.get("created") or 0
            all_c = fetch_candles(sig["instId"], tf="15m", limit=100)
            df = [c for c in all_c if (c["ts"] / 1000) >= (activated - 900)]
            if len(df) < 10:
                send_tg(
                    f"🔍 *{coin} 覆盤*\n"
                    f"🆔 `{order_id}`\n"
                    f"進場後資料太少（{len(df)} 根 K 線），可能剛開單就被插針掃損",
                    reply_to_message_id=sig.get("entry_message_id"),
                )
                return
            reasons = analyze_loss(sig, df)
            lessons = _generate_lessons(reasons)
            similar = get_similar_stats(
                sig.get("score", 0), sig["side"],
                sig.get("detail", {}), sig.get("funding_rate"), coin,
            )
            send_tg(
                _fmt_postmortem(sig, mode, reasons, lessons, similar),
                reply_to_message_id=sig.get("entry_message_id"),
            )
            if mode == "LOSS":
                record_loss_reason(coin, sig["side"], reasons)
        except Exception as e:
            logging.error(f"❌ 覆盤失敗：{e}")
            try:
                send_tg(
                    f"🔍 *{coin} 覆盤錯誤*\n🆔 `{order_id}`\n例外：`{str(e)[:100]}`",
                    reply_to_message_id=sig.get("entry_message_id"),
                )
            except Exception:
                pass

    def send_position_updates(self) -> None:
        """📊 持倉更新（15 分鐘 throttle）"""
        state = get_system_state()
        now = time.time()
        if now - state.get("last_position_update_ts", 0) < 15 * 60:
            return
        cnt = 0
        for sig in self.signals.values():
            if sig["status"] not in ("ACTIVE", "BE", "TRAIL"):
                continue
            price = fetch_price(sig["instId"])
            if price <= 0:
                continue
            send_tg(
                _fmt_position(sig, price),
                reply_markup=_order_keyboard(sig.get("order_id", "")),
                reply_to_message_id=sig.get("entry_message_id"),
            )
            cnt += 1
        if cnt:
            state["last_position_update_ts"] = now
            set_system_state(state)
            logging.info(f"📊 持倉更新 × {cnt}")

    def get_position_stats(self) -> str:
        positions = list(self.signals.values())
        if not positions:
            return "📭 *目前無持倉*"
        lines = [f"📊 *追蹤中（{len(positions)} 筆）*", "═" * 18, ""]
        for i, p in enumerate(positions):
            price = fetch_price(p["instId"]) or p["entry"]
            coin = p["instId"].split("-")[0]
            side = p["side"]
            pnl = ((price - p["entry"]) / p["entry"] * 100) if side == "LONG" else ((p["entry"] - price) / p["entry"] * 100)
            pnl_e = "🟢" if pnl >= 0 else "🔴"
            progress = (
                "🏆 TP3" if p.get("hit_tp3")
                else "🥈 TP2" if p.get("hit_tp2")
                else "🥇 TP1" if p.get("hit_tp1")
                else "⏳"
            )
            lines.append(
                f"*{coin}* · {side} · {p.get('score', 0)} 分\n"
                f"🆔 `{p.get('order_id', 'N/A')}`\n"
                f"當前 `{price:.4f}` {pnl_e}{pnl:+.2f}%\n"
                f"進場 `{p['entry']:.4f}` · SL `{p['sl']:.4f}`\n"
                f"進度：{progress}"
            )
            if i < len(positions) - 1:
                lines.append("─" * 18)
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 18. 報表（保持原邏輯）
# ═════════════════════════════════════════════════════════
def _summarize(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    win = sum(1 for t in trades if t.get("close_type") in ("TP1", "TP2", "TP3", "LOCK"))
    loss = sum(1 for t in trades if t.get("close_type") == "SL")
    be = sum(1 for t in trades if t.get("close_type") == "BE")
    pnl = sum(t.get("pnl", 0) for t in trades)
    pnls = [t.get("pnl", 0) for t in trades]
    return {
        "n": n, "win": win, "loss": loss, "be": be,
        "wr": win / n * 100, "pnl": pnl,
        "max_win": max(pnls) if pnls else 0,
        "max_loss": min(pnls) if pnls else 0,
    }


def format_daily_report(date: str | None = None) -> str:
    if date is None:
        date = tw_now().strftime("%Y-%m-%d")
    history = _load_json(TRADE_HISTORY_FILE, [])
    today = [t for t in history if t.get("date") == date]
    s = _summarize(today)
    if s["n"] == 0:
        return f"📭 *日報 {date}*\n當日尚無交易"
    lines = [
        f"📊 *日報 {date}*",
        f"━━━━━━━━━━━━━━",
        f"交易：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）",
        f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        f"",
    ]
    by_coin = {}
    for t in today:
        by_coin.setdefault(t.get("coin", "?"), []).append(t)
    if by_coin:
        lines.append("💎 *各幣種：*")
        for c, ts in sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl", 0) for t in x[1])):
            sub = _summarize(ts)
            lines.append(f"  {c}: {sub['n']} 筆 / 勝率 `{sub['wr']:.0f}%` / `{sub['pnl']:+.2f}%`")
    return "\n".join(lines)


def format_monthly_report(year_month: str | None = None) -> str:
    if year_month is None:
        year_month = tw_now().strftime("%Y-%m")
    history = _load_json(TRADE_HISTORY_FILE, [])
    month = [t for t in history if t.get("date", "").startswith(year_month)]
    s = _summarize(month)
    if s["n"] == 0:
        return f"📭 *月報 {year_month}*\n本月尚無交易"
    cur, max_w, max_l, type_ = 0, 0, 0, None
    for t in month:
        ct = t.get("close_type")
        if ct in ("TP1", "TP2", "TP3", "LOCK"):
            cur = cur + 1 if type_ == "win" else 1
            type_ = "win"; max_w = max(max_w, cur)
        elif ct == "SL":
            cur = cur + 1 if type_ == "loss" else 1
            type_ = "loss"; max_l = max(max_l, cur)
    lines = [
        f"📈 *月報 {year_month}*",
        f"━━━━━━━━━━━━━━",
        f"總交易：{s['n']} 筆（勝 {s['win']} / 平 {s['be']} / 敗 {s['loss']}）",
        f"勝率：`{s['wr']:.0f}%`　PnL：`{s['pnl']:+.2f}%`",
        f"最大獲利：`{s['max_win']:+.2f}%`　最大虧損：`{s['max_loss']:+.2f}%`",
        f"🔥 最大連勝：{max_w}　❄️ 最大連敗：{max_l}",
        f"",
    ]
    by_coin = {}
    for t in month:
        by_coin.setdefault(t.get("coin", "?"), []).append(t)
    if by_coin:
        lines.append("💎 *各幣種：*")
        for c, ts in sorted(by_coin.items(), key=lambda x: -sum(t.get("pnl", 0) for t in x[1])):
            sub = _summarize(ts)
            lines.append(f"  {c}: {sub['n']} 筆 / 勝率 `{sub['wr']:.0f}%` / `{sub['pnl']:+.2f}%`")
    return "\n".join(lines)


def format_learning_report() -> str:
    state = _load_json(LEARNING_FILE, {})
    buckets = state.get("buckets", {})
    by_coin = state.get("by_coin", {})
    if not buckets and not by_coin:
        return "🧠 *學習狀態*\n📭 尚未累積資料（需 5+ 筆已結束交易）"
    lines = ["🧠 *機器人學習狀態*", "━━━━━━━━━━━━━━", ""]
    if by_coin:
        lines.append("📊 *各幣種戰績：*")
        for c, d in sorted(by_coin.items(), key=lambda x: -x[1].get("total", 0))[:12]:
            n, w, l, be = d.get("total", 0), d.get("win", 0), d.get("loss", 0), d.get("be", 0)
            wr = w / n * 100 if n else 0
            lines.append(f"  {c}: {n} 筆（勝 {w} / 平 {be} / 敗 {l}，勝率 `{wr:.0f}%`）")
        lines.append("")
    high = [(b, d) for b, d in buckets.items() if d.get("total", 0) >= 5 and d["win"] / d["total"] > 0.6]
    low = [(b, d) for b, d in buckets.items() if d.get("total", 0) >= 5 and d["win"] / d["total"] < 0.4]
    if high:
        lines.append("✅ *高勝率組合：*")
        for b, d in sorted(high, key=lambda x: -x[1]["win"] / x[1]["total"])[:5]:
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{d['win'] / d['total'] * 100:.0f}%`")
        lines.append("")
    if low:
        lines.append("⚠️ *低勝率組合：*")
        for b, d in sorted(low, key=lambda x: x[1]["win"] / x[1]["total"])[:5]:
            lines.append(f"  `{b}` → {d['total']} 筆，勝率 `{d['win'] / d['total'] * 100:.0f}%`")
        lines.append("")
    lines.append("💡 _累積資料越多，KNN 評分調整越精準_")
    return "\n".join(lines)


def format_audit_report() -> str:
    """🔬 指標有效性審查"""
    history = _load_json(TRADE_HISTORY_FILE, [])
    closed = [t for t in history if t.get("close_type") in ("SL", "BE", "LOCK", "TP1", "TP2", "TP3")]
    n_closed = len(closed)
    if n_closed < 10:
        return f"📭 *指標有效性審查*\n資料不足（{n_closed} 筆 < 10 不足以審查）"

    def _stats(trades):
        n = len(trades)
        if n == 0:
            return None
        wins = sum(1 for t in trades if t["close_type"] in ("TP1", "TP2", "TP3", "LOCK"))
        return {"n": n, "wr": wins / n * 100}

    def _v(diff):
        return "✅" if diff > 10 else "⚠️" if diff > 0 else "❌"

    overall = _stats(closed)
    lines = [
        f"🔬 *指標有效性審查*",
        f"━━━━━━━━━━━━━━",
        f"樣本：{n_closed} 筆",
        f"整體勝率：`{overall['wr']:.0f}%`",
        f"",
    ]

    secs = []
    high_s = [t for t in closed if t.get("score", 0) >= 85]
    low_s = [t for t in closed if 70 <= t.get("score", 0) < 85]
    if len(high_s) >= 3 and len(low_s) >= 5:
        h, l = _stats(high_s), _stats(low_s)
        diff = h["wr"] - l["wr"]
        secs.append(
            f"{_v(diff)} *高分（85+）vs 一般（70-84）*\n"
            f"  高分：{h['n']} 筆 / `{h['wr']:.0f}%`\n"
            f"  一般：{l['n']} 筆 / `{l['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    mtf_a = [t for t in closed if (t.get("features") or {}).get("mtf_h1", 0) == 1.0]
    mtf_o = [t for t in closed if (t.get("features") or {}).get("mtf_h1", 1.0) == 0.0]
    if len(mtf_a) >= 3 and len(mtf_o) >= 3:
        a, o = _stats(mtf_a), _stats(mtf_o)
        diff = a["wr"] - o["wr"]
        secs.append(
            f"{_v(diff)} *MTF 1H 順勢 vs 中性*\n"
            f"  順勢：{a['n']} 筆 / `{a['wr']:.0f}%`\n"
            f"  中性：{o['n']} 筆 / `{o['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    high_v = [t for t in closed if (t.get("features") or {}).get("vol_ratio", 1.0) >= 1.5]
    low_v = [t for t in closed if (t.get("features") or {}).get("vol_ratio", 1.0) < 1.0]
    if len(high_v) >= 3 and len(low_v) >= 3:
        h, l = _stats(high_v), _stats(low_v)
        diff = h["wr"] - l["wr"]
        secs.append(
            f"{_v(diff)} *高量能 vs 低量能*\n"
            f"  高量：{h['n']} 筆 / `{h['wr']:.0f}%`\n"
            f"  低量：{l['n']} 筆 / `{l['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    longs = [t for t in closed if t.get("side") == "LONG"]
    shorts = [t for t in closed if t.get("side") == "SHORT"]
    if longs and shorts:
        l, s = _stats(longs), _stats(shorts)
        diff = l["wr"] - s["wr"]
        be = "✅" if abs(diff) < 10 else "⚠️" if abs(diff) < 20 else "❌"
        secs.append(
            f"{be} *方向平衡*\n"
            f"  LONG：{l['n']} 筆 / `{l['wr']:.0f}%`\n"
            f"  SHORT：{s['n']} 筆 / `{s['wr']:.0f}%`\n"
            f"  差異：`{diff:+.0f}%`"
        )

    if secs:
        lines.append("\n\n".join(secs))
        lines.append("")
    lines.append("💡 ✅=有效（差>10%）　⚠️=邊際　❌=反向（建議降權）")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════
# 19. 主掃描 + 監控
# ═════════════════════════════════════════════════════════
def run_scan(tracker: SignalTracker) -> int:
    cfg = load_config()
    coins = cfg.get("coins", ALL_COINS)
    max_signals = cfg.get("max_signals_per_scan", 2)
    score_thr = cfg.get("score_threshold", 75)
    cooldown_h = cfg.get("cooldown_hours", 3)
    expire_h = cfg.get("signal_expire_hours", 24)
    atr_max = cfg.get("atr_max_pct", 0.04)
    pv_cfg = cfg.get("price_verification", {})
    pv_enabled = pv_cfg.get("enabled", True)
    pv_max_dev = pv_cfg.get("max_deviation_pct", 0.5)
    pv_block = pv_cfg.get("block_on_unverified", False)

    logging.info("🚀 開始掃描...")

    unhealthy, h_msg = check_health()
    if unhealthy:
        send_tg(h_msg)

    cb_active, cb_msg = check_circuit_breaker(cfg)
    if cb_active:
        logging.warning(f"🔥 {cb_msg}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    blocked, b_reason = is_blackout_time(cfg)
    if blocked:
        logging.info(f"🕒 {b_reason}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    in_news, news = is_in_news_window(cfg)
    if in_news:
        logging.info(f"📰 {news}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    limit_hit, limit_msg = check_daily_limits(cfg, tracker)
    if limit_hit:
        logging.info(f"🛡️ {limit_msg}")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    eligible = [
        c for c in coins
        if not tracker.has_open_position(c) and not is_cooling(c, cooldown_h)
    ]
    if not eligible:
        logging.info(f"📭 全部 {len(coins)} 幣都在冷卻 / 持倉，僅跑監控")
        tracker.check_all()
        tracker.send_position_updates()
        return 0

    sent = 0
    logging.info(f"🎯 可掃：{[c.split('-')[0] for c in eligible]}")
    for instId in eligible:
        if sent >= max_signals:
            break
        try:
            okx_price = fetch_price(instId)
            if okx_price <= 0:
                continue

            if pv_enabled:
                ok, tv_price, diff = verify_price(instId, okx_price, pv_max_dev, pv_block)
                if not ok:
                    if tv_price is not None:
                        send_tg(
                            f"⚠️ *{instId.split('-')[0]} 價格異常*\n"
                            f"OKX `{okx_price:.4f}` vs TV `{tv_price:.4f}`\n"
                            f"偏離 `{diff:.3f}%` > {pv_max_dev}%"
                        )
                    continue

            df = fetch_candles(instId, tf="15m", limit=100, include_unconfirmed=False)
            if not df:
                continue

            funding = fetch_funding_rate(instId)
            sig = generate_signal(
                instId, df, okx_price, funding,
                score_threshold=score_thr,
                atr_max_pct=atr_max,
                signal_expire_hours=expire_h,
            )
            if not sig:
                continue

            in_zone = (
                sig["side"] == "LONG"
                and sig["entry"] * 0.994 <= okx_price <= sig["entry"] * 1.002
            ) or (
                sig["side"] == "SHORT"
                and sig["entry"] * 0.998 <= okx_price <= sig["entry"] * 1.006
            )

            key, order_id = tracker.add(sig, active=in_zone)

            if in_zone:
                signal_with_id = dict(sig)
                signal_with_id["order_id"] = order_id
                msg_id = send_tg(
                    _fmt_entry(signal_with_id, okx_price),
                    reply_markup=_order_keyboard(order_id),
                )
                tracker.set_entry_message_id(key, msg_id)
                logging.info(f"✅ {instId} 進場通知已送（{order_id}）")
            else:
                send_tg(
                    f"📍 *{instId.split('-')[0]} 訊號就位*\n"
                    f"🆔 `{order_id}`\n"
                    f"進場 `{sig['entry']:.4f}`（當前 `{okx_price:.4f}`）\n"
                    f"等價格進入區間自動觸發",
                    reply_markup=_order_keyboard(order_id),
                )
                logging.info(f"📍 {instId} PENDING")

            mark_cooldown(instId, cooldown_h)
            sent += 1
        except Exception as e:
            logging.error(f"[{instId}] 失敗：{e}")
            continue

    tracker.check_all()
    tracker.send_position_updates()
    logging.info(f"✅ 掃描完成，本輪 {sent} 筆")
    return sent


def run_monitor(tracker: SignalTracker) -> None:
    if not tracker.signals:
        return
    logging.info(f"🔔 monitor：追蹤 {len(tracker.signals)} 筆")
    tracker.check_all()
    tracker.send_position_updates()


# ═════════════════════════════════════════════════════════
# 20. 主入口
# ═════════════════════════════════════════════════════════
def main() -> None:
    try:
        logging.info("=" * 50)
        logging.info("🤖 Alpha Oracle Pro v15.2 專業精簡版")
        logging.info(f"⏰ {tw_ts()}")
        logging.info("=" * 50)

        tracker = SignalTracker(ACTIVE_SIGNALS_FILE)

        if len(sys.argv) > 1:
            cmd = sys.argv[1]
            if cmd in ("/stats", "/持倉", "stats"):
                send_tg(tracker.get_position_stats()); return
            if cmd in ("/daily", "/日報", "daily"):
                date = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_daily_report(date)); return
            if cmd in ("/monthly", "/月報", "monthly"):
                ym = sys.argv[2] if len(sys.argv) > 2 else None
                send_tg(format_monthly_report(ym)); return
            if cmd in ("/learning", "/學習", "learning"):
                send_tg(format_learning_report()); return
            if cmd in ("/audit", "/審查", "audit"):
                send_tg(format_audit_report()); return
            if cmd in ("monitor", "/monitor", "/監控"):
                run_monitor(tracker); return

        run_scan(tracker)
        logging.info("🎉 完成")
    except Exception as e:
        logging.error(f"🔥 系統錯誤：{e}")
        try:
            s = get_system_state()
            s["scan_failure_count"] = s.get("scan_failure_count", 0) + 1
            set_system_state(s)
        except Exception:
            pass
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
